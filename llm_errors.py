"""LLM 调用的错误分类表（把上游一堆英文翻译成一句人话 + 一条建议）。

【为什么需要这张表】
上游（DeepSeek / 网关）出错时给的是一大段英文 JSON，例如：

    litellm.BadRequestError: OpenAIException - Insufficient Balance ...
    LiteLLM: model group '<你的模型组>' failed ... no fallback model group was found ...

这段东西直接透传到界面上（2026-10-22 真实发生过），用户看到的是一屏英文，
既不知道发生了什么，也不知道该做什么。而不同错误的**处置动作完全不同**：
余额不足要充钱、密钥失效要换 key、限流只要等一下、网关没起要去看进程。

所以这里做两件事：
  ① 归成有限的 kind（可枚举、可统计、可测试）
  ② 每类给「一句人话」+「一条建议」

【设计约束】
- 纯函数、零依赖（只用标准库），这样 summarize / api / 测试都能直接用，不会造循环导入。
- **特征匹配宁可漏判到 unknown，也不要误判**：比如 "connection timeout" 里有
  "timeout"，但它属于网络不可达而不是模型思考超时——所以 unreachable 的规则要排在
  timeout 前面，且 timeout 的正则要避开 connection 语境（见 _RULES 顺序与否定词）。
- 未识别的一律落到 `unknown`，并把原文截断展示（带长度上限，避免撑爆界面）。

【怎么加一类新错误】
在 _RULES 里插一行（顺序即优先级），然后：
  - tests/test_llm_errors.py 里加正例
  - 加一条**反例**：构造一个长得像但不该归到这里的字符串
  - docs/排错与验收.md §12 的「LLM 错误分类」表补一行
"""

from __future__ import annotations

import re

# kind → (人话, 建议)
#   人话：给用户看的，说「怎么了」，不说「哪个 SDK 抛了什么异常」
#   建议：告诉用户「现在能做什么」，没有可做的就写影响范围
CATALOG: dict[str, dict[str, str]] = {
    "no_balance": {
        "label": "AI 服务余额不足",
        "hint": "转写和归档不受影响；充值后立即恢复，无需重启服务",
    },
    "bad_key": {
        "label": "AI 服务鉴权失败（密钥无效或已过期）",
        "hint": "检查 .env 里的 LLM_API_KEY 是否有效",
    },
    "rate_limit": {
        "label": "请求太频繁，被限流了",
        "hint": "隔一两分钟再试",
    },
    "quota": {
        "label": "AI 服务调用额度已用尽",
        "hint": "额度恢复或换 key 后可用",
    },
    "unreachable": {
        "label": "连不上 AI 服务",
        "hint": "先用 curl 打一下 LLM_BASE_URL，确认服务/网关在跑、端口对得上",
    },
    "timeout": {
        "label": "AI 服务响应超时",
        "hint": "重试一次；持续超时看服务日志的长尾延迟",
    },
    "model_missing": {
        "label": "AI 服务没有可用的模型",
        "hint": "检查 LLM_MODEL 与网关里的 model group 是否对上",
    },
    "empty": {
        "label": "AI 返回了空内容",
        "hint": "通常是 max_tokens 被思考链吃完，加大预算而不是当成内容过滤",
    },
    "unknown": {
        "label": "AI 服务出错",
        "hint": "详情见 logs/summarize.log",
    },
}

# 顺序即优先级：先判「不可达」，再判「超时」——
# 否则 "connection timeout" 会被 timeout 先吃掉，而它的处置动作是去看进程，不是重试。
# 每条都是一个正则，对 str(exc).lower() 做 search。
_RULES: list[tuple[str, re.Pattern]] = [
    # 网关没起来 / 网络不通。放最前，因为它最容易被后面的 timeout 抢走。
    # ⚠️ `connection timeout` / `ConnectTimeout` 在这里算**不可达**而不是超时：
    #    urllib3 的 ConnectTimeout 是「连都没连上」（端口没开 / DNS 不通），
    #    处置动作是去看网关进程；而 ReadTimeout 才是「连上了但对面不回」，那才重试。
    #    这条区分是 tests/test_llm_errors.py 的反例逼出来的（最初写漏了 timeout）。
    ("unreachable", re.compile(
        r"connection\s+(refused|reset|aborted|error)"
        r"|connect(ion)?\s*time(d)?\s*out"
        r"|failed to establish a new connection"
        r"|name or service not known"
        r"|max retries exceeded"
        r"|econnrefused|econnreset|enotfound|connecttimeout"
        r"|网关(没|未)(启动|运行)"
    )),
    # 余额：DeepSeek 原文 "Insufficient Balance"，HTTP 402
    ("no_balance", re.compile(
        r"insufficient\s+balance|insufficient\s+quota|insufficient_quota"
        r"|余额不足|欠费"
        r"|\b402\b"
    )),
    # 密钥：401 / Invalid API key / authentication
    ("bad_key", re.compile(
        r"invalid\s+api\s*key|incorrect\s+api\s*key|authentication\s+error"
        r"|unauthorized|invalid_api_key"
        r"|api\s*key.*(invalid|expired)|密钥.*(无效|过期)"
        r"|\b401\b"
    )),
    # 模型组不存在 / 网关没配：错误信息里 "model group 'x' ... no fallback model group"
    ("model_missing", re.compile(
        r"no fallback model|model group .{0,40}(not found|failed)"
        r"|the requested model .{0,40}not available"
        r"|model_not_found|does not exist"
        r"|模型.*(不存在|不可用)"
    )),
    ("rate_limit", re.compile(
        r"rate\s*limit|too many requests|requests per (minute|second)"
        r"|限流|请求过于频繁|\b429\b"
    )),
    # 额度（与余额不同：多见于「套餐用尽 / 配额超限」）
    ("quota", re.compile(r"exceeded your current quota|quota exceeded|额度已用尽|\bquota\b")),
    # 超时：放在 unreachable 之后，所以纯 "connection timeout" 不会被它吃掉
    ("timeout", re.compile(
        r"timed?\s*out|timeout|read time ?out|deadline exceeded|超时"
    )),
    ("empty", re.compile(r"返回空内容|空内容|empty (content|response)|content.*is empty")),
]

MAX_DETAIL = 160


def classify(exc: object) -> dict[str, str]:
    """把异常（或它的字符串）归成一类。

    返回 `{"kind", "label", "hint", "detail"}`，永远不抛异常——
    错误处理本身再抛错会把原始问题彻底埋掉。
    """
    raw = exc if isinstance(exc, str) else str(exc or "")
    low = raw.lower()

    for kind, pattern in _RULES:
        if pattern.search(low):
            meta = CATALOG[kind]
            return {
                "kind": kind,
                "label": meta["label"],
                "hint": meta["hint"],
                "detail": _clip(raw),
            }

    meta = CATALOG["unknown"]
    return {
        "kind": "unknown",
        "label": meta["label"],
        "hint": meta["hint"],
        # 未识别时把原文带上，否则用户连线索都没有
        "detail": _clip(raw),
    }


def _clip(raw: str) -> str:
    """原文截断：界面上一屏英文没人读，但完全不给他又没法查。"""
    text = " ".join(str(raw or "").split())
    if len(text) <= MAX_DETAIL:
        return text
    return text[: MAX_DETAIL - 1] + "…"


def message(exc: object) -> str:
    """只要那一句人话（给不需要 hint 的调用方）。"""
    return classify(exc)["label"]
