"""API 层共享依赖（O-11 C-2）。

**为什么要这个文件**：拆分前，`create_app()` 里的 27 个路由都靠**闭包**拿
`cfg` 和 `_tenant(request)`。闭包一旦打散到多个模块就没了，必须改成显式注入。

**为什么只在这里读 cfg**：`_tenant()` 依赖 `cfg.tenant_root` 与 `owner_names(cfg)`
决定「有哪些租户、归档写哪」。如果各模块各自去读模块级 `SETTINGS`，
那么在测试里传入自定义 `Settings` 时就会出现
「app 用 A 的目录，路由写进 B 的目录」——**静默串租户，回放和归档看起来还都正常**。
所以：**cfg 只从 `request.app.state.cfg` 取，别处一律走本模块的函数**。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import Request

from config import Settings, for_owner
from utils import get_logger

logger = get_logger("api")

# 归档里的原始音频扩展名（回放 / 切片 / 认领都先按这个找文件）
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".wma"}

# 收藏标记文件名。用独立的空文件而不是往 meta.json 里塞字段，
# 是因为 meta.json 会被 refresh_archive 之类的重建流程整份覆写。
# 与 deploy/retention.py 的 `FAVORITE_NAME` 必须一致——那边靠它判「免清理」。
FAVORITE_NAME = ".favorite"

# 音色归属表的短缓存：{"at": 取表时刻, "map": {voice_id: owner}}
# tts_server 是单进程、不知道谁在请求，所以隔离判定只能放在这里。
# 缓存 10 秒：合成是一句一次，每句都多打一次本机往返不划算。
_VOICE_MAP: dict = {"at": 0.0, "map": {}}


def cfg_of(request: Request) -> Settings:
    """当前 app 的全局配置。

    ⚠️ 唯一的 cfg 来源。不要 import config.SETTINGS 来用——
    那样测试里传进来的 Settings 就失效了（见模块 docstring）。
    """
    return request.app.state.cfg


def tenant(request: Request) -> Settings:
    """按当前请求的 owner 派生目录配置（归档 / 工作目录 / 声纹库全按它隔离）。"""
    cfg = cfg_of(request)
    return for_owner(getattr(request.state, "owner", cfg.default_owner), cfg)


# --------------------------------------------------------------------------- #
# 归档目录访问（meetings / speakers 两个模块共用，所以放这里，
# 不要让 api/speakers.py 去 import api/meetings.py —— 那会造出反向依赖）
# --------------------------------------------------------------------------- #
def archive_dir(request: Request, dir: str) -> Path | None:
    """校验归档目录名（必须是单层名字，且落在该租户 processed 下）。

    路径穿越防线：`name != dir` 挡掉带 / 或 \\ 的输入；
    resolve 后再 relative_to(root) 兜一层符号链接绕过。
    """
    name = Path(dir or "").name
    if not name or name != dir:
        return None
    ts = tenant(request)
    root = ts.processed_dir.resolve()
    folder = (ts.processed_dir / name).resolve()
    try:
        folder.relative_to(root)
    except Exception:
        return None
    return folder if folder.is_dir() else None


def archive_source(folder: Path) -> Path | None:
    """归档里的原始音频：优先 meta.json 记的，其次目录里第一个音频。"""
    meta = folder / "meta.json"
    if meta.is_file():
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            cand = folder / Path(str(info.get("audio", ""))).name
            if cand.is_file():
                return cand
        except Exception:
            pass
    for item in sorted(folder.iterdir()):
        if item.is_file() and item.suffix.lower() in AUDIO_EXTS:
            return item
    return None


def find_meeting(ts: Settings, mid: str) -> Path | None:
    """按 meeting_id 或目录名找归档目录（兼容旧链接直接传目录名）。"""
    processed = ts.processed_dir
    if not processed.is_dir():
        return None
    direct = processed / Path(mid).name
    if Path(mid).name == mid and direct.is_dir():
        return direct
    for folder in processed.iterdir():
        if folder.is_dir() and meeting_id(folder) == mid:
            return folder
    return None


def voice_owners(ts: Settings) -> dict[str, str]:
    """voice id → owner。拿不到就沿用上次结果，返回空表表示「不知道归属」。"""
    now = time.time()
    if now - _VOICE_MAP["at"] < 10.0:
        return _VOICE_MAP["map"]
    try:
        import urllib.request

        with urllib.request.urlopen(f"{ts.tts_base_url}/voices", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        table = {}
        for item in (data.get("voices") or []):
            vid, owner = item.get("id"), item.get("owner") or ""
            if vid:
                table[str(vid)] = str(owner)
        _VOICE_MAP.update({"at": now, "map": table})
        return table
    except Exception as exc:  # noqa: BLE001 - 合成服务没起来时不该让页面挂掉
        logger.debug("音色归属表取不到，沿用上次：%s", exc)
        return _VOICE_MAP["map"]


def voice_visible(voice: dict, owner: str, table: dict[str, str]) -> bool:
    """这条音色当前租户能不能看见（列表用）。"""
    vid = str(voice.get("id") or "")
    if vid.startswith("sys:"):
        return True                      # 电脑声音人人可用
    mine = table.get(vid)
    if mine is None:
        return True                      # 表里没有 = 老音色，放行（否则升级即全消失）
    return (not mine) or mine == owner   # owner 为空同样是老音色


def voice_allowed(voice_id: str, owner: str, table: dict[str, str]) -> bool:
    """这个租户能不能**使用**这条音色（合成前校验，越权要 403 不回退）。"""
    if str(voice_id or "").startswith("sys:"):
        return True
    mine = table.get(str(voice_id))
    if mine is None:
        return True
    return (not mine) or mine == owner


def log_llm(ts: Settings, result: dict, ref: str, scene: str) -> None:
    """把一次 LLM 调用的 usage 换算成钱记一笔。记账失败绝不影响回复。"""
    try:
        from costs import llm_cost, log as log_cost

        usage = result.get("usage") or {}
        if not usage.get("prompt_tokens") and not usage.get("completion_tokens"):
            return
        billed = llm_cost(
            str(result.get("model") or ts.llm_model),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("cache_hit_tokens") if usage.get("cache_hit_known") else None,
        )
        log_cost("llm", ref=ref, scene=scene, owner=ts.owner, **billed)
    except Exception as exc:  # pragma: no cover - 记账永远是可选的
        logger.debug("LLM 成本记账跳过：%s", exc)


def meeting_id(folder: Path) -> str:
    """从归档目录的 meta.json 取 meeting_id；取不到返回空串。"""
    meta = folder / "meta.json"
    if meta.is_file():
        try:
            return str(json.loads(meta.read_text(encoding="utf-8")).get("meeting_id") or "")
        except Exception:
            return ""
    return ""
