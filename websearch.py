"""联网检索（可选能力，2026-10-22 接入）。

**为什么单独一个文件**：`tools.py` 里的天气是「命中关键词就查」的确定性工具，
而检索是「有 key 才有、还得控隐私和超时」的可选能力，混在一起会让
两边的约束互相污染（天气必须快且稳定，检索必须可降级）。

## 设计约束（三条，都来自实际场景）

1. **没有 key 就完全不工作**，返回空串，行为与接入前**完全一致**。
   这样接入是纯增量：不配就是原来的样子，配了才生效。

2. **只发关键词，不发上下文**。
   会议内容外发到第三方搜索是有边界的，所以这里做两件事：
   · 用 `extract_query()` 从一句话里剥掉代词/语气词/标点，只留查询意图
   · 长度上限 60 字，超了截断（避免把整段对话当 query）
   原始对话永远不出本机。

3. **超时 2.5 秒**，超了就放弃。

   ⚠️ **这个值改过一次，原因值得记下来**（2026-10-22 网关实测 + 我自己的探针）：
   最初设 900ms，理由是"代答预算 1~3 秒"。但实测发现**光是到 Tavily 的网络往返
   就已经 1016~1228ms**（我用无效 key 打 `/search`，拿 401 也花了 1 秒多）。
   也就是说 900ms 会让检索 **100% 超时** —— 功能装了等于没装，而且是**静默**的。

   ⇒ 现在的策略是「**不让用户等这一次**」，而不是「把超时压到不可能的短」：
   · 提到 2.5s（人等着听一句话能忍的量级）
   · 更重要的是 `prefetch()`：听到像问句的台词就**后台先查**，
     缓存 5 分钟；等真要回复时命中缓存 = 0ms。用户感知不到检索耗时。

## 支持的数据源

### 方式一：走网关（`provider=litellm`，推荐）

LiteLLM 原生有搜索（`POST /v1/search`），key 复用应用的 `LLM_API_KEY`。

```bash
WEB_SEARCH_PROVIDER=litellm
WEB_SEARCH_TOOL=<网关起的 tool 名>
```

> 🔴 **官方只支持 20 家，没有国内厂商**（2026-10-22 网关回执，权威）：
> `perplexity tavily parallel_ai exa_ai brave google_pse dataforseo firecrawl
>  fastcrw searxng linkup duckduckgo searchapi serper you_com apiserpent
>  tinyfish agentcore nimble bing_grounding`
>
> ⇒ 我原稿写的 `search_provider: zhipu` **会直接抛 ValueError**（不是"配了不生效"）。
> 博查同样不在清单里。**能用的是 Tavily**（网关已实测可达，401 只差 key）。
>
> 另：国内网络下 duckduckgo / brave / google 全不可达（HTTP 000），
> 自建 SearXNG 实测 4 秒且多半 0 结果 —— 均已排除。

### 方式二：直连 Tavily（`provider=tavily`）

网关还没配好 tool 时的保底，也适合自己拿免费 key 试（1000 次/月）。

```bash
WEB_SEARCH_PROVIDER=tavily
WEB_SEARCH_KEY=tvly-xxxxx
```

**它比其它家更适合这个场景**：`include_answer` 会直接返回一段
「答案 + 摘要」，而不是一堆需要模型自己拼的网页片段。

### 方式三：直连国内厂商（`provider=bocha` / `zhipu`，仅备用）

端点已实测（无效 key 返回 401 = 路径与格式正确），但**网关明确说这两家
不在 LiteLLM 支持列表里**，所以只能直连：
- `bocha`：`https://api.bochaai.com/v1/web-search`
- `zhipu`：`https://open.bigmodel.cn/api/paas/v4/web_search`

> ⚠️ 智谱的 web_search 走 `glm-4-plus` + tools 时会返回
> `{"code":"1113","message":"余额不足或无可用资源包,请充值。"}`（网关实测）。
> 也就是说 key 有效但**没额度**，这条路目前不可用。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """读整数环境变量，非法值退回默认（配置写错不该让模块导入就崩）。"""
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}

# ---- 关键词清理 ----
# 目的只有一个：把「你」「我」「那个」这类指代词和客套剥掉，让查询能在搜索引擎命中。
# **不做语义改写**（那需要又一次模型调用，白白加 1 秒），只做保守的剥除。
#
# ⚠️ 这里是「减少外发」，**不是脱敏**。规则法必然有漏，边界靠三件事兜：
#   ① 只外发清理后的短查询（≤60 字），原始对话永不出本机
#   ② 明显隐私的句子整句不发（见 _PRIVACY_RE）
#   ③ 用户可整体关掉（WEB_SEARCH_PROVIDER 空 = 完全不工作）
# ⚠️ 交替分支**必须长词在前**（Python 的 re 从左到右取首个能匹配的分支）。
# 2026-10-22 实测踩过：写成 `那|那么|…|那个` 时，"那个公积金" 会被 "那" 抢走，
# 剩下 "个公积金" —— 查询词直接坏掉。现在按长度降序排。
_LEAD_FILLER = re.compile(
    r"^(?:"
    r"帮我查一下|帮我查下|可不可以|能不能|你看一下|"
    r"想问一下|问一下|查一下|"
    r"那么|那个|这个|就是|然后|所以|不过|但是|"
    r"你好|您好|请问|麻烦|帮忙|帮我查|帮我|你看下|你看|你帮我|"
    r"好的|好|谢谢|多谢|收到|明白|"
    r"对了|另外|还有"
    r"想问|问下|我说|查下|"
    r"一下|"
    r"那|呃|嗯|哦|啊|喂|可以"
    r")\s*[，,、]?\s*"
)
# 句首代词：**必须用负向断言**排除「我司」「我方」「我们」这类合成词。
# 2026-10-22 实测踩过：`我司注册资本要求` 被剥成 `司注册资本要求` ——
# 「我司」是主体，剥掉之后查询就变味了（而且显得很业余）。
_LEAD_PRONOUN = re.compile(r"^(?:你们|我们|他们|咱们|你|我|他|她|它)(?![司方们])")
# 语气尾巴。「是多久」「是多少」这类要整段剥，不能只剥最后一个字。
_TAIL_FILLER = re.compile(
    r"(?:[是为]?\s*(?:多久|多少|多长|多大)|"
    r"怎么样|怎么处理|怎么办|怎么弄|如何|"
    r"吗|呢|吧|啊|呀|嘛|么|来着|了|的)\s*$"
)
_PUNCT = re.compile(r"[，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】\s]+")

# 太短的查询没有检索价值（"好的""嗯"）
_MIN_QUERY = 2
_MAX_QUERY = 60

# 纯客套：整句只由客套词拼成，说明没有实质内容，不必检索。
# 用正则而不是集合——客套经常是**组合**（"好的谢谢""嗯嗯收到"），
# 集合只能匹配单一种说法。
_COURTESY_RE = re.compile(
    r"^(?:好的|好|谢谢|多谢|感谢|辛苦|收到|明白|知道|了解|"
    r"没问题|可以|行|对|是|在|"
    r"嗯|哦|啊|呀|哈|OK|ok)+$"
)

# 出现这些词时**整句不检索**：涉及个人/公司敏感信息，不该发到外部搜索。
# 宁可这次不查，也不要外发。规则法的价值在这一条比在"清理指代"上更大。
_PRIVACY_RE = re.compile(
    r"身份证|手机号|电话号码|银行卡|卡号|密码|验证码|"
    r"合同号|统一社会信用代码|纳税人识别号|"
    r"地址|住址|门牌"
)

# 天气走专门工具（tools.weather），这里不重复查
_SKIP_RE = re.compile(r"天气|气温|多少度|下雨|下雪|带伞|穿什么")


def extract_query(text: str) -> str:
    """从一句口语里剥出可检索的关键词。**只做保守清理，不改写语义。**

    例：
        "那我想问一下车险理赔时效是多久啊"  → "车险理赔时效"
        "嗯，帮我查下专利法最新修订"        → "专利法最新修订"
        "我司注册资本要求"                  → "我司注册资本要求"（我司是主体，不剥）
        "好的"                              → ""（没有检索价值）
    """
    s = str(text or "").strip()
    if not s:
        return ""
    # 交替剥「客套 → 代词 → 语气尾」，最多 4 轮：
    # 单轮剥不净（"那我想问一下…" 要先把"那"剥掉，"我想问一下"才露出来）
    for _ in range(4):
        before = s
        s = _LEAD_FILLER.sub("", s)
        s = _LEAD_PRONOUN.sub("", s)
        s = _TAIL_FILLER.sub("", s).strip()
        if s == before:
            break
    s = _PUNCT.sub("", s).strip()
    if len(s) < _MIN_QUERY:
        return ""
    if _COURTESY_RE.match(s):
        return ""
    return s[:_MAX_QUERY]


def should_search(text: str) -> bool:
    """这句话值不值得联网查。宁少不多——每次检索都有延迟与隐私成本。"""
    raw = str(text or "")
    if _PRIVACY_RE.search(raw):
        return False
    q = extract_query(raw)
    if not q:
        return False
    if _SKIP_RE.search(q):
        return False
    return True


# --------------------------------------------------------------------------- #
# 各数据源
# --------------------------------------------------------------------------- #
def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "meeting-scribe/1.1",
        **headers,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _search_bocha(query: str, key: str, timeout: float, count: int) -> list[dict]:
    out = _post_json(
        "https://api.bochaai.com/v1/web-search",
        {"query": query, "count": count, "summary": True},
        {"Authorization": f"Bearer {key}"},
        timeout,
    )
    pages = (((out.get("data") or {}).get("webPages") or {}).get("value") or [])
    rows = []
    for p in pages[:count]:
        rows.append({
            "title": str(p.get("name") or "").strip(),
            "snippet": str(p.get("summary") or p.get("snippet") or "").strip(),
            "url": str(p.get("url") or "").strip(),
        })
    return rows


def _search_zhipu(query: str, key: str, timeout: float, count: int) -> list[dict]:
    out = _post_json(
        "https://open.bigmodel.cn/api/paas/v4/web_search",
        {"search_engine": "search_std", "search_query": query, "count": count},
        {"Authorization": f"Bearer {key}"},
        timeout,
    )
    rows = []
    for p in (out.get("search_result") or [])[:count]:
        rows.append({
            "title": str(p.get("title") or "").strip(),
            "snippet": str(p.get("content") or "").strip(),
            "url": str(p.get("link") or "").strip(),
        })
    return rows


def _search_litellm(query: str, key: str, timeout: float, count: int) -> list[dict]:
    """走网关的 `/v1/search`。

    LiteLLM 用 `model` 字段指定 search tool 的名字（不是模型名）。
    key 复用应用现有的 LLM_API_KEY —— 网关会按这把 key 的权限决定能不能用。

    ⚠️ 2026-10-22 状态：当时那套网关**尚未配置任何 search tool**，
    所以这条路实现好了但**还没跑通过真实响应**。等网关配上再实测解析字段；
    在那之前它只会返回空列表（安全降级），不会影响代答。
    """
    base = (os.environ.get("LLM_BASE_URL") or "").rstrip("/")
    if not base:
        return []
    tool = os.environ.get("WEB_SEARCH_TOOL", "").strip()
    if not tool:
        return []
    root = base[:-3] if base.endswith("/v1") else base
    out = _post_json(
        f"{root}/v1/search",
        {"model": tool, "query": query, "max_results": count},
        {"Authorization": f"Bearer {key}"},
        timeout,
    )
    # LiteLLM 的搜索响应形状：{"results": [...]} 或 {"data": [...]}
    rows = []
    for p in (out.get("results") or out.get("data") or [])[:count]:
        rows.append({
            "title": str(p.get("title") or "").strip(),
            "snippet": str(p.get("content") or p.get("snippet") or "").strip(),
            "url": str(p.get("url") or "").strip(),
        })
    return rows


def _search_tavily(query: str, key: str, timeout: float, count: int) -> list[dict]:
    """直连 Tavily。

    **为什么优先它**：Tavily 是专为 LLM 设计的，`include_answer` 直接返回一段
    已成文的答案。我们这里要的是"能照着说的一句话"，不是一堆需要再加工的片段。
    所以把 answer 作为第一条（`title` 标记为「直接答案」），后面才是网页摘要。

    API 细节（2026-10-22 实测）：
      · POST https://api.tavily.com/search
      · 鉴权支持 `Authorization: Bearer` 和 body 里传 `api_key` 两种，
        两种都返回 401（无效 key），说明路径与格式都对
      · **光是往返就 1~1.2 秒**（拿 401 的耗时），所以超时必须 ≥2s
    """
    body = {
        "query": query,
        "max_results": max(1, count),
        "include_answer": True,
        "search_depth": "basic",
    }
    out = _post_json("https://api.tavily.com/search", body,
                     {"Authorization": f"Bearer {key}"}, timeout)
    rows = []
    ans = str(out.get("answer") or "").strip()
    if ans:
        # 放在第一位，并显式标注来源性质：模型据此知道这条是"结论"而非"片段"
        rows.append({"title": "直接答案", "snippet": ans, "url": ""})
    for p in (out.get("results") or [])[:count]:
        rows.append({
            "title": str(p.get("title") or "").strip(),
            "snippet": str(p.get("content") or "").strip(),
            "url": str(p.get("url") or "").strip(),
        })
    return rows


_PROVIDERS = {"bocha": _search_bocha, "zhipu": _search_zhipu,
              "tavily": _search_tavily, "litellm": _search_litellm}

# 同一次对话里反复问同一件事，没必要每次都发请求（也省额度）
_CACHE: dict[str, tuple[float, str]] = {}
# 检索超时（秒）。见 config() 的说明：实测到 Tavily 的往返就要 1~1.2s，
# 设 0.9 会让每次检索都超时且静默失败。2.5 是"用户等着听一句话"能忍的量级。
DEFAULT_TIMEOUT = 2.5

_CACHE_TTL = 300.0     # 5 分钟。比天气短——检索结果时效性更强
_CACHE_MAX = 32


def config() -> tuple[str, str, float]:
    """读配置：(provider, key, timeout)。拿不到就返回空 provider（= 不启用）。

    `litellm` 这条特殊：**不需要单独的 key**，复用应用现有的 LLM_API_KEY
    和 LLM_BASE_URL（网关按那把 key 的权限判断能不能搜）。
    这样网关配好 search tool 之后，应用侧只加一个 provider 名就能用，
    不用再传一次密钥。

    ⚠️ 超时默认 **2.5 秒**，不是 900ms。
    实测证据：连"到 Tavily 拿一个 401"都要 1016~1228ms ——
    900ms 会让每一次真实检索都超时，而且**静默**（返回空串，谁也不知道）。
    这个值原本就是我自己设错的，2026-10-22 由网关指出并被我实测确认。
    """
    provider = os.environ.get("WEB_SEARCH_PROVIDER", "").strip().lower()
    try:
        timeout = float(os.environ.get("WEB_SEARCH_TIMEOUT", "") or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    if provider not in _PROVIDERS:
        return "", "", timeout

    if provider == "litellm":
        # 没配 tool 名等于没配 —— 否则每次都会打一个必然失败的空查询
        if not os.environ.get("WEB_SEARCH_TOOL", "").strip():
            return "", "", timeout
        return provider, os.environ.get("LLM_API_KEY", "").strip(), timeout

    key = os.environ.get("WEB_SEARCH_KEY", "").strip()
    if not key:
        return "", "", timeout
    return provider, key, timeout


def enabled() -> bool:
    return bool(config()[0])


def search(query: str, count: int = 3) -> list[dict]:
    """真正发请求。失败/超时/没配 key 一律返回空列表 —— 绝不让调用方拿到假数据。"""
    provider, key, timeout = config()
    if not provider:
        return []
    key_cache = f"{provider}:{query}:{count}"
    now = time.time()
    hit = _CACHE.get(key_cache)
    if hit and now - hit[0] < _CACHE_TTL:
        return json.loads(hit[1])
    try:
        rows = _PROVIDERS[provider](query, key, timeout, count)
    except Exception:
        return []
    if rows:
        if len(_CACHE) >= _CACHE_MAX:
            # 简单的 LRU：丢掉最早的一条
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            _CACHE.pop(oldest, None)
        _CACHE[key_cache] = (now, json.dumps(rows, ensure_ascii=False))
    return rows


def maybe_search(text: str, count: int = 3) -> str:
    """给提示词用的入口：命中才查，返回一段可直接拼进 prompt 的文字。

    返回空串表示「没有可用检索结果」——调用方按"没有实时信息"处理，
    提示词里已经写明这种情况要说「我确认一下」，不会编造。
    """
    if not enabled() or not should_search(text):
        return ""
    q = extract_query(text)
    rows = search(q, count=count)
    if not rows:
        return ""
    lines = []
    for r in rows:
        title = r.get("title") or ""
        snippet = r.get("snippet") or ""
        if not (title or snippet):
            continue
        # 摘要控制长度：3 条 × 120 字 ≈ 360 字，对提词/代答的 prompt 是可接受增量
        body = (snippet or title)[:120].replace("\n", " ")
        lines.append(f"- {title[:40]}：{body}")
    if not lines:
        return ""
    return (
        f"刚检索到的网页摘要（查询词：{q}，共 {len(lines)} 条）：\n"
        + "\n".join(lines)
        + "\n这些是外部检索结果，可能有错。只采用与问题直接相关的部分；"
        "冲突时以对话里说过的为准；不确定就照旧说「我确认一下」。"
    )


# --------------------------------------------------------------------------- #
# 预取：把「检索延迟」从用户等待里挪走
# --------------------------------------------------------------------------- #
# 为什么需要它（2026-10-22 网关实测 + 我自己的探针共同证明）：
#   到 Tavily 光是网络往返就 **1~1.2 秒**（用无效 key 拿 401 都要这么久），
#   真正的搜索通常 1~2 秒。而用户点「让它说」之后是**在等着听**，
#   多等 1.5 秒和在文字面板上多等 1.5 秒完全不是一回事。
#
#   所以正确做法不是"把超时压到 900ms"（那等于 100% 失败，而且静默），而是：
#   **听到像问句的台词就后台先查**，结果进缓存；等真要回复时命中缓存 = 0ms。
#
# 三条约束（防止预取变成额度黑洞）：
#   ① 只在 `should_search()` 为真时预取（客套、天气、含敏感词的都不预取）
#   ② 默认关闭，`WEB_SEARCH_PREFETCH=1` 才开（额度是共享的，不该悄悄消耗）
#   ③ 每场会话有上限（默认 12 次），超了就不再派新活
_PREFETCH_MAX_PER_SESSION = _env_int("WEB_SEARCH_PREFETCH_MAX", 12)
_prefetch_lock = threading.Lock()
_prefetch_state: dict[str, int] = {"count": 0}
_prefetch_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prefetch")


def prefetch_enabled() -> bool:
    return _env_flag("WEB_SEARCH_PREFETCH", False) and enabled()


def prefetch(text: str, session: str = "") -> bool:
    """把一句台词丢到后台去查。**立即返回，绝不阻塞调用方。**

    返回 True 表示"已经派了活"（不代表查到了东西）。
    同一个查询在缓存有效期内不会重复发请求（`search()` 内部有 5 分钟 TTL），
    所以反复问同一件事只消耗一次额度。
    """
    if not prefetch_enabled() or not should_search(text):
        return False
    q = extract_query(text)
    if not q:
        return False
    with _prefetch_lock:
        if _prefetch_state["count"] >= _PREFETCH_MAX_PER_SESSION:
            return False
        _prefetch_state["count"] += 1
        seq = _prefetch_state["count"]
    _prefetch_pool.submit(_prefetch_one, q, session, seq)
    return True


def _prefetch_one(query: str, session: str, seq: int) -> None:
    """后台任务：查一次，结果自然进 _CACHE。异常全部吞掉（后台任务不能炸）。"""
    try:
        rows = search(query, count=3)
        logger.info("预取检索 #%d %r → %d 条%s",
                    seq, query[:30], len(rows), f"（{session}）" if session else "")
    except Exception as exc:      # noqa: BLE001 后台任务不允许抛出
        logger.debug("预取失败 %r：%s", query[:30], exc)


def reset_prefetch() -> None:
    """每场录音开始时调用，重置预取配额。"""
    with _prefetch_lock:
        _prefetch_state["count"] = 0


def prefetch_stats() -> dict:
    with _prefetch_lock:
        return {"used": _prefetch_state["count"],
                "max": _PREFETCH_MAX_PER_SESSION,
                "enabled": prefetch_enabled()}
