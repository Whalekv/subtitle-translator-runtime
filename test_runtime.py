"""验证会丢译文、改错时间或覆盖目录的关键风险；所有文件只放临时中文目录。

在本仓库根目录运行：python -m unittest discover -s . -p test_runtime.py -v。
不会下载模型，不会访问网络，不会改用户主目录配置。
"""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("subtitle_translator", Path(__file__).with_name("subtitle_translator.py"))
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class RuntimeTests(unittest.TestCase):
    """各测试从同一短原文起步，只改变需要验证的风险条件。"""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="字幕测试 空格 ")
        self.base = Path(self.temporary.name)
        self.root = self.base / "工具 数据"
        self.cfg_path = self.base / "配置.json"
        for name in app.FOLDERS:
            (self.root / name).mkdir(parents=True)
        self.cfg = {"config_version": "1.0", "script_version": app.VERSION, "runtime_root": str(self.root),
                    "output_root": str(self.root / "outputs"), "launcher": {"program": sys.executable, "args": []},
                    "selected_model": None, "model_files": {}, "tools": {}}
        app.atomic_write(self.cfg_path, self.cfg)
        self.job = {"job_id": "demoVideo01.zh-Hans.test", "script_version": app.VERSION,
                    "source_language": "en", "target_language": "zh-Hans", "caption_status": "available",
                    "video": {"platform": "youtube", "id": "demoVideo01", "url": "https://www.youtube.com/watch?v=demoVideo01", "title": "测试"},
                    "duration": 10.0, "warnings": [], "status": "inspected"}
        self.folder = self.root / "jobs" / self.job["job_id"]
        self.folder.mkdir()
        self.cues = [{"start": 0.0, "end": 2.0, "source_text": "Hello world."}, {"start": 2.0, "end": 4.125, "source_text": "Maybe 30%."}]
        app.save_source(self.folder, self.job, self.cues, "local", self.folder / "original.vtt")
        self.args = argparse.Namespace(config=str(self.cfg_path), job=self.job["job_id"], segmentation_plan=None, chunk_size=1)
        app.prepare(self.args)
        self.review_plan()

    def tearDown(self):
        self.temporary.cleanup()

    def review_plan(self):
        """测试原文的两句本身完整；模拟会话完成整句审查后，按真实接口提交计划副本。"""
        plan = app.read_json(self.folder / "plan.json")
        plan["segmentation_reviewed"] = True
        path = self.base / "已复核分段.json"
        app.atomic_write(path, plan)
        return app.prepare(argparse.Namespace(**(vars(self.args) | {"segmentation_plan": str(path)})))

    def incoming(self, items, partial=False, **extra):
        path = self.base / "译文.json"
        app.atomic_write(path, {"job_id": self.job["job_id"], "target_language": "zh-Hans", "items": items, **extra})
        return argparse.Namespace(**vars(self.args), file=str(path), partial=partial)

    def all_items(self):
        return [{"cue_id": "cue-000001", "translated_text": "你好，\n世界。"}, {"cue_id": "cue-000002", "translated_text": "可能是 30%。"}]

    def test_config_before_and_after_subcommand(self):
        """--config位置不影响选择，不能写到真正的用户配置。"""
        script = str(Path(app.__file__))
        for arguments in (["--config", str(self.cfg_path), "--json", "jobs"], ["jobs", "--json", "--config", str(self.cfg_path)]):
            result = subprocess.run([sys.executable, script] + arguments, capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["data"]["jobs"][0]["job_id"], self.job["job_id"])

    def test_missing_translation_not_exported(self):
        with self.assertRaises(app.ToolError) as caught:
            app.accept_translations(self.incoming(self.all_items()[:1]))
        self.assertEqual(caught.exception.code, "INVALID_TRANSLATION")
        self.assertFalse((self.folder / "translations.json").exists())
        with self.assertRaises(app.ToolError):
            app.export(argparse.Namespace(**vars(self.args), output=None))
        self.assertEqual(list((self.root / "outputs").iterdir()), [])

    def test_resume_and_pair_export(self):
        app.accept_translations(self.incoming(self.all_items()[:1], partial=True))
        state = app.resume(self.args)
        self.assertEqual(state["missing_ids"], ["cue-000002"])
        app.prepare(self.args)
        app.accept_translations(self.incoming(self.all_items()[1:]))
        result = app.export(argparse.Namespace(**vars(self.args), output=None))
        bundle = app.read_json(result["json_file"])
        srt = app.parse_subtitles(result["srt_file"])
        self.assertEqual(len(bundle["cues"]), len(srt))
        for a, b in zip(bundle["cues"], srt):
            self.assertEqual((a["start"], a["end"], a["translated_text"]), (b["start"], b["end"], b["source_text"]))

    def test_invalid_items_keep_previous_progress(self):
        app.accept_translations(self.incoming(self.all_items()[:1], partial=True))
        previous = (self.folder / "translations.json").read_bytes()
        bad_lists = [self.all_items() + self.all_items()[:1], [{"cue_id": "extra", "translated_text": "多了"}],
                     [{"cue_id": "cue-000002", "translated_text": ""}],
                     [{"cue_id": "cue-000002", "translated_text": "改时间", "start": 3}],
                     [{"cue_id": "cue-000002", "translated_text": "有\n\n空行"}]]
        for items in bad_lists:
            with self.assertRaises(app.ToolError):
                app.accept_translations(self.incoming(items, partial=True))
            self.assertEqual((self.folder / "translations.json").read_bytes(), previous)
        with self.assertRaises(app.ToolError):
            app.accept_translations(self.incoming(self.all_items(), target_language="ja"))

    def test_rolling_dedup_keeps_spoken_emphasis(self):
        cues, _ = app.normalize_cues([
            {"start": 0, "end": 2, "source_text": "very"},
            {"start": 1, "end": 3, "source_text": "very very good"},
            {"start": 3, "end": 4, "source_text": "very very good"},
        ], automatic=True)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["source_text"], "very very good")
        self.assertEqual(cues[1]["source_text"], "very very good")

    def test_accidental_overlap_is_not_silently_trimmed(self):
        cues, _ = app.normalize_cues([{"start": 0, "end": 2, "source_text": "First."}, {"start": 1, "end": 3, "source_text": "Second."}])
        self.assertEqual(cues[0]["end"], 2)
        self.assertTrue(app.timeline_errors(cues))
        app.save_source(self.folder, self.job, cues, "local", self.folder / "original.vtt")
        with self.assertRaises(app.ToolError):
            app.prepare(self.args)

    def test_source_changes_invalidate_resume(self):
        source = app.read_json(self.folder / "source.json")
        source["cues"][0]["source_text"] = "Changed"
        app.atomic_write(self.folder / "source.json", source)
        with self.assertRaises(app.ToolError) as caught:
            app.resume(self.args)
        self.assertEqual(caught.exception.code, "SOURCE_CHANGED")

    def test_plan_split_needs_matching_word_timestamps(self):
        source = app.read_json(self.folder / "source.json")
        first = source["cues"][0]
        first["words"] = [{"start": 0.0, "end": 1.0, "text": "Hello "}, {"start": 1.0, "end": 2.0, "text": "world."}]
        plan = app.read_json(self.folder / "plan.json")
        plan["source_hash"] = app.fingerprint(source)
        plan["cues"][:1] = [
            {"id": "part-1", "start": 0.0, "end": 1.0, "source_text": "Hello", "source_ids": ["cue-000001"]},
            {"id": "part-2", "start": 1.0, "end": 2.0, "source_text": "world.", "source_ids": ["cue-000001"]}]
        self.assertEqual(len(app.validate_plan(plan, source, self.job)), 3)
        plan["cues"][0]["end"], plan["cues"][1]["start"] = 0.5, 0.5
        with self.assertRaises(app.ToolError):
            app.validate_plan(plan, source, self.job)

    def test_offset_is_added_in_seconds(self):
        raw = {"transcription": [{"offsets": {"from": 1500, "to": 2750}, "text": "Hello"}]}
        cue = app.whisper_cues(raw, offset=600.0)[0]
        self.assertEqual((cue["start"], cue["end"]), (601.5, 602.75))

    def test_no_model_blocks_asr_before_download(self):
        self.job["caption_status"] = "none"
        app.atomic_write(self.folder / "job.json", self.job)
        with patch.object(app, "ytdlp") as network:
            with self.assertRaises(app.ToolError) as caught:
                app.transcribe(argparse.Namespace(**vars(self.args), audio=None))
            self.assertEqual(caught.exception.code, "MODEL_REQUIRED")
            network.assert_not_called()

    def test_network_error_not_no_captions(self):
        args = argparse.Namespace(config=str(self.cfg_path), url="https://www.youtube.com/watch?v=jNQXAC9IVRw", metadata=None, source_lang=None, target_lang="zh-Hans")
        with patch.object(app, "ytdlp", side_effect=app.ToolError("CAPTION_ACCESS_FAILED", "HTTP 403")):
            with self.assertRaises(app.ToolError) as caught:
                app.inspect_video(args)
        self.assertEqual(caught.exception.code, "CAPTION_ACCESS_FAILED")
        self.assertEqual(len(list((self.root / "jobs").iterdir())), 1)

    def test_track_selection_ignores_automatic_translation(self):
        info = {"subtitles": {"en": [{"ext": "vtt", "url": "https://example.test/captions?lang=en"}]},
                "automatic_captions": {"zh": [{"ext": "vtt", "url": "https://example.test/captions?lang=en&tlang=zh"}]}}
        tracks = app.caption_tracks(info)
        self.assertEqual([x["language"] for x in tracks], ["en"])

    def test_migration_conflict_and_failure_preserve_configuration(self):
        before = self.cfg_path.read_bytes()
        conflict = self.base / "已有 文件"
        conflict.mkdir()
        (conflict / "重要文件.txt").write_text("不要覆盖", encoding="utf-8")
        with self.assertRaises(app.ToolError):
            app.migrate(argparse.Namespace(config=str(self.cfg_path), to=str(conflict)))
        self.assertEqual(self.cfg_path.read_bytes(), before)
        destination = self.base / "另一个目录"
        with patch.object(app.shutil, "copy2", side_effect=OSError("模拟磁盘失败")):
            with self.assertRaises(OSError):
                app.migrate(argparse.Namespace(config=str(self.cfg_path), to=str(destination)))
        self.assertEqual(self.cfg_path.read_bytes(), before)
        self.assertTrue((self.folder / "source.json").exists())

    def test_migrate_then_resume_unicode_path(self):
        app.accept_translations(self.incoming(self.all_items()[:1], partial=True))
        destination = self.base / "迁移后的 中文目录"
        app.migrate(argparse.Namespace(config=str(self.cfg_path), to=str(destination)))
        self.assertEqual(app.resume(self.args)["missing_ids"], ["cue-000002"])
        self.assertTrue((self.folder / "source.json").exists())
        migrated_source = app.read_json(destination / "jobs" / self.job["job_id"] / "source.json")
        self.assertTrue(Path(migrated_source["raw_file"]).is_relative_to(destination))

    def test_import_new_source_does_not_overwrite_old_raw(self):
        original = self.base / "原始字幕.vtt"
        original.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello world.\n\n00:00:02.000 --> 00:00:04.125\nMaybe 30%.\n", encoding="utf-8")
        # 创建一个尚未准备的独立任务，再以真实fetch路径保存原始依据。
        (self.folder / "plan.json").unlink()
        app.fetch(argparse.Namespace(**vars(self.args), file=str(original), language=None))
        app.prepare(self.args)
        self.review_plan()
        app.accept_translations(self.incoming(self.all_items()))
        source = app.read_json(self.folder / "source.json")
        old_raw = Path(source["raw_file"])
        before = old_raw.read_bytes()
        original.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nA different source.\n", encoding="utf-8")
        with self.assertRaises(app.ToolError) as caught:
            app.fetch(argparse.Namespace(**vars(self.args), file=str(original), language=None))
        self.assertEqual(caught.exception.code, "SOURCE_CHANGED")
        self.assertEqual(old_raw.read_bytes(), before)
        self.assertEqual(app.read_json(self.folder / "source.json"), source)

    def test_doctor_detects_deleted_launcher(self):
        self.cfg["launcher"] = {"program": sys.executable, "args": [str(self.root / "missing.py")]}
        self.cfg["tools"]["yt_dlp"] = ["existing-tool"]
        with patch.object(app, "probe", return_value={"available": True, "version": "test"}):
            report = app.doctor_data(self.cfg)
        self.assertFalse(report["script_ready"])
        self.assertFalse(report["ready_for_captions"])

    def test_pair_write_failure_restores_both_files(self):
        json_path, srt_path = self.base / "字幕.json", self.base / "字幕.srt"
        json_path.write_bytes(b"old-json")
        srt_path.write_bytes(b"old-srt")
        real_replace = app.os.replace
        def fail_second(source, target):
            if str(source).endswith(".srt.pending"):
                raise OSError("模拟第二个文件被占用")
            return real_replace(source, target)
        with patch.object(app.os, "replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                app.commit_pair(json_path, b"new-json", srt_path, b"new-srt")
        self.assertEqual(json_path.read_bytes(), b"old-json")
        self.assertEqual(srt_path.read_bytes(), b"old-srt")

    def test_plain_code_and_escaped_html_are_never_deleted(self):
        plain = "Use vector<T> when x < 3 > 2."
        path = self.base / "原文.json"
        app.atomic_write(path, {"cues": [{"start": 0, "end": 3, "source_text": plain}]})
        normalized, _ = app.normalize_cues(app.parse_subtitles(path))
        self.assertEqual(normalized[0]["source_text"], plain)
        vtt = self.base / "代码.vtt"
        vtt.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:03.000\n<b>Use</b> &lt;button&gt; to submit.\n", encoding="utf-8")
        normalized, _ = app.normalize_cues(app.parse_subtitles(vtt))
        self.assertEqual(normalized[0]["source_text"], "Use <button> to submit.")
        self.assertEqual(app.caption_markup(plain), plain)

    def test_corrupt_subtitle_block_is_not_silently_skipped(self):
        path = self.base / "损坏.srt"
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\nFirst.\n\n2\n00:00:01,000 -> 00:00:02,000\nSecond.\n", encoding="utf-8")
        with self.assertRaises(app.ToolError) as caught:
            app.parse_subtitles(path)
        self.assertEqual(caught.exception.code, "INVALID_TIMELINE")
        vtt = self.base / "元数据.vtt"
        vtt.write_text("WEBVTT\n\nNOTE this is metadata\nnot dialogue\n\nSTYLE\n::cue {color:white;}\n\n00:00:00.000 --> 00:00:01.000\nFirst.\n", encoding="utf-8")
        self.assertEqual(len(app.parse_subtitles(vtt)), 1)

    def test_json3_missing_duration_and_invalid_clock_are_rejected(self):
        path = self.base / "损坏.json3"
        for duration in (None, 0):
            app.atomic_write(path, {"events": [{"tStartMs": 0, "dDurationMs": duration, "segs": [{"utf8": "A sentence cannot disappear."}]}]})
            with self.assertRaises(app.ToolError) as caught:
                app.parse_subtitles(path)
            self.assertEqual(caught.exception.code, "INVALID_TIMELINE")
        for value in ("00:00:99.000", "00:60:00.000"):
            with self.assertRaises(app.ToolError):
                app.timestamp(value)

    def test_asr_pipeline_uses_whole_audio_without_translation_api(self):
        """小型假模型只验证工具衔接/命令/偏移；不把它声称为真实语音识别质量验证。"""
        model = self.root / "models" / "fixture.bin"
        model.write_bytes(b"test-only, not a speech model")
        self.cfg.update({"selected_model": "base", "model_files": {"base": {"path": str(model), "sha256": app.digest(model), "size": model.stat().st_size}},
                         "tools": {"ffmpeg": ["fake-ffmpeg"], "whisper": ["fake-whisper"]}})
        app.atomic_write(self.cfg_path, self.cfg)
        self.job["caption_status"] = "none"
        app.atomic_write(self.folder / "job.json", self.job)
        local = self.base / "完整 原音频.wav"
        local.write_bytes(b"fixture-audio")
        commands = []
        def fake_tool(command, **kwargs):
            commands.append(command)
            if command[0] == "fake-ffmpeg":
                Path(command[-1]).write_bytes(b"fixture-converted")
            else:
                output = Path(command[command.index("-of") + 1] + ".json")
                app.atomic_write(output, {"result": {"language": "en"}, "transcription": [{"offsets": {"from": 1000, "to": 2500}, "text": "Hello from real offsets."}]})
            return ""
        with patch.object(app, "run", side_effect=fake_tool):
            result = app.transcribe(argparse.Namespace(**vars(self.args), audio=str(local)))
        source = app.read_json(result["source_file"])
        self.assertEqual((source["cues"][0]["start"], source["cues"][0]["end"]), (1.0, 2.5))
        self.assertNotIn("-ss", commands[0])
        self.assertNotIn("-tr", commands[1])
        self.assertTrue(Path(source["raw_file"]).is_relative_to(self.folder / "asr"))
        job = app.read_json(self.folder / "job.json")
        self.assertIsNone(job["recognition"]["version"])

    def test_new_task_cannot_skip_sentence_review(self):
        """未复核草稿不能偷偷进入翻译或导出；删字段或填字符串也不能代替确认。"""
        plan_path = self.folder / "plan.json"
        for flag in (False, None, "true"):
            plan = app.read_json(plan_path)
            plan["segmentation_reviewed"] = flag
            app.atomic_write(plan_path, plan)
            for operation in (lambda: app.accept_translations(self.incoming(self.all_items(), partial=True)),
                              lambda: app.export(argparse.Namespace(**vars(self.args), output=None))):
                with self.assertRaises(app.ToolError) as caught:
                    operation()
                self.assertEqual(caught.exception.code, "SEGMENTATION_REVIEW_REQUIRED")
            self.assertFalse((self.folder / "translations.json").exists())
        self.assertEqual(app.prepare(self.args)["segmentation_status"], "required")

    def test_three_fragments_export_as_one_sentence_with_original_span(self):
        """重现用户的三条碎片：合成一条两行字幕，JSON/SRT 均保持第一条到最后一条的时间。"""
        (self.folder / "plan.json").unlink()
        fragments = [
            {"start": 0.125, "end": 2.0, "source_text": "On our leaderboard where large language models"},
            {"start": 2.0, "end": 4.5, "source_text": "judge images, it took"},
            {"start": 4.5, "end": 6.375, "source_text": "second place."}]
        app.save_source(self.folder, self.job, fragments, "local", self.folder / "original.vtt")
        prepared = app.prepare(self.args)
        plan = app.read_json(prepared["plan_file"])
        plan["cues"] = [{"id": "sentence-1", "start": 0.125, "end": 6.375,
                         "source_text": " ".join(item["source_text"] for item in fragments),
                         "source_ids": ["cue-000001", "cue-000002", "cue-000003"],
                         "same_speaker_confirmed": True, "semantic_continuity_confirmed": True}]
        plan["segmentation_reviewed"] = True
        path = self.base / "整句计划.json"
        app.atomic_write(path, plan)
        app.prepare(argparse.Namespace(**(vars(self.args) | {"segmentation_plan": str(path)})))
        text = "在我们这个由大语言模型评判图像的排行榜上，\n它拿到了第二名。"
        app.accept_translations(self.incoming([{"cue_id": "sentence-1", "translated_text": text}]))
        result = app.export(argparse.Namespace(**vars(self.args), output=None))
        cues = app.read_json(result["json_file"])["cues"]
        srt = app.parse_subtitles(result["srt_file"])
        self.assertEqual(len(cues), 1)
        self.assertEqual((cues[0]["start"], cues[0]["end"], cues[0]["translated_text"]), (0.125, 6.375, text))
        self.assertEqual((srt[0]["start"], srt[0]["end"], srt[0]["source_text"]), (0.125, 6.375, text))
        self.assertEqual(app.read_json(self.folder / "review.json")["segmentation_issues"], [])

    def regroup_fixture(self):
        """构造一句跨两个原片段、第二句在第二片段内开始的原文，用于验证真实词边界。"""
        source = app.read_json(self.folder / "source.json")
        source["cues"] = [
            {"id": "a", "start": 0.125, "end": 2.0, "source_text": "It took"},
            {"id": "b", "start": 2.0, "end": 6.375, "source_text": "second place. Next topic.", "words": [
                {"start": 2.0, "end": 3.0, "text": "second "},
                {"start": 3.0, "end": 4.0, "text": "place. "},
                {"start": 4.0, "end": 5.0, "text": "Next "},
                {"start": 5.0, "end": 6.375, "text": "topic."}]}]
        plan = {"job_id": self.job["job_id"], "source_hash": app.fingerprint(source), "segmentation_reviewed": True, "cues": [
            {"id": "sentence-1", "start": 0.125, "end": 4.0, "source_text": "It took second place.",
             "source_ids": ["a", "b"], "same_speaker_confirmed": True, "semantic_continuity_confirmed": True},
            {"id": "sentence-2", "start": 4.0, "end": 6.375, "source_text": "Next topic.", "source_ids": ["b"]}]}
        return source, plan

    def test_merge_then_split_uses_real_words_without_losing_text(self):
        source, plan = self.regroup_fixture()
        result = app.validate_plan(plan, source, self.job)
        self.assertEqual([(c["start"], c["end"]) for c in result], [(0.125, 4.0), (4.0, 6.375)])
        # 不能把边界挪到词中间，也不能只改文字而保留不对应的时间。
        for bad in ("invented_time", "missing_text", "time_hole", "duplicate_text"):
            broken = copy.deepcopy(plan)
            if bad == "invented_time":
                broken["cues"][0]["end"] = broken["cues"][1]["start"] = 3.5
            elif bad == "missing_text":
                broken["cues"][0]["source_text"] = "It took place."
            elif bad == "time_hole":
                broken["cues"][1]["start"] = 5.0
                broken["cues"][1]["source_text"] = "topic."
            else:
                broken["cues"][1]["source_text"] = "second place. Next topic."
            with self.subTest(bad=bad), self.assertRaises(app.ToolError):
                app.validate_plan(broken, source, self.job)

    def test_incomplete_or_invalid_word_evidence_cannot_split(self):
        source, plan = self.regroup_fixture()
        for bad in ("missing", "incomplete", "reversed", "outside", "boolean"):
            changed = copy.deepcopy(source)
            words = changed["cues"][1]["words"]
            if bad == "missing":
                changed["cues"][1].pop("words")
            elif bad == "incomplete":
                words.pop()
            elif bad == "reversed":
                words.reverse()
            elif bad == "outside":
                words[-1]["end"] = 99
            else:
                words[0]["start"] = True
            plan["source_hash"] = app.fingerprint(changed)
            with self.subTest(bad=bad), self.assertRaises(app.ToolError):
                app.validate_plan(plan, changed, self.job)

    def test_merge_rejects_long_pause_speaker_change_and_unconfirmed_speaker(self):
        source, _ = self.regroup_fixture()
        source["cues"][1].pop("words")
        plan = {"job_id": self.job["job_id"], "cues": [{"id": "whole", "start": 0.125, "end": 6.375,
                "source_ids": ["a", "b"], "source_text": "It took second place. Next topic.",
                "same_speaker_confirmed": True, "semantic_continuity_confirmed": True}]}
        for bad in ("pause", "speaker", "unconfirmed", "outside_time"):
            changed, broken = copy.deepcopy(source), copy.deepcopy(plan)
            if bad == "pause":
                changed["cues"][1]["start"] = 4.0
            elif bad == "speaker":
                changed["cues"][0]["speaker"] = "Alice"
                changed["cues"][1]["speaker"] = "Bob"
            elif bad == "unconfirmed":
                broken["cues"][0]["same_speaker_confirmed"] = False
            else:
                broken["cues"][0]["start"] = 0
            broken["source_hash"] = app.fingerprint(changed)
            with self.subTest(bad=bad), self.assertRaises(app.ToolError):
                app.validate_plan(broken, changed, self.job)

    def test_fragment_and_length_warnings_do_not_rewrite_subtitles(self):
        cues = [{"id": "a", "start": 0, "end": 2, "translated_text": "在这个排行榜上，它拿到了"},
                {"id": "b", "start": 2, "end": 3, "translated_text": "第二名。"}]
        before = copy.deepcopy(cues)
        issues = app.segmentation_issues(cues, "zh-Hans")
        self.assertIn("possible_sentence_break", [x["code"] for x in issues])
        self.assertIn("short_cue", [x["code"] for x in issues])
        self.assertEqual(cues, before)
        issues = app.segmentation_issues([{"id": "long", "start": 0, "end": 12, "translated_text": "完整句子" * 15}], "zh-Hans")
        self.assertEqual(issues[0]["code"], "long_cue")

    def test_resegment_preserves_old_progress_and_exports_without_network(self):
        app.accept_translations(self.incoming(self.all_items()))
        exported = app.export(argparse.Namespace(**vars(self.args), output=None))
        paths = list(self.folder.glob("*.json")) + [Path(exported["json_file"]), Path(exported["srt_file"])]
        before = {path: path.read_bytes() for path in paths}
        with patch.object(app, "ytdlp") as network:
            result = app.resegment(self.args)
            network.assert_not_called()
        new_args = argparse.Namespace(**(vars(self.args) | {"job": result["job_id"]}))
        prepared = app.prepare(new_args)
        self.assertEqual(prepared["segmentation_status"], "required")
        new_folder = Path(result["job_directory"])
        self.assertFalse((new_folder / "translations.json").exists())
        self.assertEqual(app.read_json(new_folder / "job.json")["parent_job_id"], self.job["job_id"])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_old_version_can_resume_and_fork_without_relabeling_as_reviewed(self):
        job = app.read_json(self.folder / "job.json")
        job["script_version"] = "0.1.0"
        job.pop("segmentation_policy")
        app.atomic_write(self.folder / "job.json", job)
        plan = app.read_json(self.folder / "plan.json")
        plan.pop("segmentation_reviewed")
        app.atomic_write(self.folder / "plan.json", plan)
        app.accept_translations(self.incoming(self.all_items()))
        self.assertEqual(app.resume(self.args)["segmentation_status"], "legacy")
        self.assertEqual(app.validate(self.args)["segmentation_status"], "legacy")
        result = app.resegment(self.args)
        self.assertEqual(app.read_json(Path(result["job_directory"]) / "job.json")["script_version"], app.VERSION)

    def test_resegment_rejects_unsafe_destination_before_creating_files(self):
        """旧任务元数据损坏时，不能把整个新任务目录写到 jobs 之外。"""
        job = app.read_json(self.folder / "job.json")
        job["video"]["id"] = "../../outside"
        app.atomic_write(self.folder / "job.json", job)
        previous = list(self.folder.parent.iterdir())
        with self.assertRaises(app.ToolError) as caught:
            app.resegment(self.args)
        self.assertEqual(caught.exception.code, "INVALID_INPUT")
        self.assertEqual(list(self.folder.parent.iterdir()), previous)


if __name__ == "__main__":
    unittest.main()
