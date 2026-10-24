"""知识检索桥：把个人 KB 的索引包接进「代答」和「提词」。

为什么单独一层，而不是在 assistant.py 里直接调 kb_search：
  · 包是按 mtime 懒加载的，改完索引包不用重启服务；
  · 检索失败绝不能让代答失败——这里把所有异常都吃掉返回空串；
  · 注入格式集中在一处，改预算/改措辞不用动两个调用点。

隐私边界：索引包在**构建期**就已经把 `visibility: secret` 的条目和
`background.md` 排除掉了（见 tools/kb_search.py），线上根本不存在这些内容，
不是查到再打码。这里只再按 visibility 挡一道。

环境变量（都可选）：
    KB_PACK       索引包路径，默认 ~/data/kb/kb-pack.json
    KB_ENABLED    0 关闭检索注入
    KB_TOP_K      每次取几条，默认 3（场景包可以覆盖）
    KB_BUDGET     注入正文的字数上限，默认 700（场景包可以覆盖）
    KB_SCENARIO   默认场景档，默认 default
    KB_SCENARIOS  场景包路径，默认 ~/data/kb/scenarios.yaml
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import Lock

logger = logging.getLogger("kb")

_lock = Lock()
_cache: dict = {"path": None, "mtime": 0.0, "pack": None}
_legal: dict = {"path": None, "mtime": 0.0, "pack": None}
_scen: dict = {"path": None, "mtime": 0.0, "data": None}

# 场景包文件丢了也得能跑：这里是内置兜底，字段含义见 deploy/scenarios.yaml
DEFAULT_SCENARIOS: dict = {
    "version": 1,
    "default": "default",
    "scenarios": {
        "default": {"label": "通用", "desc": "不限领域、不限听众",
                    "scopes": [], "audience": "", "top_k": 3, "budget": 700},
        "meeting": {"label": "开会 · 客户", "desc": "技术项目 + 职场 + 成本",
                    "scopes": ["tech", "career", "finance", "general"],
                    "audience": "client", "top_k": 3, "budget": 700},
        "interview": {"label": "面试 · 求职", "desc": "只讲能对外讲的项目与技术",
                      "scopes": ["career", "tech", "general"],
                      "audience": "recruiter", "top_k": 3, "budget": 900},
        "negotiation": {"label": "谈判 · 维权", "desc": "法律与钱优先",
                        "scopes": ["legal", "finance", "career", "general"],
                        "audience": "client", "top_k": 3, "budget": 800},
        "call": {"label": "电话 · 日常", "desc": "朋友闲聊，取少一点",
                 "scopes": [], "audience": "friend", "top_k": 2, "budget": 500},
    },
}

# 只有这些可见性的条目能被注入到对话里。
# `background`（自述、草稿这类底稿）默认不注入——它适合自己看，
# 不适合让助理在电话里张口就来；确实要用时设 KB_ALLOW_BACKGROUND=1。
def allowed_visibility() -> set[str]:
    base = {"public", "internal"}
    if (os.getenv("KB_ALLOW_BACKGROUND") or "").strip().lower() in {"1", "true", "yes"}:
        base.add("background")
    return base


ALLOWED_VISIBILITY = {"public", "internal"}


def pack_path() -> Path:
    raw = (os.getenv("KB_PACK") or "").strip()
    return Path(raw).expanduser() if raw else Path("~/data/kb/kb-pack.json").expanduser()


def enabled() -> bool:
    return (os.getenv("KB_ENABLED") or "1").strip() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def legal_pack_path() -> Path:
    raw = (os.getenv("KB_LEGAL_PACK") or "").strip()
    return Path(raw).expanduser() if raw else Path("~/data/kb/legal-pack.json").expanduser()


def _load_one(path: Path, cache: dict, label: str) -> dict | None:
    """按 mtime 缓存：改了索引包，下一次查询自动生效，不用重启 meeting-web。"""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _lock:
        if cache["path"] == path and cache["mtime"] == mtime and cache["pack"]:
            return cache["pack"]
        try:
            import kb_search

            pack = kb_search.load_pack(path)
        except Exception as exc:  # 包坏了也不能拖垮代答
            logger.warning("知识包加载失败（%s）：%s", label, exc)
            return None
        cache.update(path=path, mtime=mtime, pack=pack)
        logger.info("知识包已加载（%s）：%s（%d 条 / %d 段）",
                    label, path, pack.get("count", 0), len(pack.get("chunks", [])))
        return pack


def load() -> dict | None:
    """个人 KB 主包。"""
    return _load_one(pack_path(), _cache, "个人KB")


def load_legal() -> dict | None:
    """法律语料包。没有这个文件就静默跳过，不影响代答。"""
    return _load_one(legal_pack_path(), _legal, "法律")


# ---------------------------------------------------------------- 场景包

def scenarios_path() -> Path:
    raw = (os.getenv("KB_SCENARIOS") or "").strip()
    return Path(raw).expanduser() if raw else Path("~/data/kb/scenarios.yaml").expanduser()


def load_scenarios() -> dict:
    """按 mtime 加载场景包；文件不在或格式坏了就用内置兜底（不能让代答停摆）。"""
    p = scenarios_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return DEFAULT_SCENARIOS
    with _lock:
        if _scen["path"] == p and _scen["mtime"] == mtime and _scen["data"]:
            return _scen["data"]
    try:
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict) or not isinstance(data.get("scenarios"), dict):
            raise ValueError("缺少 scenarios 段")
    except Exception as exc:
        logger.warning("场景包加载失败，改用内置兜底：%s", exc)
        return DEFAULT_SCENARIOS
    with _lock:
        _scen.update(path=p, mtime=mtime, data=data)
    logger.info("场景包已加载：%s（%d 档）", p, len(data["scenarios"]))
    return data


def scenario_names() -> list[dict]:
    """给前端下拉框用：[{name, label, desc}]。"""
    table = (load_scenarios().get("scenarios") or {})
    return [{"name": k, "label": (v or {}).get("label") or k,
             "desc": (v or {}).get("desc") or ""} for k, v in table.items()]


def resolve_scenario(name: str | None = None) -> dict:
    """把档名解析成可直接用的过滤条件。档名不认识就退回 default，不报错。"""
    data = load_scenarios()
    table = data.get("scenarios") or {}
    raw = (name or os.getenv("KB_SCENARIO") or data.get("default") or "default")
    key = str(raw).strip() or "default"
    hit = table.get(key)
    cfg = dict(hit if isinstance(hit, dict) else table.get("default")
               or DEFAULT_SCENARIOS["scenarios"]["default"])
    cfg["name"] = key if isinstance(hit, dict) else "default"

    scopes = cfg.get("scopes") or []
    if isinstance(scopes, str):
        scopes = [x.strip() for x in scopes.replace("，", ",").split(",") if x.strip()]
    cfg["scopes"] = [str(x) for x in scopes]

    # `audience`（单数）= 这次听的人是谁；`audiences`（复数）= 允许任一听众，给调试用
    aud = str(cfg.get("audience") or "").strip()
    if aud:
        cfg["audiences"] = {aud}
    else:
        many = cfg.get("audiences") or []
        if isinstance(many, str):
            many = [x.strip() for x in many.replace("，", ",").split(",") if x.strip()]
        cfg["audiences"] = {str(x) for x in many}
    return cfg


def packs() -> list[tuple[str, dict]]:
    """返回 [(corpus 标签, 包)]。主包在前，命中分数相同时个人经验优先。"""
    out: list[tuple[str, dict]] = []
    kb = load()
    if kb:
        out.append(("kb", kb))
    legal = load_legal()
    if legal:
        out.append(("legal", legal))
    return out


def warm() -> None:
    """预加载语料并把倒排索引建好。

    法律包 4.8 万条，首次检索要 1.7 秒（读 41MB JSON 0.3s + 建索引 1.4s）。
    这活儿不干在启动阶段，就会砸在用户第一次开口的时候。异常全吞——
    预热失败顶多是首答慢一点，不该影响服务起来。
    """
    for corpus, pack in packs():
        try:
            import kb_search

            kb_search.build_index(pack)
            logger.info("预热完成：%s（%d 条）", corpus, pack.get("count") or 0)
        except Exception as exc:  # pragma: no cover - 预热失败不致命
            logger.debug("预热跳过（%s）：%s", corpus, exc)


def build_query(lines: list[dict] | None, ask: str = "", window: int = 3) -> str:
    """取最近几句 + 当前待回应内容做查询词。

    只取最近 3 句：再往前的话题多半已经翻篇，带进来反而稀释关键词。
    """
    rows = [str(x.get("text") or "").strip() for x in (lines or []) if x.get("text")]
    tail = " ".join(rows[-window:])
    q = (tail + " " + str(ask or "")).strip()
    return q[:400]


def hits(lines: list[dict] | None = None, ask: str = "",
         query: str = "", top_k: int | None = None, scenario: str = "") -> list[dict]:
    """检索命中。`scenario` 决定领域/听众闸门与取几条；不认识就按 default 走。"""
    if not enabled():
        return []
    q = query or build_query(lines, ask)
    if not q.strip():
        return []
    sc = resolve_scenario(scenario)
    k = top_k or int(sc.get("top_k") or 0) or _env_int("KB_TOP_K", 3)
    allowed = allowed_visibility()
    scopes = set(sc.get("scopes") or []) or None
    audiences = set(sc.get("audiences") or []) or None
    merged: list[dict] = []
    for corpus, pack in packs():
        try:
            import kb_search

            # 法律语料才开扩展：法条是「法言法语」，用户说的是口语，
            # 不扩展根本召不回（查「假货」命中不了写着「欺诈」的条文）。
            # 个人 KB 本身也是口语体，扩展只会把不相干的条目带进来。
            rows = kb_search.search(pack, q, top_k=k, visibility=allowed,
                                    scopes=scopes, audiences=audiences,
                                    expand=(corpus == "legal"))
        except Exception as exc:
            logger.debug("检索跳过（%s）：%s", corpus, exc)
            continue
        for r in rows:
            r["corpus"] = corpus
            r["scenario"] = sc["name"]
        merged.extend(rows)
    # 两个语料各自算 BM25，分数不完全同量纲；只做粗排序，同分时个人 KB 优先
    merged.sort(key=lambda x: (x["score"], x.get("corpus") == "kb"), reverse=True)
    return merged[:k]


def context_for(lines: list[dict] | None = None, ask: str = "",
                query: str = "", top_k: int | None = None,
                scenario: str = "") -> str:
    """返回可直接拼进 user 消息的背景知识块；没命中就返回空串。"""
    sc = resolve_scenario(scenario)
    rows = hits(lines, ask, query=query, top_k=top_k, scenario=sc["name"])
    if not rows:
        return ""
    budget = int(sc.get("budget") or 0) or _env_int("KB_BUDGET", 700)
    taken: list[str] = []
    used = 0
    for h in rows:
        snippet = " ".join(str(h.get("snippet") or "").split())
        block = f"- {h['title']}：{snippet}"
        if used + len(block) > budget:
            block = block[: max(budget - used, 0)]
        if len(block) < 12:
            break
        taken.append(block)
        used += len(block)
        if used >= budget:
            break
    if not taken:
        return ""
    return (
        "【背景知识】下面是使用者本人的资料片段，可能和当前话题有关。\n"
        "用法：能对上就用里面的**事实与数字**；对不上、或只是沾边，就当没看见。\n"
        "不要照念原文，不要说「根据资料」，也不要提「知识库」。\n"
        + "\n".join(taken)
    )
