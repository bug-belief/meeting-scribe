# 参与维护

## 先跑起来

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 只装基础包就能起服务、用云端 ASR 出稿
sudo apt install -y ffmpeg               # 音频解码全交给它
cp .env.example .env                     # 必填只有 LLM_API_KEY
python server.py                         # 打开 http://127.0.0.1:8848
```

要离线转写、本地声纹认人、实时字幕，再装 `requirements-asr-local.txt`。
**不装也能用** —— 见 README 的「功能可用性矩阵」。

## 提交之前

两道门禁都要过（`git commit` 时会自动跑）：

```bash
bash tools/check-style.sh        # 行尾 / CRLF / 前导 Tab / 末尾换行 / 个人绝对路径
python3 tools/sanitize-check.py  # 内网 IP / 隧道域名 / 个人邮箱（通用）+ 你自己的名字（需配置）
python3 tests/run_all.py         # 全量测试（约 3 分钟）
```

> **脱敏门禁的个人规则要自己配**：通用规则（内网 IP、隧道域名、个人邮箱域名）开箱即用，
> 「该拦谁的真名 / 账号 / 住址」属于个人信息，不会写进公开源码 —— 它放在
> `tools/sanitize-rules.local.json`，被 `.gitignore` 挡住。
>
> ```bash
> cp tools/sanitize-rules.example.json tools/sanitize-rules.local.json
> # 编辑它，把你的名字/账号/老家地名填进 pattern
> ```
>
> 没配时门禁会打印「· 未校验：个人规则」——**不是通过**，是「这部分没查」。
> 这个区别很重要：上一版把规则写死在源码里，又靠「豁免本文件」解决自我拦截，
> 结果门禁自己成了最集中的泄漏点（19/25 处）。规则外置之后，一个文件都不需要豁免。

第一次克隆后启用钩子：

```bash
git config core.hooksPath .githooks
```

**绕过钩子（`--no-verify`）必须在提交说明里写明原因** —— 门禁存在的理由是
拦住「不会让任何测试失败」的那类问题（比如把 `/home/你的用户名` 写进注释），
这类问题一旦进了历史，`git rm` 也删不干净。

## 改代码的几条规矩

| 规矩 | 为什么 |
|---|---|
| **改包名前想清楚** | 包名 + 签名 = 应用身份。改了等于换应用：与旧版并存，且本地 cookie 要重输。自己打包请整包改名（`AndroidManifest.xml` + `java/` 目录要一起改） |
| **别改 systemd 单元名与目录名** | `meeting-web` / `~/app` / `~/data` 是环境契约，自用实例会直接挂 |
| **版本号只改 `VERSION`** | `/api/health`、APK `versionName` 都从它来，四处漂移会被 `tests/test_version.py` 抓到 |
| **共享前端状态只走 `S.xxx`** | 新变量进 `web/state.js`，别在 `app.js` 里加裸 `let` |
| **改完必须补一条会失败的测试** | 先故意破坏，确认测试真会红，再提交 |

更多约定见 [`AGENTS.md`](AGENTS.md)；有哪些现成工具先查 [`CAPABILITIES.md`](CAPABILITIES.md)
（**写新脚本之前先看一眼，别重新发明**）。

## 不要提交这些

`.env`、`DIALECT.md`（家乡话词表含真实地名）、`samples/` 下的真实录音、
`logs/`、`processed/`、`.deploy-state`。

前两个与语料目录已写进 `.gitignore`。**如果你要加新的个人配置，也照这个套路**：
提交 `xxx.example.md` / `.env.example`，真实文件进 `.gitignore`。

## 提 issue / PR

- 说清**怎么复现**（命令、样本音频特征、环境），比说「不工作」有用得多
- 音频样本请不要贴真实会议录音；用 `python samples/make_samples.py` 生成的合成 WAV
- 行为改动请一并更新受影响的 `docs/`，特别是 `docs/排错与验收.md`

## 许可约定

本项目以 [MIT](LICENSE) 发布，版权记为 `meeting-scribe contributors`。

**提交 PR 即视为你同意**：你提交的代码按 MIT 许可授权给本项目及其他使用者，
并允许以该许可再分发。理由很实际——MIT 要求保留版权声明，如果每位贡献者的
代码授权不明确，「这份代码能不能商用」就会变成一个没人能回答的问题。

不需要签任何文件，也不需要 DCO 签署。如果你提交的内容**不是你自己写的**
（从别的项目抄的片段、别人给的代码），请在 PR 里说明来源与它的许可，
我们不会合入许可不兼容的代码。

> 这条约定是为了让授权链完整，不是为了限制你。你自己写的代码，
> 版权仍然是你的，MIT 只是允许别人用。

