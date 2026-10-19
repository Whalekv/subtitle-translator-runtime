# 字幕翻译配套脚本 0.2.0

它准备原文、管理任务、运行本地识别、验证译文并导出字幕；翻译由当前 Codex / Claude Code 会话模型完成，不需要另一个翻译 API。

- [下载 0.2.0 脚本 ZIP](https://github.com/Whalekv/subtitle-translator-runtime/releases/download/v0.2.0/subtitle-translator-runtime-0.2.0.zip) · [SHA-256 校验文件](https://github.com/Whalekv/subtitle-translator-runtime/releases/download/v0.2.0/subtitle-translator-runtime-0.2.0.zip.sha256)
- [版本发布说明](https://github.com/Whalekv/subtitle-translator-runtime/releases/tag/v0.2.0)
- [配套 Skill 与安装教程](https://github.com/Whalekv/subtitle-translator-skill)

普通用户先安装配套 Skill，首次使用时由它下载并校验这里的固定版本脚本包。手动安装时，将 ZIP 解压到一个独立文件夹，再在该文件夹中执行下面的命令。GitHub 自动生成的 Source code 压缩包用于查看源码，Skill 的 `--archive` 需要上面带校验值的专用脚本 ZIP。

需要 Python 3.11 或更高版本。Windows 10/11 x64 是优先平台；主程序只使用 Python 标准库。工具之间用参数数组调用，支持中文和空格路径。

## 第一次配置

先告诉用户目录会保存脚本、工具、所选模型、缓存、任务和输出，再取得用户选择的完整路径。开发验证应始终使用独立 `--config`，不会修改真正的用户配置。

```powershell
python subtitle_translator.py setup --root 'D:/字幕工具' --json
python subtitle_translator.py doctor --json
```

默认配置是当前用户主目录下 `.subtitle-translator/config.json`。`--config 'D:/测试/配置.json'` 可以放在子命令之前或之后。`setup --runtime-root` 是 `--root` 的别名。

配置中 `launcher` 的格式是 `{"program":"Python绝对路径","args":["脚本绝对路径"]}`。调用时把 program、args、子命令及选项组成数组，不能拼接为 shell 命令。配置还保存 `config_version`、`runtime_root`、`script_version`、`selected_model`、`model_files`、`output_root` 和 `tools`。

`setup` 复制本目录的四个运行文件到 `scripts/0.2.0/`，创建六个分类目录，优先复用已经存在的工具；缺少 yt-dlp 时下载并校验固定发行包。不创建虚拟环境，不更改系统 PATH，不下载语音识别模型。`--offline` 允许仅配置本地字幕导入路线，之后可用 `install-tools` 补齐下载工具。已有配置指向其他目录时必须使用 `migrate`；重复配置同一目录保留所选模型和任务。0.1.0 旧任务可以恢复，但不会自动改写旧字幕。

可以显式指定现有工具，例如：

```powershell
python subtitle_translator.py setup --root 'D:/字幕工具' --ffmpeg 'D:/FFmpeg/bin/ffmpeg.exe' --whisper 'D:/whisper/whisper-cli.exe' --yt-dlp 'D:/工具/yt-dlp.exe' --json
```

`doctor` 验证启动脚本、目录、工具和模型位置，给出 `script_ready`、`ready_for_captions` 和 `ready_for_asr`。未下载模型不会阻止有字幕路线；缺少主脚本或目录会返回失败。已有外部工具不会自动升级，报告会显示其实际自报版本；无法获知的版本明确标为未知。

## 命令约定

每条命令支持 `--json`。最终结果只写标准输出，工具日志写标准错误；失败退出码非零，格式固定：

```json
{"ok":true,"code":"OK","message":"操作完成","data":{}}
```

| 命令 | 选项 | 结果/作用 |
|---|---|---|
| `setup` | `--root PATH`，可选 `--offline`、`--ffmpeg PATH`、`--whisper PATH`、`--yt-dlp PATH` | 检查后写配置，不下载模型 |
| `doctor` | 无 | 脚本、目录、工具和模型可用性 |
| `install-tools` | 可选 `--asr` | 补齐 yt-dlp；加 `--asr` 时补齐 FFmpeg 与 whisper.cpp，不装模型 |
| `inspect` | `--url URL --target-lang CODE`，可选 `--source-lang CODE`、`--metadata FILE` | 新建任务，返回 `job_id`、视频、轨道、`caption_status` |
| `fetch` | `--job ID`，可选 `--language CODE`、`--file FILE` | 原字幕下载/本地导入，返回 `source_file`、条数 |
| `hardware` | 可选 `--root PATH` | CPU、线程数、内存、显卡、显存、磁盘，未知项为 null |
| `models` | 可选 `--root PATH`、`--hardware FILE` | 全部候选、精确下载大小、推荐与理由；不会选择或下载 |
| `download-model` | `--model base\|small\|medium\|large`，可选 `--root PATH` | 用户明确选择后才调用；校验后保存所选模型 |
| `transcribe` | `--job ID`，可选 `--audio FILE` | 使用已选模型识别；缺模型先返回 `MODEL_REQUIRED` |
| `prepare` | `--job ID`，可选 `--segmentation-plan FILE`、`--chunk-size N` | 原文/时间检查，保存计划、上下文与翻译块 |
| `accept-translations` | `--job ID --file FILE`，可选 `--partial` | 验证并合并译文；普通文件不能改时间 |
| `validate` | `--job ID` | 错误阻止导出，阅读速度/数字等给复核提示 |
| `export` | `--job ID`，可选 `--output DIR` | 同一份数据生成 JSON 和 SRT，返回两个绝对路径 |
| `resume` | `--job ID` | 检查原文/计划摘要、目标语言及版本，返回未完成编号 |
| `resegment` | `--job ID` | 从已保存原文创建断句修订任务；保留旧进度，不重新下载 |
| `jobs` | 无 | 当前目录中的任务列表 |
| `migrate` | `--to PATH` | 复制、校验并检查新目录后切换配置；保留旧目录 |

错误编号包括 `INVALID_INPUT`、`SETUP_REQUIRED`、`TOOL_REQUIRED`、`CAPTION_ACCESS_FAILED`、`SOURCE_LANGUAGE_REQUIRED`、`NO_CAPTIONS`、`DOWNLOAD_FAILED`、`MODEL_REQUIRED`、`INSUFFICIENT_SPACE`、`INVALID_TRANSLATION`、`INVALID_TIMELINE`、`SOURCE_CHANGED`、`VERSION_MISMATCH`、`EXPORT_FAILED`、`MIGRATE_FAILED` 和 `BUSY`。`message` 给下一步，`data.errors` / `missing_ids` 给具体问题。

## 有字幕的完整流程

```powershell
python subtitle_translator.py inspect --url 'https://www.youtube.com/watch?v=视频ID' --target-lang zh-Hans --json
python subtitle_translator.py fetch --job '上一步返回的job_id' --json
python subtitle_translator.py prepare --job '上一步返回的job_id' --json
```

`caption_status=none` 只表示元数据检查成功且无可用字幕。权限、超时、视频下架等失败统一保留真实错误，不当成无字幕。存在字幕但无法下载时不静默下载音频。只有原语言明确时才优先原语言人工字幕、再选择原语言自动字幕；原语言不明确会列出候选，需 `fetch --language en` 明确选择。带 `tlang` 的 YouTube 自动翻译轨道不会被选成原文。

`inspect --metadata FILE` 是明确标记为 `fixture=true` 的本地元数据入口。JSON 使用 yt-dlp 元数据字段，例如 `id`、`title`、`duration`、`language`、`subtitles`、`automatic_captions`。仅本地样例模式额外允许设计文档中的演示 ID `demoVideo01`；正常网络 URL 必须有真实格式的 11 位 ID。该入口不证明网络可用。

`fetch --file` 支持 `.vtt`、`.srt`、YouTube `.json3` 和含 `cues` 的原文 JSON。原始文件按内容摘要保存，不覆盖旧输入。相邻的重复台词保留，只处理重叠滚动展示；其他重叠会阻止准备/导出，须人工处理时间依据。

`source.json`：

```json
{
  "job_id":"实际任务编号",
  "source_language":"en",
  "source_kind":"manual",
  "raw_file":"原始输入的绝对路径",
  "cues":[{"id":"cue-000001","start":12.0,"end":16.0,"source_text":"Hello."}]
}
```

如果输入存在实际词/识别 token 的时间，原文单元还会有 `words:[{text,start,end}]`。无法取得词时间时不伪造，只能使用原单元已有边界合并或保留分段。

`prepare` 返回 `plan_file`、`source_file`、`context_file`、`chunks`（文件路径数组）、`missing_ids`、`cue_count`、`segmentation_status` 和 `reading_limits`。首次计划是待审草稿；会话要先结合上下文拟译，以完整句意和阅读时长决定分段。将复核后的计划副本设为 `segmentation_reviewed:true`，再 `prepare --segmentation-plan <副本路径>`。未完成时状态为 `required`，提交译文与导出会报 `SEGMENTATION_REVIEW_REQUIRED`；确认后为 `reviewed`。旧任务的 `legacy` 状态不等于完成了整句审查。

`context.json` 保存 `title`、`summary`、`terminology`、`review_notes`，可供会话模型逐步补充；重复 prepare 不覆盖它。每个块中 `items` 是本批计划单元，`translate_ids` 指定尚未翻译的编号，`context_before` / `context_after` 只供理解，不重复进入输出。批次边界不是字幕断句边界，长片先完成全片断句及接缝复核，再分批保存译文。

会话模型写出以下文件，不能自行改时间：

```json
{
  "job_id":"实际任务编号",
  "target_language":"zh-Hans",
  "items":[{"cue_id":"cue-000001","translated_text":"你好。"}]
}
```

```powershell
python subtitle_translator.py accept-translations --job '任务编号' --file 'D:/本批译文.json' --partial --json
python subtitle_translator.py resume --job '任务编号' --json
python subtitle_translator.py validate --job '任务编号' --json
python subtitle_translator.py export --job '任务编号' --json
```

分批导入用 `--partial`，保存已有译文并返回缺失编号。最后一批可不带 `--partial`，此时检查“已有+本批”的总完整性。重复、额外、遗漏编号、错误语言、空译文和空白行会被拒绝；一次无效导入不会损坏先前进度。可以用相同编号重交修订译文。

只有完整通过检查后才导出 `视频ID.目标语言.subtitles.json` 与同名 `.srt`。SRT只含译文，秒数转毫秒后与JSON完全一致；两份文件均从同一份数据生成。写入失败回滚旧成果；异常中断留下的成对导出日志会在下次 export 时恢复。跨两个独立文件不存在真正的文件系统原子替换，极端断电后需再运行 export 完成恢复。

## 没有字幕时

先说明本地识别用途，再依次 `hardware`、`models`，让用户从全部候选中选择。推荐不是授权，不能自动执行 `download-model`。

| 模型 ID | 固定权重 | 下载字节数 | 十进制 MB |
|---|---|---:|---:|
| base | ggml-base.bin，多语言 | 147951465 | 147.95 |
| small | ggml-small.bin，多语言 | 487601967 | 487.60 |
| medium | ggml-medium.bin，多语言 | 1533763059 | 1533.76 |
| large | ggml-large-v3.bin，多语言 | 3095033483 | 3095.03 |

固定 Hugging Face 提交为 `5359861c739e955e79d9a303bcbc70fb988958b1`，各文件 SHA-256 在 `dependencies.json`。下载保存 `.part`，支持续传；校验失败的权重不启用。模型选择只在下载与校验完成后写入配置。

```powershell
# 只在用户已明确选择 small 后执行。
python subtitle_translator.py install-tools --asr --json
python subtitle_translator.py download-model --model small --json
python subtitle_translator.py transcribe --job '任务编号' --json
python subtitle_translator.py prepare --job '任务编号' --json
```

Windows x64 缺工具时安装固定 FFmpeg 发行包及 whisper.cpp 1.8.3 CPU 版。已可用 FFmpeg 和 whisper-cli 会复用。其他系统使用已安装工具路径；whisper.cpp 可按官方 1.8.3 源码说明编译，运行 `cmake -B build`、`cmake --build build --config Release` 后把 whisper-cli 路径传给 setup。程序不会擅自安装系统包管理器或 GPU 驱动。

识别会取得完整音频，转换为 16kHz 单声道 WAV，绝不删除片头、广告或静音，再运行 whisper-cli `-ojf` 输出带时间原文 JSON。采用整段处理；未实现自动切长音频，因此没有人为接段边界。解析函数支持输入分段结果的绝对偏移并有测试，但CLI不把该单测宣传成真实分段识别验收。`--audio` 可以提供用户已有的完整音频作为恢复输入；应与原视频从零开始的时间一致。

YouTube 有时还需要 JavaScript 运行环境；若系统有 Node，脚本会显式提供给 yt-dlp。固定 Windows yt-dlp 二进制包含其打包依赖；复用 Python 版时应遵循其官方 EJS 文档补全环境。若网站接口或访问限制导致失败，先报告错误并使用用户取得的本地字幕/音频恢复，不在日常命令中自动追踪最新工具。

## 断句计划与时间

首次待审 `plan.json`（实际复核后才把布尔值改为 `true`）：

```json
{
  "job_id":"实际任务编号",
  "source_hash":"由prepare生成的原文摘要",
  "segmentation_reviewed":false,
  "cues":[{
    "id":"cue-000001","source_ids":["cue-000001"],
    "start":12.0,"end":16.0,"source_text":"Hello."
  }]
}
```

修改计划后执行 `prepare --segmentation-plan FILE`。逐条单元保留原文与起止时间；合并仅允许连续原单元，明确填写 `same_speaker_confirmed:true` 和 `semantic_continuity_confirmed:true`，且停顿不超过设置阈值，原文和整体范围必须完整保留。不要把这些确认字段当成自动判断。

拆分仅接受完整有效的 `words` 的实际时间边界，支持跨多个原单元组合后再分段。按每段覆盖的词核对文本，要求连续覆盖全部原时间、全文无遗漏或重复。当前未提供独立音频停顿证据入口：没有原词时间时使用原片段边界或记录复核，不能按译文字数均分。一个字幕内换行无需改时间。

已有译文的任务不能替换分段计划。要重做分段，用 `resegment --job 原编号` 从已保存原文创建修订任务，不再下载视频。对返回的新编号执行 `prepare`，参考旧译文措辞，按新计划重新对应译文；旧任务可继续查看。修订任务导出时使用同名输出文件，如需并排比较，在 `export` 中指定另一个 `--output` 目录。

`validate` 会在 `segmentation_issues` 标记过长、过短和可能属于同一句的相邻字幕，附带编号和理由。会话负责逐项复核；完整短回答可保留，疑似碎片不能只补句号消除提示。程序不会自动把提示当作语言错误或擅自合并。

## 迁移与常调参数

`migrate --to 完整路径` 检查磁盘、新旧嵌套、非空冲突和正在运行的任务；复制后逐文件校验，改写新副本中的原始输入/音频/输出位置并同步更新原文、计划和译文摘要，然后检查工具入口，最后原子切换配置。任何失败保留原配置。旧目录始终保留，清理是另外一项由用户决定的操作。默认配置必须放在工具目录之外。

`settings.json` 集中提供下载超时/次数、磁盘预留、每批条数、上下文条数、合并停顿上限、阅读速度和CPU模型推荐阈值，每个区域有中文说明。新增 `sentence_segmentation` 控制长短提示：默认中文一条总长参考 44 字符、持续时间参考 8 秒；调大减少长句提示，不会自动改变分段。调大每批条数会增大单次翻译输入；调大阅读速度只减少复核提示，不改变字幕内容。脚本不会把机械检查当成语言复核已完成。

## 验证与实际边界

```powershell
python -m unittest discover -s . -p test_runtime.py -v
```

30 项离线测试已通过。除原有完整性、恢复、目录迁移和成对导出等检查，新增验证了：未审查断句不能提交；三片段合成一句且两种导出时间一致；跨原片段合并后按真实词时间切分；没有可靠词时间、原文缺失、时间空洞、跨长停顿或说话者时拒绝；断句提示不改字幕；修订任务保留旧成果；0.1.0 任务兼容。另通过真实 CLI 与插件校验器的离线交接。语义判断和真实视频观看仍需实际会话与播放复核。

本次已真实取得 YouTube `jNQXAC9IVRw` 的元数据与6条英文人工字幕；原语言未知时先返回候选，再明确选择 `en`。另一个已不可用视频正确返回 `CAPTION_ACCESS_FAILED`。还在隔离目录下载并校验了 whisper.cpp 1.8.3 的约4MB程序包，确认 `whisper-cli -h` 可运行，同时复用现有 FFmpeg；所选模型仍为空。真实结果不代表所有地区、登录状态或视频均可访问。没有下载语音识别模型，没有做真实模型识别、GPU速度/显存或完整Chrome观看验收。开发临时任务和模型均不属于发布包。

固定工具下载信息来自各项目官方发行API与模型库；没有手填猜测校验值：

- [yt-dlp 字幕选项与安装](https://github.com/yt-dlp/yt-dlp#subtitle-options)
- [yt-dlp 2026.08.19 固定发行](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19)
- [YouTube EJS 环境说明](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [whisper.cpp 1.8.3 源码和运行说明](https://github.com/ggml-org/whisper.cpp/tree/v1.8.3)
- [whisper.cpp 1.8.3 CLI 参数与 JSON 实现](https://github.com/ggml-org/whisper.cpp/blob/v1.8.3/examples/cli/cli.cpp)
- [固定模型提交与文件](https://huggingface.co/ggerganov/whisper.cpp/tree/5359861c739e955e79d9a303bcbc70fb988958b1)
- [固定 FFmpeg Windows 发行](https://github.com/yt-dlp/FFmpeg-Builds/releases/tag/autobuild-2026-10-17-20-33)

各外部工具和模型保留自身许可；发布本脚本包时不捆绑模型或这些工具的二进制。若另行再分发工具，需一并遵守它们发行包中的许可要求。
