#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕工具 0.2.0：取原文、校验整句分段、保存译文，并输出插件 JSON 和 SRT。

翻译由当前聊天里的模型完成，本脚本不调用翻译 API。常调参数集中在 settings.json；
工具和模型的固定下载版本在 dependencies.json。所有路径和外部命令都分开传递，
因此中文、空格和视频地址里的符号不会被当作另一条命令执行。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
import hashlib
import html
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

VERSION = "0.2.0"
HERE = Path(__file__).resolve().parent
FOLDERS = ("scripts", "tools", "models", "cache", "jobs", "outputs")
DEFAULT_CONFIG = Path.home() / ".subtitle-translator" / "config.json"


# 所有可预期失败都带错误编号。调用方能据此决定重试、补译或请用户选模型。
class ToolError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data or {}


def read_json(path):
    """读 UTF-8 JSON；兼容 Windows 编辑器的 BOM，但拒绝重复字段和 NaN。"""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"重复字段：{key}")
            result[key] = value
        return result
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, ValueError) as exc:
        raise ToolError("INVALID_INPUT", f"无法读取 JSON：{path}；{exc}") from exc


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def atomic_write(path, data):
    """先写同目录临时文件，再替换正式文件；中途失败仍保留旧的完整文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data if isinstance(data, bytes) else json_bytes(data)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def fingerprint(data):
    return hashlib.sha256(json_bytes(data)).hexdigest()


def settings():
    return read_json(HERE / "settings.json")


def manifest():
    return read_json(HERE / "dependencies.json")


def absolute(value):
    """目录必须由用户指定完整位置，避免当前工作目录改变后悄悄存到别处。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ToolError("INVALID_INPUT", "请提供完整的绝对路径。")
    return path.resolve()


def writable(path):
    path.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError as exc:
        raise ToolError("INVALID_INPUT", f"目录不可写：{path}；{exc}") from exc


@contextlib.contextmanager
def runtime_lock(root):
    """操作期间锁住工具目录；进程退出自动释放，不会把中断误当成永久占用。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".runtime.lock").open("a+b") as handle:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ToolError("BUSY", "另一项任务正在使用该目录；等它结束后重试。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def config(args):
    path = Path(args.config)
    if not path.exists():
        raise ToolError("SETUP_REQUIRED", "尚未配置目录；请先运行 setup --root 完整路径。")
    cfg = read_json(path)
    if cfg.get("config_version") != "1.0" or cfg.get("script_version") != VERSION:
        raise ToolError("VERSION_MISMATCH", "配置与脚本版本不一致，请使用配置中记录的启动入口。")
    return cfg


def run(command, code="TOOL_FAILED", timeout=120, env=None, log=True):
    """只按参数数组启动程序，不经过 shell。下载/识别日志去标准错误，JSON结果单独输出。"""
    try:
        completed = subprocess.run([str(x) for x in command], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(code, f"工具无法运行或超时：{command[0]}；{exc}") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout)[-6000:].strip()
        raise ToolError(code, f"工具返回失败，请检查网络、访问权限或工具路径。{detail}")
    if log and completed.stderr.strip():
        print(completed.stderr.strip(), file=sys.stderr)
    return completed.stdout


def probe(command, argument):
    if not command:
        return {"available": False, "version": None}
    try:
        output = run(command + [argument], timeout=20, log=False)
        return {"available": True, "version": output.splitlines()[0] if output.strip() else "已运行", "command": command}
    except ToolError as exc:
        return {"available": False, "version": None, "error": exc.message, "command": command}


def discover_tools(root, overrides=None):
    """优先使用明确指定或已安装的工具，再寻找 PATH；不会静默替用户升级已有工具。"""
    overrides = overrides or {}
    found = {}
    for name, executable in (("ffmpeg", "ffmpeg"), ("whisper", "whisper-cli"), ("yt_dlp", "yt-dlp")):
        explicit = overrides.get(name)
        if explicit:
            found[name] = [str(absolute(explicit))]
            continue
        suffix = ".exe" if os.name == "nt" else ""
        candidates = sorted((Path(root) / "tools").rglob(executable + suffix)) if (Path(root) / "tools").exists() else []
        external = shutil.which(executable)
        if candidates:
            found[name] = [str(candidates[-1])]
        elif external:
            found[name] = [str(Path(external).resolve())]
        elif name == "yt_dlp":
            if probe([sys.executable, "-m", "yt_dlp"], "--version")["available"]:
                found[name] = [sys.executable, "-m", "yt_dlp"]
    return found


def download(url, destination, sha256=None, expected_size=None):
    """下载到 .part，支持断点续传；大小和 SHA-256 都正确才成为可用文件。"""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and (not sha256 or digest(destination) == sha256):
        if not expected_size or destination.stat().st_size == expected_size:
            return destination
    part = destination.with_name(destination.name + ".part")
    options = settings()
    have = part.stat().st_size if part.exists() else 0
    if expected_size and shutil.disk_usage(destination.parent).free < max(0, expected_size - have) + options["reserve_disk_bytes"]:
        raise ToolError("INSUFFICIENT_SPACE", "目标磁盘空间不足，请改位置或选更小模型。")
    last = None
    for attempt in range(options["download_retries"]):
        try:
            have = part.stat().st_size if part.exists() else 0
            if not expected_size or have != expected_size:
                headers = {"User-Agent": "subtitle-translator/" + VERSION}
                if have:
                    headers["Range"] = f"bytes={have}-"
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=options["download_timeout_seconds"]) as response:
                    append = have > 0 and response.status == 206
                    if append and not response.headers.get("Content-Range", "").startswith(f"bytes {have}-"):
                        raise ValueError("服务器返回了不一致的续传位置")
                    with part.open("ab" if append else "wb") as handle:
                        shutil.copyfileobj(response, handle, length=1024 * 1024)
            if expected_size and part.stat().st_size != expected_size:
                raise ValueError("下载大小与固定清单不符")
            if sha256 and digest(part) != sha256:
                part.unlink(missing_ok=True)
                raise ValueError("SHA-256 校验不一致，已丢弃损坏文件")
            os.replace(part, destination)
            return destination
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last = exc
            print(f"下载未完成，第 {attempt + 1} 次：{exc}", file=sys.stderr)
    raise ToolError("DOWNLOAD_FAILED", f"下载失败，已保留可续传文件。请检查网络后重试：{last}")


def install_dependency(root, name):
    """安装经过固定校验的工具包。Windows提供CPU识别版；其他系统可传已有程序路径。"""
    item = manifest()[name]
    if name != "yt_dlp" and (os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64")):
        raise ToolError("TOOL_REQUIRED", "该系统请先安装 FFmpeg / whisper.cpp，再用 setup --ffmpeg / --whisper 指定程序。", {"source": "https://github.com/ggml-org/whisper.cpp/tree/v1.8.3"})
    spec = item["windows" if os.name == "nt" else "portable"]
    package = download(spec["url"], Path(root) / "cache" / spec["filename"], spec["sha256"], spec["size"])
    folder = Path(root) / "tools" / f"{name}-{item['version']}"
    folder.mkdir(parents=True, exist_ok=True)
    if package.suffix == ".zip":
        with zipfile.ZipFile(package) as archive:
            for member in archive.infolist():
                target = (folder / member.filename).resolve()
                if not target.is_relative_to(folder.resolve()) or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ToolError("DOWNLOAD_FAILED", "工具包中存在不安全路径，停止解压。")
            archive.extractall(folder)
        binary = "ffmpeg.exe" if name == "ffmpeg" else "whisper-cli.exe"
        matches = list(folder.rglob(binary))
        if not matches:
            raise ToolError("TOOL_REQUIRED", f"已解压工具但缺少 {binary}，请检查固定发行包。")
        return [str(matches[0])]
    final = folder / spec["filename"]
    shutil.copy2(package, final)
    final.chmod(final.stat().st_mode | 0o111)
    return [str(final)] if os.name == "nt" else [sys.executable, str(final)]


def setup(args):
    """首次运行时创建分类目录、复制固定脚本并验证下载工具；最后才保存正式配置。"""
    root = absolute(args.root)
    writable(root)
    cfg_path = Path(args.config)
    previous = read_json(cfg_path) if cfg_path.exists() else {}
    if previous and Path(previous["runtime_root"]).resolve() != root:
        raise ToolError("INVALID_INPUT", "已有配置指向另一目录；更换目录请运行 migrate --to。")
    with runtime_lock(root):
        for name in FOLDERS:
            (root / name).mkdir(exist_ok=True)
        target = root / "scripts" / VERSION
        target.mkdir(parents=True, exist_ok=True)
        for name in ("subtitle_translator.py", "settings.json", "dependencies.json", "README.md"):
            source = HERE / name
            if source.exists() and source.resolve() != (target / name).resolve():
                atomic_write(target / name, source.read_bytes())
        overrides = {"ffmpeg": args.ffmpeg, "whisper": args.whisper, "yt_dlp": args.yt_dlp}
        tools = {**previous.get("tools", {}), **discover_tools(root, overrides)}
        if not args.offline and not probe(tools.get("yt_dlp"), "--version")["available"]:
            tools["yt_dlp"] = install_dependency(root, "yt_dlp")
        cfg = {**previous, "config_version": "1.0", "runtime_root": str(root), "script_version": VERSION,
               "launcher": {"program": str(Path(sys.executable).resolve()), "args": [str(target / "subtitle_translator.py")]},
               "selected_model": previous.get("selected_model"), "output_root": previous.get("output_root", str(root / "outputs")),
               "tools": tools, "model_files": previous.get("model_files", {})}
        report = doctor_data(cfg)
        if not args.offline and not report["ready_for_captions"]:
            raise ToolError("TOOL_REQUIRED", "下载工具未通过检查，配置尚未切换。", report)
        atomic_write(cfg_path, cfg)
    return {"config_file": str(cfg_path.resolve()), **report, "launcher": cfg["launcher"], "offline": args.offline}


def doctor_data(cfg):
    """检查当前入口和目录；没有模型不影响有原字幕的路线。"""
    root = Path(cfg["runtime_root"])
    checks = {name: probe(cfg.get("tools", {}).get(name), "-h" if name == "whisper" else "-version" if name == "ffmpeg" else "--version") for name in ("yt_dlp", "ffmpeg", "whisper")}
    selected = cfg.get("selected_model")
    record = cfg.get("model_files", {}).get(selected, {})
    model_path = Path(record.get("path", "__missing__"))
    model_ok = model_path.is_file() and model_path.stat().st_size == record.get("size")
    directories = {name: (root / name).is_dir() for name in FOLDERS}
    launcher = cfg.get("launcher", {})
    entry_args = launcher.get("args", [])
    script_ready = bool(entry_args and Path(entry_args[0]).is_file() and Path(launcher.get("program", "__missing__")).is_file())
    if script_ready:
        try:
            script_ready = run([launcher["program"]] + entry_args + ["--version"], timeout=20, log=False).strip() == VERSION
        except ToolError:
            script_ready = False
    base_ready = script_ready and all(directories.values())
    return {"script_version": VERSION, "runtime_root": str(root), "directories": directories, "tools": checks,
            "script_ready": script_ready, "ready_for_captions": base_ready and checks["yt_dlp"]["available"],
            "ready_for_asr": base_ready and checks["ffmpeg"]["available"] and checks["whisper"]["available"] and model_ok,
            "selected_model": selected, "model_available": model_ok,
            "notes": ["未选择模型时不会下载权重；只有无字幕路线才需要模型。", "已有外部工具版本记录在 tools；不会在翻译时自动升级。"]}


def doctor(args):
    report = doctor_data(config(args))
    if not report["script_ready"] or not all(report["directories"].values()):
        raise ToolError("TOOL_REQUIRED", "脚本入口或目录缺失，请重新运行同版本 setup 修复。", report)
    return report


def install_tools(args):
    """用户需要联网或本地识别时补齐缺少的工具；只装程序，不替用户选择模型。"""
    cfg = config(args)
    names = ("yt_dlp", "ffmpeg", "whisper") if args.asr else ("yt_dlp",)
    for name in names:
        argument = "-h" if name == "whisper" else "-version" if name == "ffmpeg" else "--version"
        if not probe(cfg.get("tools", {}).get(name), argument)["available"]:
            cfg.setdefault("tools", {})[name] = install_dependency(cfg["runtime_root"], name)
        if not probe(cfg["tools"][name], argument)["available"]:
            raise ToolError("TOOL_REQUIRED", f"{name} 安装后仍无法运行，请检查运行库。")
    atomic_write(args.config, cfg)
    return doctor_data(cfg)


def youtube_url(url, fixture=False):
    """只接受YouTube视频；去掉播放列表和跟踪参数，任务始终对应一个视频。"""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
        raise ToolError("INVALID_INPUT", "请输入有效的 YouTube 视频网址。")
    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.strip("/")
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/shorts/", "/embed/")):
            video_id = parsed.path.split("/")[2]
        else:
            video_id = ""
    else:
        video_id = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) and not (fixture and video_id == "demoVideo01"):
        raise ToolError("INVALID_INPUT", "网址没有有效的视频 ID；第一版只处理 YouTube 普通录播。")
    return video_id, "https://www.youtube.com/watch?v=" + video_id


def language_code(code):
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", code):
        raise ToolError("INVALID_INPUT", "语言请使用 en、zh-Hans、ja 等代码。")
    return code


def ytdlp(cfg, extras, url, code="CAPTION_ACCESS_FAILED", timeout=180):
    tool = cfg.get("tools", {}).get("yt_dlp")
    if not tool:
        raise ToolError("TOOL_REQUIRED", "未找到 yt-dlp，请运行 install-tools。")
    flags = ["--ignore-config", "--no-playlist", "--no-update", "--socket-timeout", "30", "--retries", "2"]
    node = shutil.which("node")
    if node:
        flags.extend(["--js-runtimes", "node:" + node])
    return run(tool + flags + extras + ["--", url], code, timeout)


def caption_tracks(info):
    """排除URL里的tlang自动翻译轨道；原语言不明确时返回候选，交给用户/Skill判断。"""
    tracks = []
    for kind, key in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        for lang, entries in (info.get(key) or {}).items():
            native = [entry for entry in entries if entry.get("url") and not urllib.parse.parse_qs(urllib.parse.urlsplit(entry["url"]).query).get("tlang")]
            formats = [entry for entry in native if entry.get("ext") in ("json3", "vtt", "srt")]
            if formats and lang != "live_chat":
                tracks.append({"language": lang, "kind": kind, "name": formats[0].get("name", lang), "formats": sorted(set(x["ext"] for x in formats))})
    return tracks


def inspect_video(args):
    """只读视频元数据和字幕列表，为本次目标语言创建独立任务，不下载完整视频。"""
    cfg = config(args)
    video_id, url = youtube_url(args.url, bool(args.metadata))
    info = read_json(args.metadata) if args.metadata else json.loads(ytdlp(cfg, ["--dump-single-json", "--skip-download"], url))
    if info.get("id") != video_id:
        raise ToolError("INVALID_INPUT", "元数据中的视频 ID 与网址不一致。")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise ToolError("INVALID_INPUT", "第一版不处理直播或尚未归档的视频。")
    tracks = caption_tracks(info)
    source = args.source_lang or info.get("language")
    selected = None
    if source:
        matches = [x for x in tracks if x["language"].removesuffix("-orig") == source]
        if not matches:
            matches = [x for x in tracks if x["language"].split("-")[0] == source.split("-")[0]]
        selected = next((x for x in matches if x["kind"] == "manual"), matches[0] if matches else None)
    if not selected and not source:
        originals = [x for x in tracks if x["language"].endswith("-orig")]
        if originals:
            selected = originals[0]
            source = selected["language"].removesuffix("-orig")
    target = language_code(args.target_lang)
    job_id = f"{video_id}.{target}.{int(time.time())}-{uuid.uuid4().hex[:8]}"
    folder = Path(cfg["runtime_root"]) / "jobs" / job_id
    folder.mkdir(parents=True)
    job = {"job_id": job_id, "script_version": VERSION, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "video": {"platform": "youtube", "id": video_id, "url": url, "title": info.get("title") or video_id},
           "source_language": source or "und", "target_language": target, "duration": info.get("duration"),
           "caption_status": "available" if tracks else "none", "tracks": tracks, "selected_track": selected,
           "source_language_basis": "用户指定" if args.source_lang else "视频元数据或原始轨道标记" if source else "未知；需确认原语言轨道",
           "fixture": bool(args.metadata), "status": "inspected", "warnings": []}
    if not selected and tracks:
        job["warnings"].append("原语言轨道尚未确认；请查看 tracks 后用 fetch --language 指定原语言。")
    atomic_write(folder / "metadata.json", info)
    atomic_write(folder / "job.json", job)
    return {**job, "job_file": str(folder / "job.json")}


def load_job(cfg, job_id):
    """读取当前任务或兼容的旧任务；旧译文仍绑定原计划，不因升级而改掉时间。

0.1.0 的原文与译文格式可继续读取。要改善旧字幕的断句，用 resegment 建立修订副本。
"""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", job_id) or ".." in job_id:
        raise ToolError("INVALID_INPUT", "任务编号无效。")
    folder = Path(cfg["runtime_root"]) / "jobs" / job_id
    job = read_json(folder / "job.json")
    if job.get("script_version") not in {"0.1.0", VERSION} or job.get("job_id") != job_id:
        raise ToolError("VERSION_MISMATCH", "任务版本或编号不一致，请使用原版本脚本恢复。")
    return folder, job


def clean_text(value):
    """纯文本只整理空白：C++ 的 vector<T>、比较符号等都是原话，不能当标签删掉。"""
    text = str(value)
    return "\n".join(line.strip() for line in text.replace("\r", "").split("\n") if line.strip()).strip()


def caption_markup(value):
    """仅在VTT/SRT入口去掉已知显示标签，再解码一次实体；之后全部按纯文本处理。

先删真实标签、后解码，可保证 &lt;button&gt; 这样的讲代码字幕保留为 <button>。
未知尖括号内容也保留，避免“x < 3 > 2”或泛型参数丢失。
"""
    known = r"</?(?:b|i|u|ruby|rt|font)(?:\s+[^>]*)?>|</?(?:c|v|lang)(?:[.\s][^>]*)?>|<(?:\d+:)?\d{2}:\d{2}\.\d{3}>"
    return clean_text(html.unescape(re.sub(known, "", str(value), flags=re.I)))


def timestamp(value):
    values = value.replace(",", ".").split(":")
    if len(values) not in (2, 3):
        raise ToolError("INVALID_TIMELINE", f"无法读取时间：{value}")
    if not 0 <= float(values[-1]) < 60 or not 0 <= int(values[-2]) < 60:
        raise ToolError("INVALID_TIMELINE", f"分、秒必须小于60：{value}")
    number = 0.0
    for part in values:
        number = number * 60 + float(part)
    return round(number, 3)


def parse_subtitles(path):
    """支持 VTT、SRT、YouTube json3 和本工具原文JSON；保留实际词时间作为拆分依据。"""
    path = Path(path)
    if path.suffix.lower() == ".json" or path.suffix.lower() == ".json3":
        raw = read_json(path)
        if isinstance(raw, dict) and "cues" in raw:
            return raw["cues"]
        cues = []
        for event in raw.get("events", []):
            if not event.get("segs"):
                continue
            text = clean_text("".join(seg.get("utf8", "") for seg in event["segs"]))
            if not text:
                continue
            if not isinstance(event.get("tStartMs"), (int, float)) or not isinstance(event.get("dDurationMs"), (int, float)) or event["dDurationMs"] <= 0:
                raise ToolError("INVALID_TIMELINE", "JSON字幕含有文字却缺少有效时间，不能跳过该句。")
            start = event["tStartMs"] / 1000
            end = start + event["dDurationMs"] / 1000
            words = []
            for index, seg in enumerate(event["segs"]):
                if "tOffsetMs" in seg and seg.get("utf8", "").strip():
                    next_offset = next((x["tOffsetMs"] for x in event["segs"][index + 1:] if "tOffsetMs" in x), event["dDurationMs"])
                    words.append({"text": seg["utf8"], "start": round(start + seg["tOffsetMs"] / 1000, 3), "end": round(start + next_offset / 1000, 3)})
            cues.append({"start": round(start, 3), "end": round(end, 3), "source_text": text, "words": words,
                         "rolling": bool(event.get("aAppend") or words), "raw_event": len(cues)})
        return cues
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", raw):
        lines = block.splitlines()
        if not block.strip():
            continue
        first = lines[0].lstrip()
        if re.match(r"^(?:NOTE(?:\s|$)|STYLE\s*$|REGION\s*$)", first):
            continue
        matched = False
        for index, line in enumerate(lines):
            match = re.match(r"\s*((?:\d+:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+((?:\d+:)?\d{2}:\d{2}[.,]\d{3})", line)
            if match:
                matched = True
                body = "\n".join(lines[index + 1:])
                if re.search(r"^\s*(?:\d+:)?\d{2}:\d{2}[.,]\d+\s+[-=]+>", body, re.M):
                    raise ToolError("INVALID_TIMELINE", "字幕块之间缺少空行，无法可靠区分原文与时间，请修复输入文件。")
                text = caption_markup(body)
                if not text:
                    raise ToolError("INVALID_INPUT", "字幕中存在空白正文，不能静默丢弃该条。")
                cues.append({"start": timestamp(match[1]), "end": timestamp(match[2]), "source_text": text,
                             "rolling": bool(re.search(r"<\d{2}:\d{2}", body)), "raw_event": len(cues)})
                break
        if not matched and not re.match(r"^WEBVTT(?:\s|$)", first):
            raise ToolError("INVALID_TIMELINE", "存在无法解析的字幕块（可能时间箭头或格式损坏），不会跳过后导出不完整原文。", {"block_preview": block[:200]})
    return cues


def normalize_cues(cues, automatic=False):
    """只合并重叠的滚动展示。相邻的“很、很、很好”不去重，避免删掉说话者强调。

不同文字的普通字幕重叠不会擅自剪时间，而是留到检查报告中让用户校对。
"""
    result = []
    notes = []
    for index, original in enumerate(cues):
        cue = copy.deepcopy(original)
        if not isinstance(cue, dict) or not isinstance(cue.get("source_text"), str) or not cue["source_text"].strip():
            raise ToolError("INVALID_INPUT", f"第{index + 1}条原文为空或格式不正确，不能忽略这条后继续。")
        cue["source_text"] = clean_text(cue.get("source_text", ""))
        cue["raw_indices"] = [index]
        if result:
            prior = result[-1]
            overlap = isinstance(cue.get("start"), (int, float)) and isinstance(prior.get("end"), (int, float)) and cue["start"] < prior["end"]
            same_time = cue.get("start") == prior.get("start") and cue.get("end") == prior.get("end")
            rolling = automatic or cue.get("rolling") or prior.get("rolling")
            a, b = prior["source_text"], cue["source_text"]
            if overlap and (rolling or same_time):
                combined = None
                if a == b or a.startswith(b):
                    combined = a
                elif b.startswith(a):
                    combined = b
                else:
                    # 多行滚动字幕只去掉完整重复行，不能按几个字符猜测重复语句。
                    alines, blines = a.splitlines(), b.splitlines()
                    for count in range(min(len(alines), len(blines)), 0, -1):
                        if alines[-count:] == blines[:count]:
                            combined = "\n".join(alines + blines[count:])
                            break
                if combined is not None:
                    prior["source_text"] = combined
                    prior["end"] = max(prior["end"], cue["end"])
                    prior["raw_indices"].append(index)
                    prior.pop("words", None)  # 合并后旧词列表不再完整，不把它当作精确拆分依据。
                    notes.append("合并了重叠的滚动展示；原始文件保留供核对。")
                    continue
        result.append(cue)
    for index, cue in enumerate(result, 1):
        cue["id"] = f"cue-{index:06d}"
        cue.pop("rolling", None)
    return result, sorted(set(notes))


def save_source(folder, job, cues, source_kind, raw_path):
    """保存清理后的原文及来源；已有译文时拒绝换原文，避免旧译文套到不同台词。"""
    normalized, notes = normalize_cues(cues, source_kind == "automatic")
    if not normalized:
        raise ToolError("CAPTION_ACCESS_FAILED", "字幕文件中没有可用原文；请检查格式或换一个原语言轨道。")
    source = {"job_id": job["job_id"], "source_language": job["source_language"], "source_kind": source_kind,
              "raw_file": str(raw_path), "cues": normalized}
    # 原文一旦进入翻译就不能悄悄替换；要处理不同输入，应创建新的任务编号。
    path = folder / "source.json"
    if path.exists() and fingerprint(read_json(path)) != fingerprint(source) and (folder / "translations.json").exists():
        raise ToolError("SOURCE_CHANGED", "任务已经有译文，新的原文不同；请 inspect 创建新任务，原成果已保留。")
    atomic_write(path, source)
    job.update({"status": "source_ready", "source_hash": fingerprint(source), "source_kind": source_kind,
                "warnings": sorted(set(job.get("warnings", []) + notes))})
    atomic_write(folder / "job.json", job)
    return {"job_id": job["job_id"], "source_file": str(path), "cue_count": len(normalized), "warnings": job["warnings"]}


def fetch(args):
    """下载明确选中的原字幕，或导入用户给出的本地字幕；失败保持原任务与音频路线分开。"""
    cfg = config(args)
    folder, job = load_job(cfg, args.job)
    if args.language:
        job["source_language"] = language_code(args.language.removesuffix("-orig"))
    if args.file:
        source_path = Path(args.file).resolve()
        # 文件名包含内容摘要；用户误导入不同字幕时，不会先覆盖旧原始依据再报错。
        raw = folder / "raw" / ("original-" + digest(source_path) + source_path.suffix.lower())
        atomic_write(raw, source_path.read_bytes())
        return save_source(folder, job, parse_subtitles(raw), "local", raw)
    if job["caption_status"] == "none":
        raise ToolError("NO_CAPTIONS", "已确认没有字幕，请先选择本地识别模型，再运行 transcribe。")
    track = job.get("selected_track")
    if args.language:
        options = [x for x in job["tracks"] if x["language"] == args.language]
        track = next((x for x in options if x["kind"] == "manual"), options[0] if options else None)
    if not track:
        raise ToolError("SOURCE_LANGUAGE_REQUIRED", "请根据轨道列表确认原语言，并用 fetch --language 指定。", {"tracks": job["tracks"]})
    # 让 yt-dlp 重新取得短期有效地址；失败不改成“无字幕”，也不转去下载音频。
    destination = folder / "raw" / ("download-" + uuid.uuid4().hex)
    destination.mkdir(parents=True)
    flag = "--write-subs" if track["kind"] == "manual" else "--write-auto-subs"
    ytdlp(cfg, ["--skip-download", flag, "--sub-langs", "^" + re.escape(track["language"]) + "$", "--sub-format", "json3/vtt/srt", "-o", str(destination / "original.%(ext)s")], job["video"]["url"])
    files = [path for path in destination.iterdir() if path.suffix.lower() in (".json3", ".vtt", ".srt")]
    if not files:
        raise ToolError("CAPTION_ACCESS_FAILED", "字幕轨道存在但未能下载；请检查访问权限后重试，或导入本地原字幕。")
    received = max(files, key=lambda path: path.stat().st_mtime_ns)
    raw = folder / "raw" / ("original-" + digest(received) + received.suffix)
    atomic_write(raw, received.read_bytes())
    job["source_language"] = track["language"].removesuffix("-orig")
    job["warnings"] = [note for note in job["warnings"] if "原语言轨道尚未确认" not in note]
    job["selected_track"] = track
    job["source_language_basis"] = "已明确选择原语言轨道"
    return save_source(folder, job, parse_subtitles(raw), track["kind"], raw)


def hardware_report(root):
    """硬件读不到就返回 null（未知）；不会用猜测值替代显卡、显存或可用内存。"""
    root = absolute(root)
    existing = root
    while not existing.exists():
        existing = existing.parent
    data = {"cpu": platform.processor() or None, "cpu_cores": os.cpu_count(), "ram_total_bytes": None,
            "ram_available_bytes": None, "gpus": [], "disk_free_bytes": shutil.disk_usage(existing).free,
            "root": str(root), "asr_backend": "CPU（默认安装版）"}
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong), ("page_avail", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong), ("virtual_avail", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            data["ram_total_bytes"], data["ram_available_bytes"] = status.total, status.available
        try:
            raw = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-CimInstance Win32_VideoController | Select-Object Name | ConvertTo-Json -Compress"], timeout=15)
            devices = json.loads(raw)
            data["gpus"] = [{"name": item["Name"], "vram_bytes": None} for item in (devices if isinstance(devices, list) else [devices])]
        except (ToolError, ValueError, TypeError):
            pass
    elif Path("/proc/meminfo").exists():
        values = dict(re.findall(r"^(\w+):\s+(\d+)", Path("/proc/meminfo").read_text(), re.M))
        data["ram_total_bytes"] = int(values["MemTotal"]) * 1024
        data["ram_available_bytes"] = int(values.get("MemAvailable", "0")) * 1024 or None
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            output = run([nvidia, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], timeout=15)
            data["gpus"] = [{"name": line.rsplit(",", 1)[0].strip(), "vram_bytes": int(line.rsplit(",", 1)[1]) * 1048576} for line in output.splitlines() if "," in line]
        except (ToolError, ValueError):
            pass
    data["unknown_fields"] = [key for key, value in data.items() if value is None]
    return data


def models(args):
    """把四个候选模型和精确大小交给用户比较；根据CPU、可用内存和磁盘只给建议。"""
    cfg = config(args) if Path(args.config).exists() else None
    root = args.root or (cfg["runtime_root"] if cfg else str(Path.cwd()))
    hardware = read_json(args.hardware) if args.hardware else hardware_report(root)
    options = settings()
    thresholds = options["recommendations"]
    ram = (hardware.get("ram_available_bytes") or 0) / 1073741824
    cores = hardware.get("cpu_cores") or 0
    recommendation = "base"
    if ram >= thresholds["medium_ram_gib"] and cores >= thresholds["medium_cpu_cores"]:
        recommendation = "medium"
    elif ram >= thresholds["small_ram_gib"] and cores >= thresholds["small_cpu_cores"]:
        recommendation = "small"
    candidates = []
    for item in manifest()["models"]:
        item = dict(item)
        item["download_mb"] = round(item["size"] / 1000000, 2)
        item["disk_sufficient"] = (hardware.get("disk_free_bytes") or 0) >= item["size"] + options["reserve_disk_bytes"]
        item["url"] = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{manifest()['model_revision']}/{item['filename']}"
        candidates.append(item)
    recommended = next(x for x in candidates if x["id"] == recommendation)
    if not recommended["disk_sufficient"]:
        recommendation = next((x["id"] for x in candidates if x["disk_sufficient"]), None)
    return {"models": candidates, "recommended": recommendation, "selected_model": cfg.get("selected_model") if cfg else None,
            "reason": "结合当前可用内存、CPU线程数和磁盘空间的CPU版经验建议；显卡和显存已列出，但默认工具未启用GPU，不据此承诺加速。推荐不会触发下载。",
            "hardware": hardware, "model_revision": manifest()["model_revision"]}


def download_model(args):
    """调用方取得用户选择后才运行；只下载指定权重，校验成功后保存选择供下次复用。"""
    cfg = config(args)
    root = Path(cfg["runtime_root"])
    if args.root and absolute(args.root) != root:
        raise ToolError("INVALID_INPUT", "模型必须放在当前工具目录；更换目录请先 migrate。")
    item = next((x for x in manifest()["models"] if x["id"] == args.model), None)
    if not item:
        raise ToolError("INVALID_INPUT", "未知模型，请先运行 models 查看候选。")
    url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{manifest()['model_revision']}/{item['filename']}"
    target = root / "models" / item["filename"]
    reused = target.is_file() and target.stat().st_size == item["size"] and digest(target) == item["sha256"]
    path = download(url, target, item["sha256"], item["size"])
    cfg["selected_model"] = item["id"]
    cfg.setdefault("model_files", {})[item["id"]] = {**item, "path": str(path), "revision": manifest()["model_revision"], "verified_sha256": digest(path)}
    atomic_write(args.config, cfg)
    return {"selected_model": item["id"], "model_file": str(path), "sha256": item["sha256"], "reused": reused}


def whisper_cues(raw, offset=0.0):
    """whisper JSON 的 offsets 单位是毫秒，加回音频段起点后才是视频秒数。

本工具默认整段识别，不截掉片头/静音；此函数仍支持已有分段结果的绝对偏移。
"""
    cues = []
    for segment in raw.get("transcription", []):
        times = segment.get("offsets", {})
        if "from" not in times or "to" not in times:
            raise ToolError("INVALID_TIMELINE", "识别JSON缺少毫秒 offsets，不能猜测语音时间。")
        text = clean_text(segment.get("text", ""))
        if not text:
            continue
        words = []
        for token in segment.get("tokens", []):
            loc = token.get("offsets", {})
            if "from" in loc and "to" in loc and loc["from"] >= 0 and loc["to"] > loc["from"] and not token.get("text", "").startswith("[_"):
                words.append({"text": token["text"], "start": round(offset + loc["from"] / 1000, 3), "end": round(offset + loc["to"] / 1000, 3)})
        cues.append({"start": round(offset + times["from"] / 1000, 3), "end": round(offset + times["to"] / 1000, 3), "source_text": text, "words": words})
    return cues


def transcribe(args):
    """确认所选模型可用后，将完整音频交给本地识别工具，保存相对原视频的时间。"""
    cfg = config(args)
    folder, job = load_job(cfg, args.job)
    if job["caption_status"] != "none" and not args.audio:
        raise ToolError("CAPTION_ACCESS_FAILED", "该视频有字幕，请先修复字幕获取；不会静默改用语音识别。")
    selected = cfg.get("selected_model")
    record = cfg.get("model_files", {}).get(selected)
    if not record or not Path(record["path"]).is_file():
        raise ToolError("MODEL_REQUIRED", "本地识别需要用户先选择模型；请运行 hardware、models，确认后 download-model。")
    if digest(record["path"]) != record["sha256"]:
        raise ToolError("MODEL_REQUIRED", "模型校验失败，请重新下载所选模型。")
    tools = cfg.get("tools", {})
    if not tools.get("ffmpeg") or not tools.get("whisper"):
        raise ToolError("TOOL_REQUIRED", "缺少音频或识别工具，请运行 install-tools --asr；不会下载额外模型。")
    run_folder = folder / "asr" / ("run-" + uuid.uuid4().hex)
    run_folder.mkdir(parents=True)
    audio = Path(args.audio).resolve() if args.audio else None
    if audio is None:
        ytdlp(cfg, ["-f", "bestaudio/best", "--no-simulate", "-o", str(folder / "audio.%(ext)s")], job["video"]["url"], "DOWNLOAD_FAILED", timeout=7200)
        audio = next((p for p in folder.glob("audio.*") if p.suffix not in (".part", ".ytdl", ".wav")), None)
        if audio is None:
            raise ToolError("DOWNLOAD_FAILED", "音频下载后未找到完整文件，请重试。")
    # 16kHz 单声道只是改变采样格式，绝不裁剪或压缩静音，保持和原视频同一时间轴。
    # 每次识别使用独立目录；重跑失败或原文变化时不会覆盖旧任务的音频/识别依据。
    wav_path = run_folder / "audio.wav"
    temporary_wav = run_folder / "audio.converting.wav"
    run(tools["ffmpeg"] + ["-y", "-i", str(audio), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(temporary_wav)], timeout=7200)
    os.replace(temporary_wav, wav_path)
    prefix = run_folder / "recognition"
    language = job["source_language"].split("-")[0] if job["source_language"] != "und" else "auto"
    run(tools["whisper"] + ["-m", record["path"], "-f", str(wav_path), "-l", language, "-ojf", "-of", str(prefix), "-t", str(max(1, min(os.cpu_count() or 2, 8)))], timeout=86400)
    raw_path = prefix.with_suffix(".json")
    raw = read_json(raw_path)
    job["source_language"] = raw.get("result", {}).get("language", job["source_language"])
    job["warnings"].append("本地识别结果需对照音频检查人名、数字与低把握段落；未声称完成音频校准。")
    # 识别JSON通常带编译版本；复用外部程序而没有版本字段时明确记录未知，不能冒称固定发行版。
    actual_version = raw.get("systeminfo", {}).get("version") if isinstance(raw.get("systeminfo"), dict) else None
    known_install = Path(cfg["runtime_root"]) / "tools" / ("whisper-" + manifest()["whisper"]["version"])
    managed = Path(tools["whisper"][0]).resolve().is_relative_to(known_install.resolve())
    job["recognition"] = {"tool": "whisper.cpp", "version": actual_version or (manifest()["whisper"]["version"] if managed else None),
                          "version_basis": "识别结果" if actual_version else "已校验的固定发行包" if managed else "外部工具未报告版本，未知",
                          "command": tools["whisper"], "model": selected,
                          "audio": str(wav_path), "offset_seconds": 0, "timing": "完整原音频；未裁剪静音"}
    return save_source(folder, job, whisper_cues(raw), "asr", raw_path)


def timeline_errors(cues, duration=None):
    """所有时间都以视频开头为零；开始包含、结束不包含，首尾相接允许，重叠拒绝。"""
    errors, ids = [], set()
    previous_end = 0.0
    for index, cue in enumerate(cues):
        identity = cue.get("id")
        if not isinstance(identity, str) or not identity or identity in ids:
            errors.append(f"第{index + 1}条编号为空或重复")
        ids.add(identity if isinstance(identity, str) else str(identity))
        start, end = cue.get("start"), cue.get("end")
        valid = all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and abs(x * 1000 - round(x * 1000)) < 0.000001 for x in (start, end))
        if not valid or not 0 <= start < end:
            errors.append(f"{identity} 时间无效，应为0≤开始<结束且最多三位小数")
            continue
        if start < previous_end - 1e-9:
            errors.append(f"{identity} 与前条重叠或未按时间排序")
        if isinstance(duration, (int, float)) and end > duration + 0.001:
            errors.append(f"{identity} 超出视频时长")
        previous_end = end
        if not isinstance(cue.get("source_text"), str) or not cue["source_text"].strip():
            errors.append(f"{identity} 缺少原文")
    if not cues:
        errors.append("至少需要一条字幕")
    return errors


def normalize_words(text):
    return re.sub(r"\s+", "", text)


def source_slice(original, start, end):
    """按真实时间取出一段原文，供合并后重新断句时核对，绝不按中文字数分配秒数。

完整保留一条原字幕时直接使用它的文字。切进原字幕内部时，必须有完整、有序的
words（带时间的原词）列表；切点只能是下一个原词的开始。第一词前的原有留白仍保留。
"""
    if start == original["start"] and end == original["end"]:
        return original["source_text"]
    words = original.get("words", [])
    valid = isinstance(words, list) and bool(words)
    previous_start = -1.0
    if valid:
        for word in words:
            if not isinstance(word, dict) or not isinstance(word.get("text"), str) or not word["text"].strip():
                valid = False
                break
            times = [word.get("start"), word.get("end")]
            if not all(isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)
                       and abs(t * 1000 - round(t * 1000)) < 0.000001 for t in times):
                valid = False
                break
            if not (original["start"] <= times[0] < times[1] <= original["end"] and times[0] > previous_start):
                valid = False
                break
            previous_start = times[0]
    if not valid or normalize_words("".join(w["text"] for w in words)) != normalize_words(original["source_text"]):
        raise ToolError("INVALID_TIMELINE", f"{original['id']} 拆分缺少完整、有效的原词时间依据")
    boundaries = [original["start"]] + [word["start"] for word in words[1:]] + [original["end"]]
    if start not in boundaries or end not in boundaries or start >= end:
        raise ToolError("INVALID_TIMELINE", f"{original['id']} 切点不是原词边界，不能据字数猜时间")
    return "".join(word["text"] for word in words[boundaries.index(start):boundaries.index(end)])


def validate_plan(plan, source, job):
    """检查整句计划没有漏原文、重复原文或移动时间，支持跨原字幕合并后再按词时间分段。

逐条取出计划实际覆盖的原文，再检查每条原字幕是否从头到尾恰好覆盖一次。
这样既能把三条碎片合成一句，也能把较长的一句切在真实的词边界，而不是照搬旧切口。
"""
    if plan.get("job_id") != job["job_id"] or plan.get("source_hash") != fingerprint(source):
        raise ToolError("SOURCE_CHANGED", "断句计划不属于当前任务或原文已变化，请重新 prepare。")
    units = plan.get("cues")
    if not isinstance(units, list) or any(not isinstance(unit, dict) for unit in units):
        raise ToolError("INVALID_TIMELINE", "断句计划需要 cues 数组。")
    originals = {cue["id"]: cue for cue in source["cues"]}
    coverage = {key: [] for key in originals}
    errors = timeline_errors(source["cues"], job.get("duration")) + timeline_errors(units, job.get("duration"))
    if errors:
        raise ToolError("INVALID_TIMELINE", "原文或计划时间不合法，不能继续分段。", {"errors": errors})
    order = list(originals)
    positions_by_id = {key: index for index, key in enumerate(order)}
    max_gap = settings()["merge_max_gap_seconds"]
    for unit in units:
        ids = unit.get("source_ids", [])
        if (not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids)
                or len(ids) != len(set(ids)) or any(key not in originals for key in ids)):
            errors.append(f"{unit.get('id')} 原文对应编号无效")
            continue
        raw_units = [originals[key] for key in ids]
        positions = [positions_by_id[key] for key in ids]
        if positions != list(range(positions[0], positions[0] + len(ids))):
            errors.append("合并只能选原顺序中的连续原文")
        if not (raw_units[0]["start"] <= unit["start"] < raw_units[0]["end"]
                and raw_units[-1]["start"] < unit["end"] <= raw_units[-1]["end"]):
            errors.append(f"{unit['id']} 起止时间超出了对应原文，不能提前或延后整句")
            continue
        if len(ids) > 1:
            if unit.get("same_speaker_confirmed") is not True or unit.get("semantic_continuity_confirmed") is not True:
                errors.append("合并必须确认同一说话者和语义连续")
            speakers = {str(x.get("speaker_id", x.get("speaker"))) for x in raw_units
                        if x.get("speaker_id", x.get("speaker")) is not None}
            if len(speakers) > 1:
                errors.append("合并跨越已标注的不同说话者")
            if any(b["start"] - a["end"] > max_gap for a, b in zip(raw_units, raw_units[1:])):
                errors.append("合并跨越长停顿")
        pieces = []
        for original in raw_units:
            start, end = max(unit["start"], original["start"]), min(unit["end"], original["end"])
            coverage[original["id"]].append((start, end))
            try:
                pieces.append(source_slice(original, start, end))
            except ToolError as exc:
                errors.append(exc.message)
        if normalize_words(unit["source_text"]) != normalize_words("".join(pieces)):
            errors.append(f"{unit['id']} 文字不对应所覆盖的原文时间，或存在遗漏/重复")
    for key, mapped in coverage.items():
        original = originals[key]
        if not mapped:
            errors.append(f"遗漏原文 {key}")
        elif (mapped[0][0] != original["start"] or mapped[-1][1] != original["end"]
              or any(a[1] != b[0] for a, b in zip(mapped, mapped[1:]))):
            errors.append(f"{key} 必须完整、连续覆盖原时间，不能留下空洞或重复")
    if errors:
        raise ToolError("INVALID_TIMELINE", "时间/断句计划不通过；请保留原时间或补充可靠依据。", {"errors": errors})
    return units


def segmentation_status(plan, job):
    """告诉 Skill 是否还欠整句检查；旧任务继续可读，但明确标出它尚未走过新流程。"""
    if plan.get("segmentation_reviewed") is True:
        return "reviewed"
    return "required" if job.get("segmentation_policy") == "sentence-first" else "legacy"


def require_segmentation_review(plan, job):
    """新任务提交译文和导出前执行，防止直接把原字幕碎片逐条翻译就交付。"""
    if segmentation_status(plan, job) == "required":
        raise ToolError("SEGMENTATION_REVIEW_REQUIRED", "请先按完整句意复核分段，将计划中的 segmentation_reviewed 设为 true，再用 prepare --segmentation-plan 提交；原计划与译文均未覆盖。")


def reading_limits(language):
    """把 settings.json 的阅读建议交给 Skill；这些是提示线，不能据此机械截字或删信息。"""
    cfg = settings()
    limits = cfg["sentence_segmentation"]
    base = language.split("-")[0]
    return {"max_chars": limits["max_chars"].get(base, limits["max_chars"]["default"]),
            "max_seconds": limits["max_seconds"], "short_chars": limits["short_chars"].get(base, limits["short_chars"]["default"]),
            "short_seconds": limits["short_seconds"], "merge_max_gap_seconds": cfg["merge_max_gap_seconds"],
            "chars_per_second": cfg["reading_chars_per_second"].get(base, cfg["reading_chars_per_second"]["default"])}


def segmentation_issues(cues, language):
    """找出需要模型再看一眼的过长、过短和疑似断句位置，不自动合并，也不冒充语义判断。

例如前条没有句末标点，后条是“第二名”，且合起来放得下，就列出两条编号供复核。
完整的短回答也可能触发提示；由会话结合上下文判断，不能为了消除提示乱拼句子。
"""
    limits = reading_limits(language)
    issues = []
    for index, cue in enumerate(cues):
        text = cue["translated_text"]
        count = len(normalize_words(text))
        duration = cue["end"] - cue["start"]
        if count > limits["max_chars"] or duration > limits["max_seconds"]:
            issues.append({"code": "long_cue", "cue_ids": [cue["id"]], "message": "字幕较长；先保留整句，确实难读时再按完整分句和可靠时间拆分"})
        if count <= limits["short_chars"] or duration < limits["short_seconds"]:
            issues.append({"code": "short_cue", "cue_ids": [cue["id"]], "message": "字幕较短；检查是否把词组、句尾或一个完整意思切碎，独立短回答可保留"})
        if index == 0:
            continue
        previous = cues[index - 1]
        previous_text = previous["translated_text"].rstrip().rstrip('"\'”’」』）》)]}')
        span = cue["end"] - previous["start"]
        combined_count = len(normalize_words(previous["translated_text"])) + count
        if (not re.search(r"[。！？.!?…]$", previous_text)
                and 0 <= cue["start"] - previous["end"] <= limits["merge_max_gap_seconds"]
                and span <= limits["max_seconds"] and combined_count <= limits["max_chars"]):
            issues.append({"code": "possible_sentence_break", "cue_ids": [previous["id"], cue["id"]],
                           "message": "相邻字幕可能属于同一句，且合起来放得下；请检查完整意思和说话者"})
    return issues


def checked_source(folder, job):
    """恢复或导出前核对原文摘要；手工换原文后不能误用旧计划。"""
    source = read_json(folder / "source.json")
    if fingerprint(source) != job.get("source_hash") or source.get("job_id") != job["job_id"] or source.get("source_language") != job["source_language"]:
        raise ToolError("SOURCE_CHANGED", "原文或语言已发生变化，旧译文不能继续套用；请新建任务。")
    return source


def translations_state(folder, plan, job):
    """读取已完成译文，同时确认它们属于当前计划和语言；返回编号到文字的对应表。"""
    path = folder / "translations.json"
    if not path.exists():
        return {}
    saved = read_json(path)
    if saved.get("job_id") != job["job_id"] or saved.get("target_language") != job["target_language"] or saved.get("plan_hash") != fingerprint(plan):
        raise ToolError("SOURCE_CHANGED", "已保存译文与当前计划或目标语言不一致；请恢复原计划或创建新任务。")
    return {item["cue_id"]: item["translated_text"] for item in saved["items"]}


def prepare(args):
    """先生成待复核的原文草稿，再接收会话整理好的整句计划，最后产生对应翻译块。

首次草稿仍列出原始片段，方便追溯；它不代表最终断句。新任务要先完成整句复核才可提交译文。
已有译文的旧任务保留原计划和进度；需要改变边界时通过 resegment 新建修订任务。
"""
    cfg = config(args)
    folder, job = load_job(cfg, args.job)
    source = checked_source(folder, job)
    plan_path = folder / "plan.json"
    if args.segmentation_plan:
        plan = read_json(args.segmentation_plan)
    elif plan_path.exists():
        plan = read_json(plan_path)
    else:
        plan = {"job_id": job["job_id"], "source_hash": fingerprint(source), "segmentation_reviewed": False, "cues": [
            {key: cue[key] for key in ("id", "start", "end", "source_text")} | {"source_ids": [cue["id"]]} for cue in source["cues"]]}
    if not (folder / "translations.json").exists():
        job["segmentation_policy"] = "sentence-first"
    units = validate_plan(plan, source, job)
    if plan_path.exists() and fingerprint(plan) != fingerprint(read_json(plan_path)) and (folder / "translations.json").exists():
        raise ToolError("SOURCE_CHANGED", "已有译文时不能直接替换断句计划；请用 resegment --job 创建修订任务，原进度会保留。")
    done = translations_state(folder, plan, job)
    atomic_write(plan_path, plan)
    if not (folder / "context.json").exists():
        atomic_write(folder / "context.json", {"title": job["video"]["title"], "summary": "", "terminology": [], "review_notes": []})
    chunk_size = args.chunk_size or settings()["chunk_size"]
    if chunk_size < 1 or chunk_size > 500:
        raise ToolError("INVALID_INPUT", "每批条数应在1到500之间。")
    chunks = []
    margin = settings()["context_cues"]
    review_status = segmentation_status(plan, job)
    for index in range(0, len(units), chunk_size):
        selected = units[index:index + chunk_size]
        path = folder / "chunks" / f"chunk-{index // chunk_size + 1:04d}.json"
        data = {"job_id": job["job_id"], "target_language": job["target_language"], "source_language": job["source_language"],
                "context_before": units[max(0, index - margin):index], "items": selected,
                "context_after": units[index + chunk_size:index + chunk_size + margin],
                "translate_ids": [unit["id"] for unit in selected if unit["id"] not in done],
                "segmentation_status": review_status}
        atomic_write(path, data)
        chunks.append(str(path))
    if not done:
        job["status"] = "segmentation_pending" if review_status == "required" else "translating"
    atomic_write(folder / "job.json", job)
    return {"job_id": job["job_id"], "plan_file": str(plan_path), "context_file": str(folder / "context.json"),
            "source_file": str(folder / "source.json"), "segmentation_status": review_status,
            "reading_limits": reading_limits(job["target_language"]),
            "chunks": chunks, "missing_ids": [x["id"] for x in units if x["id"] not in done], "cue_count": len(units)}


def accept_translations(args):
    """把会话模型写出的译文按编号并入进度；先完整检查本批，任何错误都不覆盖旧内容。"""
    cfg = config(args)
    folder, job = load_job(cfg, args.job)
    source = checked_source(folder, job)
    plan = read_json(folder / "plan.json")
    units = validate_plan(plan, source, job)
    require_segmentation_review(plan, job)
    data = read_json(args.file)
    if data.get("job_id") != job["job_id"] or data.get("target_language") != job["target_language"]:
        raise ToolError("INVALID_TRANSLATION", "译文任务编号或目标语言不一致，未修改已有进度。")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ToolError("INVALID_TRANSLATION", "译文需要非空 items 数组。")
    allowed = {unit["id"] for unit in units}
    incoming, errors = {}, []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"cue_id", "translated_text"}:
            errors.append("每项只允许 cue_id 和 translated_text；时间变化必须走断句计划")
            continue
        cue_id, text = item["cue_id"], item["translated_text"]
        if not isinstance(cue_id, str) or cue_id not in allowed:
            errors.append(f"额外或无效编号：{cue_id}")
        elif cue_id in incoming:
            errors.append(f"重复编号：{cue_id}")
        elif not isinstance(text, str) or not text.strip() or "\x00" in text:
            errors.append(f"空译文或无效文字：{cue_id}")
        elif any(not line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")):
            errors.append(f"{cue_id} 译文中不能包含空白行，会破坏SRT块")
        else:
            incoming[cue_id] = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    prior = translations_state(folder, plan, job)
    merged = {**prior, **incoming}
    missing = sorted(allowed - merged.keys())
    if missing and not args.partial:
        errors.append("缺少编号：" + ", ".join(missing))
    if errors:
        raise ToolError("INVALID_TRANSLATION", "译文检查未通过，已有内容已保留。", {"errors": errors, "missing_ids": missing})
    saved = {"job_id": job["job_id"], "target_language": job["target_language"], "plan_hash": fingerprint(plan),
             "items": [{"cue_id": unit["id"], "translated_text": merged[unit["id"]]} for unit in units if unit["id"] in merged]}
    atomic_write(folder / "translations.json", saved)
    job["status"] = "translated" if not missing else "translating"
    atomic_write(folder / "job.json", job)
    return {"job_id": job["job_id"], "accepted_count": len(incoming), "completed_count": len(merged), "missing_ids": missing, "complete": not missing}


def validation(folder, job):
    """检查完整性并提示数字和阅读速度风险；提示不删信息，也不宣称已做语言校对。"""
    source = checked_source(folder, job)
    plan = read_json(folder / "plan.json")
    units = validate_plan(plan, source, job)
    require_segmentation_review(plan, job)
    translated = translations_state(folder, plan, job)
    errors, warnings = [], list(job.get("warnings", []))
    final = []
    limit = settings()["reading_chars_per_second"].get(job["target_language"].split("-")[0], settings()["reading_chars_per_second"]["default"])
    for unit in units:
        text = translated.get(unit["id"])
        if not isinstance(text, str) or not text.strip():
            errors.append(f"缺少译文：{unit['id']}")
            continue
        if any(not line.strip() for line in text.split("\n")):
            errors.append(f"译文含空白行：{unit['id']}")
        if len(text.replace("\n", "")) / (unit["end"] - unit["start"]) > limit:
            warnings.append(f"{unit['id']} 阅读速度超过参考值 {limit} 字符/秒，请精简表达或人工复核")
        if text.count("\n") > 1:
            warnings.append(f"{unit['id']} 超过两行，建议检查显示效果")
        # 数字不同只提示；“one→一”等自然表达不应被机械规则当成一定错误。
        if re.findall(r"\d+(?:[.,]\d+)*", unit["source_text"]) != re.findall(r"\d+(?:[.,]\d+)*", text):
            warnings.append(f"{unit['id']} 数字写法与原文不同，请核对数字、单位和条件")
        final.append({key: unit[key] for key in ("id", "start", "end", "source_text")} | {"translated_text": text})
    issues = segmentation_issues(final, job["target_language"])
    warnings.extend(f"{' / '.join(item['cue_ids'])}：{item['message']}" for item in issues)
    review_status = segmentation_status(plan, job)
    if review_status == "legacy":
        warnings.append("旧任务尚未按整句优先流程复核；返修断句时用 resegment 创建修订任务。")
    report = {"job_id": job["job_id"], "valid": not errors, "errors": errors, "warnings": sorted(set(warnings)),
              "segmentation_status": review_status, "segmentation_issues": issues,
              "cue_count": len(final), "expected_count": len(units), "linguistic_review": "语言质量需由当前会话对照原文复核；脚本只做结构和风险提示"}
    atomic_write(folder / "review.json", report)
    return report, final


def validate(args):
    """导出前运行，缺译文时返回失败和缺失列表；通过时仍列出需人工复核的提示。"""
    folder, job = load_job(config(args), args.job)
    report, _ = validation(folder, job)
    if not report["valid"]:
        raise ToolError("INVALID_TRANSLATION", "字幕尚不完整，不能作为完成结果导出。", report)
    return report


def srt_time(seconds):
    """把视频秒数转为SRT的时:分:秒,毫秒；与JSON使用同一时间，不另算分段。"""
    milliseconds = round(seconds * 1000)
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def commit_pair(json_path, json_data, srt_path, srt_data):
    """成对导出：先写两份临时文件，并记录备份；替换失败时恢复之前的两个成果。

文件系统不能同时替换两个独立文件；journal用于意外断电后下次export先恢复旧成果。
"""
    journal = json_path.with_suffix(".export-journal.json")
    if journal.exists():
        recover_pair(journal)
    records = []
    for path, raw in ((json_path, json_data), (srt_path, srt_data)):
        backup = path.with_name(path.name + ".previous")
        staged = path.with_name(path.name + ".pending")
        existed = path.exists()
        if existed:
            atomic_write(backup, path.read_bytes())
        atomic_write(staged, raw)
        records.append({"path": str(path), "backup": str(backup), "staged": str(staged), "existed": existed})
    atomic_write(journal, {"records": records})
    try:
        for record in records:
            os.replace(record["staged"], record["path"])
        journal.unlink()
    except OSError:
        recover_pair(journal)
        raise
    for record in records:
        Path(record["backup"]).unlink(missing_ok=True)


def recover_pair(journal):
    """只恢复与日志同一目录的文件，防止手工损坏日志后误覆盖其他位置。"""
    data = read_json(journal)
    root = journal.parent.resolve()
    for record in data["records"]:
        paths = [Path(record[key]).resolve() for key in ("path", "backup", "staged")]
        if any(path.parent != root for path in paths):
            raise ToolError("EXPORT_FAILED", "导出恢复日志路径不一致，请人工检查日志。")
        path, backup, staged = paths
        if record["existed"] and backup.exists():
            atomic_write(path, backup.read_bytes())
        elif not record["existed"]:
            path.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
    journal.unlink()


def export(args):
    """只导出完整通过结构检查的任务；JSON供插件切双语，SRT只写译文。"""
    cfg = config(args)
    folder, job = load_job(cfg, args.job)
    report, cues = validation(folder, job)
    if not report["valid"]:
        raise ToolError("INVALID_TRANSLATION", "仍有缺失译文，不会生成冒充完整成果的文件。", report)
    output = absolute(args.output) if args.output else Path(cfg["output_root"])
    writable(output)
    stem = f"{job['video']['id']}.{job['target_language']}"
    json_path, srt_path = output / (stem + ".subtitles.json"), output / (stem + ".srt")
    bundle = {"schema_version": "1.0", "video": job["video"], "languages": {"source": job["source_language"], "target": job["target_language"]}, "cues": cues}
    srt = "\n\n".join(f"{index}\n{srt_time(cue['start'])} --> {srt_time(cue['end'])}\n{cue['translated_text']}" for index, cue in enumerate(cues, 1)) + "\n"
    try:
        commit_pair(json_path, json_bytes(bundle), srt_path, srt.encode("utf-8"))
    except OSError as exc:
        raise ToolError("EXPORT_FAILED", f"导出失败，原成果已尽力恢复；请检查磁盘和文件占用：{exc}") from exc
    job.update({"status": "exported", "outputs": {"json_file": str(json_path), "srt_file": str(srt_path)}})
    atomic_write(folder / "job.json", job)
    return {"job_id": job["job_id"], "json_file": str(json_path), "srt_file": str(srt_path), "cue_count": len(cues), "warnings": report["warnings"]}


def resume(args):
    """中断后读取当前任务状态与缺失编号，不重新下载原文，也不重做已完成译文。"""
    folder, job = load_job(config(args), args.job)
    data = {**job, "job_directory": str(folder), "files": [str(path) for path in folder.iterdir() if path.is_file()]}
    if (folder / "source.json").exists():
        source = checked_source(folder, job)
        if (folder / "plan.json").exists():
            plan = read_json(folder / "plan.json")
            units = validate_plan(plan, source, job)
            translated = translations_state(folder, plan, job)
            data["missing_ids"] = [unit["id"] for unit in units if unit["id"] not in translated]
            data["completed_count"] = len(translated)
            data["plan_file"] = str(folder / "plan.json")
            data["segmentation_status"] = segmentation_status(plan, job)
    return data


def resegment(args):
    """从已保存原文建立断句修订任务，不重下视频、不删除旧译文，也不覆盖原来的导出文件。

新任务使用新的编号和计划；旧译文只作为措辞参考，不能把旧编号直接套到新句子。
原始字幕文件仍引用保留的旧任务原件；迁移目录时和其他原文路径一起更新。
"""
    cfg = config(args)
    folder, previous = load_job(cfg, args.job)
    source = checked_source(folder, previous)
    job_id = f"{previous['video']['id']}.{previous['target_language']}.{int(time.time())}-{uuid.uuid4().hex[:8]}"
    jobs_root = folder.parent.resolve()
    destination = (jobs_root / job_id).resolve()
    # 新目录名来自旧任务信息；先检查解析后的绝对路径，避免损坏的编号把整目录移到 jobs 外。
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+", job_id) or ".." in job_id
            or destination.parent != jobs_root):
        raise ToolError("INVALID_INPUT", "旧任务的视频或语言编号无效，不能创建断句修订目录。")
    job = copy.deepcopy(previous)
    job.pop("outputs", None)
    source = copy.deepcopy(source)
    source["job_id"] = job_id
    job.update({"job_id": job_id, "script_version": VERSION, "parent_job_id": previous["job_id"],
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": "source_ready",
                "source_hash": fingerprint(source), "segmentation_policy": "sentence-first"})
    # 先在 jobs 下面的独立临时目录写全材料，最后整体改名；失败只清理本次临时目录。
    with tempfile.TemporaryDirectory(prefix=".resegment-", dir=jobs_root) as staging:
        temporary = Path(staging)
        atomic_write(temporary / "source.json", source)
        atomic_write(temporary / "job.json", job)
        if (folder / "context.json").exists():
            shutil.copy2(folder / "context.json", temporary / "context.json")
        os.replace(temporary, destination)
    return {"job_id": job_id, "parent_job_id": previous["job_id"], "job_directory": str(destination),
            "source_file": str(destination / "source.json"), "previous_plan_file": str(folder / "plan.json"),
            "previous_translations_file": str(folder / "translations.json") if (folder / "translations.json").exists() else None,
            "next_step": "对新 job_id 运行 prepare，完成整句分段和对应译文后再导出。"}


def migrate(args):
    """先复制并验证新目录及所有任务关联，再切换配置；任何失败继续使用原目录。"""
    cfg = config(args)
    old, new = Path(cfg["runtime_root"]).resolve(), absolute(args.to)
    if old == new:
        return {"runtime_root": str(old), "changed": False}
    if new.is_relative_to(old) or old.is_relative_to(new):
        raise ToolError("INVALID_INPUT", "新旧目录不能互相嵌套，请选择独立目录。")
    if new.exists() and any(new.iterdir()):
        raise ToolError("INVALID_INPUT", "新目录非空，为避免覆盖无关文件，请选择空目录。")
    cfg_path = Path(args.config).resolve()
    if cfg_path.is_relative_to(old) or cfg_path.is_relative_to(new):
        raise ToolError("INVALID_INPUT", "配置必须在工具目录之外，迁移时才能稳定找到。")
    files = [path for path in old.rglob("*") if path.is_file() and path.name != ".runtime.lock"]
    if any(path.name.endswith(".export-journal.json") for path in files):
        raise ToolError("MIGRATE_FAILED", "发现未完成的导出恢复记录；请先在原目录重新 export 完成恢复，再迁移。")
    if any(path.is_symlink() for path in old.rglob("*")):
        raise ToolError("INVALID_INPUT", "目录含符号链接，请先明确其位置后再迁移。")
    needed = sum(path.stat().st_size for path in files)
    writable(new.parent)
    if shutil.disk_usage(new.parent).free < needed + settings()["reserve_disk_bytes"]:
        raise ToolError("INSUFFICIENT_SPACE", "新目录磁盘空间不够，原配置未变。")
    new.mkdir(parents=True, exist_ok=True)
    for name in FOLDERS:
        (new / name).mkdir(parents=True, exist_ok=True)
    for source in files:
        target = new / source.relative_to(old)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if digest(source) != digest(target):
            raise ToolError("MIGRATE_FAILED", "复制校验失败，仍使用原目录。")
    # 本项目不创建venv；外部Python继续复用，目录里的可执行工具路径则映射到新目录。
    def remap(value):
        if isinstance(value, dict):
            return {key: remap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [remap(item) for item in value]
        if isinstance(value, str):
            try:
                path = Path(value)
                if path.is_absolute() and path.is_relative_to(old):
                    return str(new / path.relative_to(old))
            except (ValueError, OSError):
                pass
        return value
    candidate = remap(cfg)
    # 更新迁移副本中的文件位置，并依次重算“原文→计划→译文”关联摘要。
    # 只改已约定的路径字段，原文和译文本身即使提到旧目录也不更改。
    for task_path in (new / "jobs").glob("*/job.json"):
        task_dir = task_path.parent
        task = read_json(task_path)
        if "outputs" in task:
            task["outputs"] = remap(task["outputs"])
        if "recognition" in task:
            task["recognition"] = remap(task["recognition"])
        source_path = task_dir / "source.json"
        if source_path.exists():
            source = read_json(source_path)
            if fingerprint(source) != task.get("source_hash"):
                raise ToolError("MIGRATE_FAILED", "原文摘要不一致，停止切换；原配置保持不变。")
            source["raw_file"] = remap(source.get("raw_file"))
            task["source_hash"] = fingerprint(source)
            atomic_write(source_path, source)
            plan_path = task_dir / "plan.json"
            if plan_path.exists():
                plan = read_json(plan_path)
                previous_plan_hash = fingerprint(plan)
                plan["source_hash"] = task["source_hash"]
                atomic_write(plan_path, plan)
                translations_path = task_dir / "translations.json"
                if translations_path.exists():
                    translations = read_json(translations_path)
                    if translations.get("plan_hash") != previous_plan_hash:
                        raise ToolError("MIGRATE_FAILED", "译文与原计划不一致，停止切换；原配置保持不变。")
                    translations["plan_hash"] = fingerprint(plan)
                    atomic_write(translations_path, translations)
        atomic_write(task_path, task)
    report = doctor_data(candidate)
    original_report = doctor_data(cfg)
    if not all(report["directories"].values()) or (original_report["script_ready"] and not report["script_ready"]) or any(original_report["tools"][name]["available"] and not report["tools"][name]["available"] for name in report["tools"]):
        raise ToolError("MIGRATE_FAILED", "新目录检查失败，原配置和成果均保留。", report)
    atomic_write(args.config, candidate)
    return {"runtime_root": str(new), "previous_root": str(old), "old_directory_retained": True, "changed": True, "files_verified": len(files)}


def jobs(args):
    """列出当前工具目录的历史任务，让会话找到同视频/同语言的可恢复进度。"""
    root = Path(config(args)["runtime_root"]) / "jobs"
    result = []
    for path in sorted(root.glob("*/job.json")):
        item = read_json(path)
        result.append({key: item.get(key) for key in ("job_id", "video", "target_language", "status", "created_at")})
    return {"jobs": result}


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolError("INVALID_INPUT", "命令参数有误：" + message)


def parser():
    """命令保持稳定；--config 和 --json 在子命令前后均可使用，方便 Skill 数组调用。"""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    main = JsonArgumentParser(description="字幕翻译配套脚本；用 --help 查看命令。", parents=[common])
    main.add_argument("--version", action="version", version=VERSION)
    sub = main.add_subparsers(dest="command", required=True)
    def command(name):
        return sub.add_parser(name, parents=[common])
    setup_parser = command("setup")
    setup_parser.add_argument("--root", "--runtime-root", required=True)
    setup_parser.add_argument("--offline", action="store_true")
    for option in ("ffmpeg", "whisper", "yt-dlp"):
        setup_parser.add_argument("--" + option)
    command("doctor")
    install = command("install-tools")
    install.add_argument("--asr", action="store_true")
    inspect = command("inspect")
    inspect.add_argument("--url", required=True)
    inspect.add_argument("--target-lang", required=True)
    inspect.add_argument("--source-lang")
    inspect.add_argument("--metadata", help="本地元数据恢复/样例入口，明确标为fixture")
    fetch_parser = command("fetch")
    fetch_parser.add_argument("--job", required=True)
    fetch_parser.add_argument("--file")
    fetch_parser.add_argument("--language")
    hardware = command("hardware")
    hardware.add_argument("--root")
    model_list = command("models")
    model_list.add_argument("--root")
    model_list.add_argument("--hardware")
    model_download = command("download-model")
    model_download.add_argument("--root")
    model_download.add_argument("--model", required=True)
    transcribe_parser = command("transcribe")
    transcribe_parser.add_argument("--job", required=True)
    transcribe_parser.add_argument("--audio", help="本地完整音频恢复输入；仍须选定模型")
    prepare_parser = command("prepare")
    prepare_parser.add_argument("--job", required=True)
    prepare_parser.add_argument("--segmentation-plan")
    prepare_parser.add_argument("--chunk-size", type=int)
    accept = command("accept-translations")
    accept.add_argument("--job", required=True)
    accept.add_argument("--file", required=True)
    accept.add_argument("--partial", action="store_true")
    for name in ("validate", "export", "resume", "resegment"):
        child = command(name)
        child.add_argument("--job", required=True)
        if name == "export":
            child.add_argument("--output")
    migration = command("migrate")
    migration.add_argument("--to", required=True)
    command("jobs")
    return main


def main(argv=None):
    """每次只返回一个JSON结果；失败退出码为1，调用方不可只靠文件存在判断完成。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raw = sys.argv[1:] if argv is None else argv
    machine = "--json" in raw
    try:
        args = parser().parse_args(raw)
        # 不在共享参数对象上设置默认值，否则子命令会覆盖写在它前面的 --config。
        if not hasattr(args, "config"):
            args.config = str(DEFAULT_CONFIG)
        if not hasattr(args, "json"):
            args.json = False
        functions = {"setup": setup, "doctor": doctor, "install-tools": install_tools,
                     "inspect": inspect_video, "fetch": fetch, "hardware": lambda a: hardware_report(a.root or config(a)["runtime_root"]),
                     "models": models, "download-model": download_model, "transcribe": transcribe, "prepare": prepare,
                     "accept-translations": accept_translations, "validate": validate, "export": export,
                     "migrate": migrate, "resume": resume, "resegment": resegment, "jobs": jobs}
        no_lock = {"setup", "doctor", "hardware", "models", "jobs", "resume"}
        if args.command in no_lock:
            data = functions[args.command](args)
        else:
            with runtime_lock(config(args)["runtime_root"]):
                data = functions[args.command](args)
        result = {"ok": True, "code": "OK", "message": "操作完成", "data": data}
        status = 0
    except ToolError as exc:
        result = {"ok": False, "code": exc.code, "message": exc.message, "data": exc.data}
        status = 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        result = {"ok": False, "code": "INVALID_INPUT", "message": f"操作未完成，请检查输入或目录权限：{exc}", "data": {}}
        status = 1
    except KeyboardInterrupt:
        result = {"ok": False, "code": "INTERRUPTED", "message": "操作已中断，已写入的进度保留，可用 resume 继续。", "data": {}}
        status = 130
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=None if machine else 2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
