# ADR-0001 · 两个 venv 为什么不能合并

## 状态

已采纳（2026-10-19）

## 背景

本项目有两套互相冲突的 Python 依赖，分别服务两条链路：

| 链路 | 装什么 | 跑在哪 |
|---|---|---|
| 主服务（转写 / 纪要 / Web） | funasr、fastapi、numpy、python-dotenv | systemd 用户单元 |
| 语音合成（CosyVoice2） | torch、torchaudio、CosyVoice2 | 独立进程，独立 venv |

**冲突点**：CosyVoice2 对 `torch` 版本敏感，而 funasr 会把它往下拉。
实测降级后合成音质明显变差，个别音色直接报错。

## 决策

1. **两套 venv 永久分离**，各装各的。
2. 跨 venv 调用只有**一个方向**：主服务通过 **HTTP** 调合成服务
   （默认 `127.0.0.1:9880`），**从不 import 对方的包**。
   版本冲突被进程边界隔开，任一侧升级都不影响另一侧的 import 图。
3. 配置唯一入口是 `config.py` 的 `SETTINGS`（dataclass），代码里不写死路径与密钥。

## 放弃的方案

| 方案 | 为什么放弃 |
|---|---|
| 合并两个 venv | torch 版本冲突；CosyVoice2 对版本敏感，降级会改变音质 |
| 用 Docker 隔离 | GPU 直通与模型缓存都要重新安排，收益不抵改造风险；systemd 链路要重写 |
| 合成服务做成库被主服务 import | 那就回到「同一个 venv」的老问题；而且合成进程崩了会连带主服务 |

## 重评条件

- 若 CosyVoice 与 FunASR 的 torch 版本要求收敛到同一版本，可重评合并
  （需重跑 `test_voice_isolation` 与听感 A/B 对比）；
- 若要部署到多台机器，可考虑容器化，届时这套 venv 约定可以整体替换。

## 后果

- 部署多一个 systemd 单元、多一个端口；
- 升级 torch 前必须确认两侧都兼容，不能只看一侧 `pip install` 成功；
- 好处：合成服务可以单独重启、单独降级，不影响转写。
