# ADR-0002 · 格式门禁分级：lint 常开，format 默认关闭

## 状态

已采纳（2026-10-19）

## 背景

项目在 2026-10-19 22:55 才首次入 git，此前 15,393 行代码（不含 `knowledge/`）**没有任何格式约束**：

| 审计项 | 实测 |
|---|---|
| `.editorconfig` | 不存在 |
| `ruff` / `black` / `flake8` | 全未安装 |
| `pre-commit` / `.githooks` | 不存在 |
| git hook / CI | 都没有（350 项测试全靠人记得手跑） |

行尾问题已经踩过一次：服务器上 26 个文件原为 CRLF，导致 `git add` 后出现「显示已修改、`git diff` 却是空」的幽灵脏区。
修法是 `.gitattributes` 写 `* text=auto eol=lf`，并 `git reset --hard` 统一成 LF。

## 决策

门禁分三层，**依赖越少越靠前**：

| 层 | 内容 | 何时执行 |
|---|---|---|
| ① 零依赖层 | CRLF / 行尾空白 / Python 前导 Tab / 缺 final newline | **永远执行**（`bash tools/check-style.sh`，纯 bash + tr + grep） |
| ② ruff lint | `ruff check --select E,F,I` | 装了 ruff 才执行；没装则**明确打印「未校验」** |
| ③ ruff format | `ruff format --check` | **默认关闭**，需显式 `--format` |

关键取舍：**format 不做一次性全仓重排。** 15K 行重排会让 6 个提交之后的所有 `git blame` 全部指向
"格式化提交"，而真正的改动无法追溯。要重排就单独一个提交、单独一次评审、单独一个分支。

`knowledge/legal/**`（527 个外部抓取的法律原文）**不参与格式门禁**，保持原样。

## 放弃的方案

| 方案 | 为什么放弃 |
|---|---|
| 只靠 ruff，不做零依赖层 | 门禁如果依赖一个没装的工具，它就不是门禁，是一句愿望。实测服务器上 ruff 未安装 |
| 一次 `ruff format` 全仓重排 | 15K 行 diff，blame 全毁；且与逻辑改动混在一起违反「一个提交一个决策」 |
| 把全量测试放进云 CI | 核心测试依赖 GPU、systemd 与本地模型，云 CI 跑不了；云 CI 只跑「门禁 + 无需模型的套件」（见 `.github/workflows/ci.yml`） |
| pre-commit 强制 `ruff --fix` | 自动改代码会掩盖「未知字段/未定义名」的真实问题，也会在提交时引入未评审的改动 |

## 重评条件

- 装机统一到「ruff 已安装」之后，可把 ② 层从「未校验」升级为「必须执行」；
- 若决定做一次性重排，先新建 `refactor/format-only` 分支、只跑 `ruff format`、单独评审合并，并把本 ADR 状态改为「已废弃」；
- 若迁移到 GitHub/GitLab 且测试可无 GPU 运行，可加 CI 做 ①② 层。
