# ADR-0004 · 版本号只认一个文件，且不进 Settings

## 状态

已采纳（2026-10-20）

## 背景

这套系统有三种产出形态：Python 服务、PWA 网页、Android APK。改造前版本号的状态是：

| 位置 | 改造前 |
|---|---|
| `android/AndroidManifest.xml` | 写死 `versionName="1.0.0"`，靠 `build_apk.sh --version` 临时覆盖 |
| Python 服务 | **完全没有版本号概念**，`/api/health` 只回 `ok / model_loaded / sessions` |
| `web/manifest.webmanifest` | 没有 version 字段（PWA 本身也不需要） |

结果是「这单跑的是哪版」这个问题无法回答。更实际的问题是：改完代码重启服务，
**没人能确认重启后的进程加载的到底是不是新代码**——多处手写版本号时，漏改一处就无人察觉。

## 决策

1. **唯一真源是仓库根的 `VERSION` 文件**，内容只有一行 semver（如 `1.0.0`）。
   - 不用 `pyproject.toml`：本仓没有打包分发，只为读一个版本号引入打包元数据不划算。
   - 不用 `__init__.py` 里的 `__version__`：本仓不是包，且 bash（打 APK）读 Python 文件要额外解析。
   - 纯文本任何语言都能读：Python `read_text()`、bash `tr -d '[:space:]'`、CI 直接 `cat`。

2. **版本号不进 `config.Settings`，也不允许被环境变量覆盖**。
   版本是「这份代码是什么」，不是「这台机器怎么配」。放进 Settings 就会被 `.env` 覆盖，
   两个工作区各写各的，立刻回到漂移老路。

3. **暴露分级**：
   | 接口 | 鉴权 | 给什么 | 为什么 |
   |---|---|---|---|
   | `/api/health` | 免 | `version`（仅 semver） | 它本来就印在 APK 的 versionName 里，装过的人都看得到，不算秘密；探活要能一眼看出跑的是哪版 |
   | `/api/me` | 需 | `build` = `{version, commit, dirty}` | git 提交号能反推代码状态，不给扫描器；`dirty` 能立刻看出「服务器有未提交的改动」 |

4. **读不到就降级，不抛异常**。`read_version()` 缺文件时返回 `0.0.0-unknown`。
   探活接口不能因为少一个文件就 500——那时候最需要它活着报信。

## 放弃的方案

| 方案 | 放弃原因 |
|---|---|
| 版本号放 `.env` | 会被各机器覆盖，等于没有真源 |
| 每次请求读 VERSION | 无谓 IO；版本运行期不变，启动读一次即可 |
| `/api/health` 也给 `commit` | 免鉴权接口暴露 git 提交号 = 告诉扫描器「这个部署落后几个补丁」 |
| 用 `git describe` 当版本号 | 提交号不能表达「用户装的 APK 是哪个版本」，两者语义不同 |
| 自动递增（CI 打 tag） | 目前没有 CI，手工改一行 `VERSION` 的成本低于搭一套版本发布流程 |

## 后果

- 改版本 = 改 `VERSION` 一个文件。`tests/test_version.py` 会校验
  `AndroidManifest.versionName`、`build_apk.sh 是否真的读 VERSION` 与它一致，漂移会被测出来。
- `build_apk.sh --version` 仍然保留，但只作为「临时出个不同版本」的应急口子，
  文档里标明「慎用，否则真源形同虚设」。
- `config.py` 的打印输出末尾多一行 `version = 1.0.0`（不在 Settings 的 72 个字段里）。

## 重评条件

- 出现需要「同一份代码、多个版本并存」的场景（如灰度发布）→ 得改成构建期注入。
- 有了 CI → 可以改成 CI 读 `VERSION` 打 tag，但真源仍是这个文件。
