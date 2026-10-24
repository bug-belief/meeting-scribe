"""首次启动自检：让「clone 下来能不能起来」这件事有明确答案。

为什么单独一个文件而不是塞进 config.py
--------------------------------------
`config.py` 只负责「把环境变量读成 Settings」，它不该知道目录该不该在仓库里、
也不该打印任何东西 —— 那是副作用。启动期的检查与引导都属于副作用，放这里。

三件事，按「拦不拦得住人」排序
------------------------------
1. **数据目录不能落在代码仓库里**（`check_data_outside_repo`）
   不拦的话，`git status` 会被几百个音频文件污染，`git clean -fdx` 一键删光，
   提交时还可能把别人的录音带上去。这是**唯一拒启动**的项。

2. **没配口令就不许对外监听**（`check_web_token`）
   `config.resolve_owner()` 的空口令语义是「本地调试，直接放行」
   —— 对一个绑 0.0.0.0 的服务，等于把录音、声纹、纪要全部公开。
   所以：监听非回环地址 + 没有 WEB_TOKEN/TENANT_TOKENS → 自动生成随机口令。

3. **可选能力缺失只降级不拦**（`report_optional`）
   声纹/离线 ASR/知识库/语音合成都属于「没有也能出稿」的，缺了要明确说
   缺了什么、怎么补，而不是让用户在一堆 traceback 里猜。

设计约束
--------
- 纯函数优先：判定逻辑（`is_inside`、`needs_token`）与副作用（写文件、打印）分开，
  这样判定能用假数据测，不必真起服务。
- 幂等：跑 N 次结果一致；已有的 `.env` 只**追加**缺失项，不覆盖用户配置。
"""

from __future__ import annotations

import logging
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("bootstrap")

# 这些主机名/IP 视为「只给本机用」，没口令也可以
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}

# 未配时自动追加到 .env 的项（只在需要时写，不主动改用户已有值）
TOKEN_KEY = "WEB_TOKEN"


class BootstrapError(RuntimeError):
    """启动前置条件不满足。消息会原样打印给用户，所以要说人话 + 给修法。"""


@dataclass
class BootstrapReport:
    """自检结果。`ok=False` 表示拒启动。"""

    ok: bool = True
    generated_token: str = ""
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = []
        if self.generated_token:
            lines.append(f"已生成访问口令：{self.generated_token}")
            lines.append("（已写入 .env 的 WEB_TOKEN，打开页面时用 ?k=<口令> 进入）")
        lines.extend(f"提示：{w}" for w in self.warnings)
        lines.extend(f"信息：{n}" for n in self.notes)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 判定（纯函数，可测）
# --------------------------------------------------------------------------- #
def is_inside(child: Path, parent: Path) -> bool:
    """child 是否等于 parent 或在它下面。两者都会先 resolve 掉 `..` 与软链接。

    为什么必须 resolve：`processed/../../app/data` 字面上不以 `app` 开头，
    但解析后就是仓库内部；只做字符串前缀比较会漏掉这种写法。
    """
    try:
        c = child.resolve()
        p = parent.resolve()
    except OSError:      # 路径不存在 / 权限不足：宁可当作不在里面，别误拦
        return False
    if c == p:
        return True
    return p in c.parents


def needs_token(host: str, has_token: bool) -> bool:
    """这个监听地址 + 当前口令状态，是否必须有个口令才能启动。

    `has_token` 为真时永远不需要生成：用户已经自己配了。
    """
    if has_token:
        return False
    return (host or "").strip().lower() not in _LOOPBACK_HOSTS


def token_configured(settings) -> bool:
    """是否已经配了口令（单口令或多租户任一）。"""
    return bool((getattr(settings, "web_token", "") or "").strip()
                or getattr(settings, "tenant_token_map", None))


# --------------------------------------------------------------------------- #
# 检查 1：数据目录不能落在仓库里（拒启动）
# --------------------------------------------------------------------------- #
def check_data_outside_repo(settings) -> None:
    """数据目录落在代码仓库里 → 抛 BootstrapError。

    只检查**最上层的几个根**：所有租户目录都从 `tenant_root` 派生，
    声纹库在 `processed.parent`，钉住这两处就够了，不必逐个字段比。
    """
    repo = Path(getattr(settings, "base_dir", ".")).resolve()
    candidates = {
        "PROCESSED_DIR": getattr(settings, "processed_dir", None),
        "INBOX_DIR": getattr(settings, "inbox_dir", None),
        "WORK_DIR": getattr(settings, "work_dir", None),
        "LOGS_DIR": getattr(settings, "logs_dir", None),
        "TENANT_ROOT": getattr(settings, "tenant_root", None),
        "SPEAKER_STORE": getattr(settings, "speaker_store", None),
    }
    bad = [f"{k}={v}" for k, v in candidates.items() if v is not None and is_inside(Path(v), repo)]
    if not bad:
        return
    raise BootstrapError(
        "数据目录不能放在代码仓库里（现在这些目录在仓库内）：\n  "
        + "\n  ".join(bad)
        + f"\n\n代码目录：{repo}"
        + "\n\n原因：录音、声纹、纪要会被 git 当成未跟踪文件（`git status` 一片红），"
          "\n`git clean -fdx` 会连录音一起删掉，提交时还可能把别人的录音带上去。"
        + "\n\n修法（二选一）："
          "\n  ① 在 .env 里把数据挪出去（推荐，一次到位）："
          "\n       PROCESSED_DIR=$HOME/data/processed"
          "\n       INBOX_DIR=$HOME/data/audios_inbox"
          "\n       WORK_DIR=$HOME/data/work"
          "\n       LOGS_DIR=$HOME/data/logs"
          "\n     （脚本不展开 $HOME，请写绝对路径，例如 /home/<你>/data/processed）"
          "\n  ② 用部署脚本生成：bash deploy/setup-env.sh"
          "\n后再启动。"
    )


# --------------------------------------------------------------------------- #
# 检查 2：没口令就不许对外监听（自动生成）
# --------------------------------------------------------------------------- #
def generate_token() -> str:
    """生成访问口令。`token_urlsafe(24)` ≈ 32 个字符，足够抗暴力猜。"""
    return secrets.token_urlsafe(24)


def _append_env(env_file: Path, key: str, value: str) -> bool:
    """向 .env 追加一行。已有同名键则不写（绝不覆盖用户配置）。返回是否写入。"""
    try:
        text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    except OSError as exc:
        logger.warning("读 %s 失败：%s", env_file, exc)
        return False
    for line in text.splitlines():
        if line.strip().startswith(f"{key}="):
            return False                 # 已有（可能是空值），交给用户自己处理
    env_file.parent.mkdir(parents=True, exist_ok=True)
    # 先建文件再写：`open("a")` 对**已存在**的文件不会改权限，
    # 所以必须显式 chmod，否则口令可能是 0644（同机其他用户可读）。
    existed = env_file.exists()
    with open(env_file, "a", encoding="utf-8", newline="\n") as fh:
        if text and not text.endswith("\n"):
            fh.write("\n")
        fh.write(f"\n# 首次启动自动生成（{key}）——想换口令就改这一行后重启\n{key}={value}\n")
    if not existed:
        try:
            env_file.chmod(0o600)        # 只给属主读
        except OSError:
            pass
    else:
        # 已存在的 .env 也可能是 644，顺手收紧（不改内容，只改权限）
        try:
            if env_file.stat().st_mode & 0o077:
                env_file.chmod(0o600)
        except OSError:
            pass
    return True


def check_web_token(settings, host: str, env_file: Path | None = None,
                    report: BootstrapReport | None = None) -> str:
    """没配口令且对外监听 → 生成一个并写进 .env。返回生成的口令（没生成则为空）。

    为什么要在**启动时**做而不是报错退出：新人跑起来第一件事应该是看到面板，
    而不是先去翻文档配口令。但同时**绝不能悄悄用空口令对外服务**。
    """
    report = report if report is not None else BootstrapReport()
    if not needs_token(host, token_configured(settings)):
        if not token_configured(settings):
            report.notes.append(
                "未设置 WEB_TOKEN，当前只监听本机，所有请求按默认租户放行。"
                "要给别人访问请先设置 WEB_TOKEN。")
        return ""

    token = generate_token()
    written = False
    if env_file is not None:
        written = _append_env(env_file, TOKEN_KEY, token)
    # 立刻生效：本进程不再走「空口令放行」分支
    try:
        settings.web_token = token           # type: ignore[misc]
    except Exception:                        # frozen dataclass 之类
        object.__setattr__(settings, "web_token", token)
    report.generated_token = token
    report.warnings.append(
        f"监听 {host} 但没有配置访问口令 —— 已自动生成并写入 "
        f"{env_file if written else '（内存中，未能写 .env）'}。"
        "不改的话重启会再生成一个新的。")
    return token


# --------------------------------------------------------------------------- #
# 检查 3：可选能力缺失（只提示）
# --------------------------------------------------------------------------- #
def _has_module(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _kb_enabled() -> bool:
    """知识库是否启用。关掉时不必报「包缺失」——那是预期状态。"""
    import os

    return (os.getenv("KB_ENABLED") or "1").strip().lower() not in {"0", "false", "no", "off"}


def _probe(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def report_optional(settings, report: BootstrapReport | None = None,
                    probe_retries: int = 3, probe_delay: float = 1.0) -> list[str]:
    """检查可选依赖与外部服务，缺什么就说清后果与补法。返回提示列表。

    `probe_retries` / `probe_delay` 是为了避开**启动竞争**：`deploy.sh` 会把
    web 和 tts 一起重启，web 先起来时 tts 还没监听 —— 实测报过一次
    「语音合成服务连不上」，实际上 2 秒后就 200 了。误报比不报更糟：
    用户不会再信第二条提示。
    """
    report = report if report is not None else BootstrapReport()
    msgs: list[str] = []

    # 🔴 放在最前面：这一项比其他所有项都严重。
    #    缺 funasr 只是少一个功能；缺 python-dotenv 会让 **.env 整个不被读取** ——
    #    所有配置静默退回代码默认值，包括 LLM_API_KEY、WEB_TOKEN、数据目录。
    #    症状是「我明明配了却不生效」，而系统一声不响（全新环境首次部署时撞到过）。
    if not _has_module("dotenv"):
        msgs.append(
            "未安装 python-dotenv：**.env 不会被读取**，所有配置都退回默认值"
            "（含 LLM_API_KEY / WEB_TOKEN / 数据目录）。"
            "装它：pip install python-dotenv（已在 requirements.txt 里）。")

    if not _has_module("funasr"):
        msgs.append(
            "未安装 funasr：实时字幕不可用、上传文件也无法本地转写。"
            "改用云端请设 ASR_BACKEND=cloud + ASR_CLOUD_API_KEY；"
            "要本地转写装 requirements-asr-local.txt。")

    # 索引包：两个都要看，只看一个会假绿（KB_PACK 有而 LEGAL_PACK 缺时，
    # 法条检索静默变空，界面上看不出区别）。
    # ⚠️ 路径规则不在这里重写一遍 —— 调 kb_bridge 自己的函数。
    #    两处各写一套解析（默认值、expanduser、环境变量名）迟早会漂。
    if _has_module("kb_bridge") and _kb_enabled():
        import kb_bridge

        missing = [str(p) for p in (kb_bridge.pack_path(), kb_bridge.legal_pack_path())
                   if not p.is_file()]
        if missing:
            msgs.append("知识库索引包缺失（对应检索会为空，其它功能不受影响）："
                        + "、".join(missing)
                        + "。生成方法见 knowledge/legal/README.md。")

    # 语音合成是独立进程，探一下端口即可，别在这里 import
    base = str(getattr(settings, "tts_base_url", "") or "").strip()
    if base:
        raw = base.split("://", 1)[-1].rstrip("/")
        host, _, port_s = raw.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            host, port = raw, 80
        # 重试几次：启动时序不确定，web 可能比 tts 先起来
        alive = False
        for i in range(max(1, probe_retries)):
            if _probe(host or "127.0.0.1", port, timeout=1.0):
                alive = True
                break
            if i + 1 < probe_retries:
                time.sleep(probe_delay)
        if not alive:
            msgs.append(f"语音合成服务 {base} 连不上（已重试 {probe_retries} 次）："
                        "会没有声音，其余功能正常。"
                        "启动它：systemctl --user start meeting-tts。"
                        "若是刚重启过，等几秒再刷新页面即可。")

    report.notes.extend(msgs)
    for m in msgs:
        logger.info("自检提示：%s", m)
    return msgs


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run(settings, host: str, env_file: Path | None = None) -> BootstrapReport:
    """完整自检。抛 BootstrapError 表示必须停下；否则返回报告（可能含自动修复）。"""
    report = BootstrapReport()
    check_data_outside_repo(settings)          # 唯一拒启动项
    check_web_token(settings, host, env_file, report)
    report_optional(settings, report)
    return report
