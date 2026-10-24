# CAPABILITIES.md · 共享能力清单

> **写这份清单的唯一目的：让「已经有什么」可被查到**，从而不再新写一个同样的脚本。
> 规则：**只写签名与用途，不写实现**。新增或改参数时同步改本文件（否则 = 又一份会漂移的文档）。
> 当前共 **25 个可执行入口**。全部命令默认在 `~/app` 下执行，用的是 `~/app/.venv/bin/python`。

## 1. 运行与自检

| 名称 | 一句话用途 | 关键参数 | 调用示例 |
|---|---|---|---|
| `config.py` | 打印全部生效配置，不启动服务（口令类四键 `llm_api_key` / `hf_token` / `web_token` / `tenant_token_map` 一律打码成 `***`） | 无 | `.venv/bin/python config.py` |
| `server.py` | 实时转写 Web 服务（生产由 systemd 拉起，手工只用于调试）。**本体只做装配**：路由都在 `api/` 各组里 | `--host` `--port` `--warmup` | `.venv/bin/python server.py --port 8899` |
| `watcher.py` | 常驻监听 inbox → 转写 → 纪要 → 归档（生产由 systemd 拉起） | `--once` | `.venv/bin/python watcher.py --once` |
| `tests/run_all.py` | **全量回归唯一入口**：13 套件 + 基线比对 | `--only` `-q` `--json` `--fail-fast` | `.venv/bin/python tests/run_all.py --only kb` |
| `tools/check-style.sh` | 零依赖风格门禁（CRLF / 行尾空白 / 前导 Tab / final newline） | `--format` `--all` | `bash tools/check-style.sh` |
| `tools/check-dead-settings.py` | 查出 `Settings` 里声明了但没人读的字段（同时认 `.字段` 与 `getattr(s,"字段")` 两种形式） | `-v` `--json` | `.venv/bin/python tools/check-dead-settings.py -v` |
| `tools/repo_map.py` | 生成符号地图 `docs/REPO_MAP.md`：模块级函数/类/常量，**按被引用次数降序**，≤2k token | `--write` `--check` `--budget` | `.venv/bin/python tools/repo_map.py --write` |

## 2. 转写与分离

| 名称 | 一句话用途 | 关键参数 | 调用示例 |
|---|---|---|---|
| `pipeline.py` | 单文件流水线：校验 → 去重 → 转写 → 纪要 → 归档 | `--once` `--file` `--force` | `.venv/bin/python pipeline.py --file ~/audios_inbox/x.m4a` |
| `transcribe.py` | 本地 ASR + 说话人分离 | `--input` `--output-dir` `--backend` `--split-minutes` `--keep-wav` `--dual` `--force` | `.venv/bin/python transcribe.py --input a.m4a --dual` |
| `summarize.py` | 云端 LLM 生成结构化纪要（分块 + 成本记账） | `--transcript` `--all` `--output` `--dry-run` `--force` | `.venv/bin/python summarize.py --transcript t.json` |
| `diarize.py` | 说话人分离**算法库**（无 CLI，被 `transcribe.py` 调用） | — | — |

## 3. 声纹与音色

| 名称 | 一句话用途 | 关键参数 | 调用示例 |
|---|---|---|---|
| `speakers.py` | 声纹库**实现库**（多模板 × 信道 × 三档结论），被 `server` / `transcribe` / `tools/speaker_cli` 调用 | — | — |
| `tools/speaker_cli.py` | 声纹库**命令行**：看/确认/登记/问「这段像谁」/改名/删除 | `list` `show` `label` `enroll` `who` `rename` `forget` | `.venv/bin/python tools/speaker_cli.py show --dir processed/2026-01-01_电话录音` |
| `voice_corpus.py` | 音色语料池：攒「确定是你」的干净片段，重建参考音 | `collect` `list` `add` `build`（`--concat` 才拼接） | `.venv/bin/python voice_corpus.py build --profile 默认` |
| `voice_register.py` | 注册一个克隆音色（含 ASR 取文本、TTS 试听） | `--name` `--profile` `--audio` `--channel` `--owner` `--list` | `.venv/bin/python voice_register.py --name 张三 --audio a.wav` |
| `bootstrap.py` | **首次启动自检**：数据目录不能在仓库里（拒启动）、对外监听却没口令时自动生成 `WEB_TOKEN`、可选能力缺失只降级 | 被 `server.main()` 调用 | `from bootstrap import run; run(settings, "0.0.0.0")` |
| `tts_server.py` | CosyVoice2 合成服务（**必须用 `~/tts/venv`**，生产由 systemd 拉起） | `--voices` `--cache` `--host` `--port` `--fp16` `--warm` | `~/tts/venv/bin/python tts_server.py --port 9880` |

## 4. 成本

| 名称 | 一句话用途 | 关键参数 | 调用示例 |
|---|---|---|---|
| `costs.py` | 成本记账核心：用量落盘 + 双口径折算（`cost_yuan` 实付 / `fair_yuan` 折算） | `--ref` `--since` | `.venv/bin/python costs.py --ref M20260101-000000-cccc` |
| `cost_report.py` | 把 `usage.jsonl` 汇总成人看的表 | `--ref` `--since` `--days` `--top` `--markdown` `--json` | `.venv/bin/python cost_report.py --days 7 --markdown` |

⚠️ 查成本一律用 **`meeting_id`**（`M<日期>-<时间>-<sha前4位>`），不是目录名。参数名是 `--ref`。

## 5. 检索与脱敏

| 名称 | 一句话用途 | 关键参数 | 调用示例 |
|---|---|---|---|
| `kb_search.py` | 二元组 BM25 检索核心（`build` 建索引包 / `search` 查 / `stats` 看规模） | `search`: `--pack` `--top` `--scope` `--audience` `--json`；`build`: `--kb` `--out` `--redact` | `.venv/bin/python kb_search.py search --pack ~/data/kb/kb-pack.json --top 3 "定金不退"` |
| `kb_bridge.py` | 检索桥：懒加载 + 场景包 + 预算 700 字 + 异常全吞（被 `server.py` 调用） | — | — |
| `redact.py` | 字段级脱敏：`audit` 只统计 / `scan` 看命中 / `mask` 写脱敏副本 / `rules` 列规则 | `audit` `scan` `mask` `rules` | `.venv/bin/python redact.py scan --kb ~/personal-kb` |

⚠️ `kb_search.py` 与个人知识库工具**同源**（同一份代码用在两处），改一处必须两边同步。

## 6. 归档与运维

| 名称 | 一句话用途 | 关键参数 | 调用示例 |
|---|---|---|---|
| `deploy/refresh_archive.py` | 按归档里的音频**原地重跑**转写/纪要（不新建目录） | `--dir` `--no-report` | `.venv/bin/python deploy/refresh_archive.py --dir ~/data/tenants/me/processed/2026-01-01_xxx` |
| `deploy/retention.py` | 留存策略清理（**默认 dry-run**，`--apply` 才真删） | `--days` `--trash-days` `--apply` | `.venv/bin/python deploy/retention.py --days 90` |
| `deploy/backfill_meeting_id.py` | 给老归档补 `meeting_id`（可重复执行） | `--root` `--dry-run` | `.venv/bin/python deploy/backfill_meeting_id.py --dry-run` |
| `deploy/build_legal_pack.py` | 合并两份外部法律语料 → `legal-pack.json` | `--a-db` `--b-db` `--out` `--only-status` | `.venv/bin/python deploy/build_legal_pack.py --a-db A/laws.db --b-db B/law.db --out ~/data/kb/legal-pack.json` |
| `deploy/deploy.sh` | 服务器部署（六步 + 断言）：pull → **七位版本断言** → 装配 → 关键串 → **按 diff 判定重启范围** → 重启 → health。脏工作区/正在录音会拒绝执行 | `--dry-run` `--expect 串:文件` `--force` `--require-model` `--no-wait-model` `UNITS=web,watcher,tts` | `bash deploy/deploy.sh --expect '智能语音分身助理:web/index.html'` |
| `tools/sanitize-check.py` | **脱敏门禁**：真实姓名 / 用户名 / 内网 IP / 隧道域名 / 住址 / 案情不许进仓库。白名单按**字符串**放行（如 Android 包名），不按文件放行 | `--json` | `python3 tools/sanitize-check.py` |
| `deploy/install-units.sh` | **一键装 systemd user unit** 并断言 `%h` 展开正确（装成 system unit 会静默指错） | `--dry-run` `--enable` `--all` | `bash deploy/install-units.sh --enable` |
| `samples/make_samples.py` | **生成演示音频**：默认纯合成音（离线、零依赖、无授权问题），`--speech` 用 edge-tts 生成人声（不入库） | `--speech` | `python3 samples/make_samples.py` |
| `deploy/plan_units.sh` | 纯函数：按改动文件判定该重启哪些 unit（watcher/tts 是独立进程，只重启 web 会让它们跑旧代码且**不报错**）。被 `deploy.sh` source；判定表 = 各 unit 的 import 依赖闭包 | （被 source，无参数） | `. deploy/plan_units.sh && plan_units "$(git diff --name-only A B)" && echo "$need_watcher $need_tts"` |
| `deploy/fetch_legal.py` | 抓取/增补法律摘引原文到 `knowledge/legal/` | `--dry` `--add` `--out` | `.venv/bin/python deploy/fetch_legal.py --dry` |

## 7. 资源生成

| 名称 | 一句话用途 | 关键参数 | 调用示例 |
|---|---|---|---|
| `web/make_icons.py` | 生成 PWA 5 个图标（需要 Pillow，只有托管 venv 有） | 无 | `.venv/bin/python web/make_icons.py` |
| `android/make_launcher_icons.py` | 生成 Android 各密度 launcher 图标 | 无 | `.venv/bin/python android/make_launcher_icons.py` |
| `deploy/build_apk.sh` | 打 APK（不用 Gradle：aapt2 → javac → d8 → zipalign → apksigner） | 见脚本头 | `bash deploy/build_apk.sh` |

## 8. 什么时候该**新写**一个工具

三条同时成立才新写，否则先扩展现有成工具：

1. 现有工具**语义不同**（不是为了省一个参数）；
2. 现有工具无法通过加一个参数解决；
3. 新工具能被本表用**一行**说清（说不清 = 职责太杂）。

写完必须补进本表，否则下一个人（或下一个模型）会再写一遍。

## 9. 已知重复（见 `docs/重构审计-2026-10-19.md`）

- ~~`backfill_meeting_id.py` 根目录一份~~ → **已删**（O-07）。唯一入口是 `deploy/backfill_meeting_id.py`。
- ~~`test_assistant.py`（根）~~ → **已删**（O-07）。根版是 `tests/` 版的旧子集，唯一差异是旧格式汇总行；全量回归只认 `tests/test_assistant.py`。
- ~~`speaker.py`（CLI，324 行）与 `speakers.py`（库，529 行）**差一个字母**，极易误改~~ → **已改**（A-9）：CLI 移到 `tools/speaker_cli.py`，根目录不再有与库差一个字母的文件。
