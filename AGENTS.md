# AGENTS.md · meeting-scribe 根规约

> 适用范围：本仓全目录。**本文件只写四件事：怎么跑 / 目录是什么 / 什么不能改 / 怎么提交。**
> 不写「请保持代码整洁」这类愿望，只写可执行的判据。
> 配套：`CAPABILITIES.md`（有哪些现成工具，先查再写）、`docs/adr/`（为什么这么定）。

## 1. 先知道一件事：服务只能在 Linux 跑

代码依赖 `systemd --user`、`pwd` 语义、`ffmpeg`，以及 Linux 下的音频设备模型。
**服务本身在 Windows / macOS 跑不起来**（不是没适配，是设计如此 —— 它靠 systemd 管进程、
靠 `%h` 展开路径、靠 ALSA 抓双声道）。

所以典型用法是：Linux 机器上跑服务，任意平台编辑代码、push，Linux 端 pull + 重启。
纯逻辑测试（检索、脱敏、成本、解析）在 Windows / macOS 也能跑，
但**涉及 ASR、流式、双声道、声纹的改动必须在 Linux 上再验一次**。

> 下文用 `$APP` 指代码目录（systemd 单元里写作 `%h/app`），`$DATA` 指数据根（默认 `~/data`）。
> 两者刻意分开：数据不进仓库，否则 `git clean -fdx` 会连录音一起删掉。

## 2. 构建 / 测试 / 冒烟（可直接复制）

```bash
# ① 全量回归：350 项，13 个套件。改任何 .py 之后必跑
cd ~/app && .venv/bin/python tests/run_all.py
#    期望末行：通过 350 项，失败 0 项 / 结论：全部通过
#    少于基线也会判失败（专抓「断言被跳过但仍 0 失败」的假绿）

# ② 单套件（调试时用，比全量快）
cd ~/app && .venv/bin/python tests/test_kb_search.py

# ③ 配置自检：打印全部 72 个生效配置，不启动服务
cd ~/app && .venv/bin/python config.py

# ④ 冒烟：服务在跑 + 接口可用（令牌从 .env 读，别手打）
cd ~/app && systemctl --user is-active meeting-web && \
  curl -s -o /dev/null -w 'health:%{http_code}\n' http://127.0.0.1:8848/api/health

# ⑤ 风格门禁（hook 会自动跑；手动预览用这个）
bash tools/check-style.sh

# ⑥ 死声明检查：Settings 里声明了但没人读的字段
.venv/bin/python tools/check-dead-settings.py -v
```

### Windows 本地能跑多少测试（不必上服务器）

纯逻辑套件在 Windows 直接可跑，**291 / 350 项**：

```bash
# 前提：本地装 pyyaml（否则 test_scenario 会退化成测内置兜底而报 1 项失败）
pip install --target /tmp/pylibs pyyaml
PYTHONPATH=/tmp/pylibs python tests/run_all.py
#    期望：通过 291 项，失败 0 项；4 个套件 no-summary（缺 funasr/torch）属正常
```

跑不了的 4 个套件（共 59 项）需要 GPU 环境：`test_live_echo` `test_noise` `test_dual` `test_speakers`。
其余 9 个套件（assistant / costs / prompter / voice_isolation / kb_search / redact / scenario / legal_pack / wake）
本地全绿即可放心提交，**但涉及 ASR、流式、双声道、声纹的改动必须上服务器再验一次**。

**冒烟最小集 = ① + ④。这两条不过不要往下做。**

改完 `server.py` / `kb_bridge.py` / `kb_search.py` **必须重启**：
`systemctl --user restart meeting-web`（Python 是常驻进程，不重启等于没改）。

## 3. 目录地图

| 路径 | 是什么 | 什么时候进来 |
|---|---|---|
| `server.py` | FastAPI 单体：`create_app()` 内 27 个路由 + 实时会话状态机 | 改接口 / 实时链路 |
| `pipeline.py` | 离线管线：转写 → 分离 → 纪要 → 归档 | 改离线流程 |
| `transcribe.py` | FunASR 转写 + 说话人分离调度 | 改 ASR 参数 |
| `diarize.py` | 自研说话人分离（能量窗 + 聚类 + 合并） | 改分离质量 |
| `speakers.py` | 声纹库**实现**（多模板 × 信道 × 三档结论） | 改声纹匹配 |
| `tools/speaker_cli.py` | 声纹库**命令行**（show/label/enroll/who） | 人工确认身份 |
| `tts_server.py` | CosyVoice2 合成服务（独立进程、独立 venv） | 改合成 |
| `voice_corpus.py` / `voice_register.py` | 音色语料构建 / 注册 | 加音色 |
| `summarize.py` | DeepSeek 纪要（分块 + 成本记账） | 改纪要 |
| `costs.py` / `cost_report.py` | 成本双口径（`cost_yuan` 实付 / `fair_yuan` 折算）+ 报表 | 改计价 |
| `kb_search.py` | 二元组 BM25 检索核心（**本机 personal-kb 同一份，改一处两边同步**） | 改检索算法 |
| `kb_bridge.py` | 检索桥：懒加载 + 预算裁剪 + 异常全吞 | 改供给策略 |
| `redact.py` | 字段级脱敏（构建期跑，线上不留明文） | 改脱敏规则 |
| `prompter.py` | 提词器（便签式要点，不进 TTS） | 改提词 |
| `llm_errors.py` | LLM 错误分类表：英文原文 → `kind` + 一句人话 + 一条建议（界面靠它显示红色提醒） | 加/改错误分类 |
| `config.py` | **配置唯一入口**：71 个环境变量 → `SETTINGS` dataclass | 加配置项 |
| `web/app.js` | 前端单文件 IIFE（99 个函数，2193 行） | 改界面 |
| `deploy/` | systemd unit、归档同步、APK 构建、法律包构建 | 改部署 |
| `tests/` | 13 套件 + `run_all.py` | 加测试 |
| `docs/` | 专题文档（`docs/adr/` 是决策记录） | 判断「为什么」 |
| `knowledge/legal/` | 法律摘引原文（527 个文件），`legal-pack.json` 的上游 | 只读，不要改 |

## 4. 禁改区（改了必须评审 / 必须先备份）

- 🔴 **`~/data/`**：运行数据，不在仓库里。尤其 `tenants/*/speakers.json`（声纹库，丢了无法重建）。
- 🔴 **`~/tts/voices/`**：克隆音色。`voice_corpus.py build` 会**整个覆写 `meta.json`**，必须沿用旧 `owner`。
- 🔴 **`~/app/.env`**：口令与密钥。**零明文凭据进仓库**；新增配置项要同步 `.env.example`。
- 🔴 **`~/build/keystore/meeting.jks`**：APK 签名。丢了老用户无法升级。
- 🔴 **`~/data/kb/legal-pack.json`**：48,620 条。重建必须与 `kb-pack.json` **一起**重建（补 `scopes:["legal"]`）。
- 🟡 **`~/data/tenants/*/processed/`**：归档成品。改结构要能回滚，且要先跑 `deploy/refresh_archive.py --dir <归档>` 验证。
- 🟡 **`.env.example` 与 `config.py` 必须同构**：加一项配置两边都改（当前差 2 项，见审计报告）。

## 5. 提交规范

- **一个提交 = 一个决策**；格式化与逻辑改动**必须分两次提交**。
- 信息格式 `<type>(<范围>): <做了什么>`，类型用 `feat` / `fix` / `refactor` / `docs` / `test` / `chore`。
- 改了 `.py`，**提交信息里必须贴 `run_all.py` 的结果行**。
- 行尾 **LF**；`core.autocrlf=true` + `.gitattributes(* text=auto eol=lf)` 共存，属性优先。
- 推送前 `git pull --ff-only`；两端都设了 upstream。
- hook 门禁：`git config core.hooksPath .githooks`（每个工作区各设一次）。

## 6. 五个最容易踩的坑

1. **两个 venv 不可合并**：`~/app/.venv` 装 funasr，`~/tts/venv` 装 CosyVoice2。合成必须用 `~/tts/venv`。
2. **测「缺包降级」必须隔离全部语料变量**（`KB_PACK` **和** `KB_LEGAL_PACK`）。只设其一 = 读线上真包 = 假绿。
3. **`kb_bridge.warm()` 靠启动钩子预热**，改检索代码后不重启 = 跑的是旧索引。
4. **`cost_report.py` 的参数是 `--ref` 不是 `--meeting`**；查成本一律用 `meeting_id`，不是目录名。
5. **命令行脚本用 `cli_settings()`**，别用 `load_settings()`——后者指回旧的单租户目录。
