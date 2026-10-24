"""语音分身的「接一句」：把最近几轮对话交给模型，产出一句能直接念出来的稿子。

设计上刻意只做**一轮**：生成 → 进台词队列 → 播出去 → 交还。
不主动接话、不连续自说，避免抢话；要不要接着说由使用者的下一条指令决定。

稿子的生命周期完全交给前端的台词队列（预览 / 逐句进度 / 打断作废），
这里只管「写什么」，不管「怎么播」。
"""

from __future__ import annotations

import logging
import re
import time

from config import Settings

logger = logging.getLogger("assistant")

SYSTEM_PROMPT = """你是用户的语音分身，正在替他在电话或语音里跟人对话。

硬性要求：
1. 用第一人称、口语，像真人接电话；不要书面语，不要分点，不要表情符号。
2. 一到三句话，总共不超过 80 个字。宁可短，不要长——你是在说话，不是在写文章。
3. **能答的就直接答**：常识、天气怎么穿、日程安排、人情客套、对方明确问到的、
   以及你能从对话里推出来的，都正面回答，不要动不动就说「我确认一下」。
4. 只有这三种情况才说「这个我确认一下再回你」：
   a) 涉及具体数字、金额、时间、承诺，而对话里没出现过；
   b) 需要查实时信息（今天天气、股价、新闻），你手上没有；
   c) 你不确定对方指的是哪件事。
5. 绝对不许编造事实、数字、时间、承诺。宁可说不知道，也不要给一个像真的假答案。
6. 不要复述对方的话，不要「好的我明白了」这类空话，直接给答案。
7. 不要提「根据上文」「作为助理」之类的话。
8. 只输出要念出来的那句话本身：不要引号、不要「答：」、不要解释。

如果对方是在叫你（比如喊「语音助理」「助理」），就直接接话，别反问是谁。
这是连续对话：下面会告诉你「对方刚才说了什么」和「你自己上一句说了什么」，
接着往下聊就行，不要重新自我介绍，也不要把已经说过的话再说一遍。

如果下面附了「刚查到的天气」，那就是最新数据，直接用来说，
不要再说「我去查一下」——你已经查到了。
**里面的温度区间和降雨概率必须逐字照抄**：不许四舍五入、不许自己换算、
不许说「六成多」这种概括、不许加「左右」。数字写错比不说更糟。

如果下面附了「刚检索到的网页摘要」，说明你查过了：
- 只用与对方问题**直接相关**的部分，一句带过，不要念标题、不要说「根据搜索」
- 摘要之间互相冲突就挑最可信的一条；拿不准仍然说「我确认一下」
- **摘要里没有的，不许补**。宁可说没查到，也不要拿训练记忆冒充刚查到的
- 摘要可能是过期网页：涉及数字/政策/价格时，若摘要里没写明发布时间，按「可能过时」处理

__DIALECT_HINT__"""

# 方言提示只有**配了家乡**才追加。
#
# 为什么不能留在 SYSTEM_PROMPT 里写死：那会把「说话人是哪里人」每轮发给模型，
# 等于把地理位置写进每一条请求；而绝大多数用户不需要它，
# 开源仓更不该带某个人的家乡。
DIALECT_HINT = """说话人来自{hometown}一带。如果对方用方言，可以用同样的方言词回应，
但只在你有把握时用；不确定的词就用普通话，不要为了像而硬造。"""


def build_system_prompt(hometown: str = "") -> str:
    """拼系统提示词。`hometown` 为空则整个方言段不出现（默认行为）。

    单独抽成函数是为了可测：测试要能断言「没配家乡时提示词里没有编造的地名」，
    而不用去猜调用点。
    """
    hint = ""
    ht = (hometown or "").strip()
    if ht:
        hint = DIALECT_HINT.format(hometown=ht)
    return SYSTEM_PROMPT.replace("__DIALECT_HINT__", hint).strip()


# 这些前缀模型偶尔会带出来，念出来很怪
_PREFIX_RE = re.compile(r"^(答|回复|我|助理)\s*[:：]\s*")
_WEEKDAY = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def format_lines(lines: list[dict], limit: int = 14, me: str = "我", peer: str = "对方") -> str:
    """把最近几轮实时转写排成「我：… / 对方：…」的样子。

    `lines` 来自服务端 LiveSession.lines，每项形如 {start, text, who}。
    没有 who 的（单声道、或老数据）统一按「未标注」处理，让模型自己判断。
    """
    rows = [x for x in (lines or []) if str(x.get("text") or "").strip()][-limit:]
    out = []
    for x in rows:
        who = str(x.get("who") or "").strip()
        text = str(x.get("text") or "").strip()
        if who:
            out.append(f"{who}：{text}")
        else:
            out.append(f"（未标注说话人）：{text}")
    return "\n".join(out)


def clean_reply(text: str, max_chars: int = 120) -> str:
    """擦掉模型带出来的引号、前缀、换行，压成一句能直接念的话。"""
    s = str(text or "").strip()
    if not s:
        return ""
    s = s.replace("\r", "\n")
    s = "\n".join(x.strip() for x in s.split("\n") if x.strip())
    s = _PREFIX_RE.sub("", s)
    # 整句被引号包住的情况
    for a, b in (("“", "”"), ('"', '"'), ("「", "」"), ("'", "'")):
        if len(s) > 2 and s.startswith(a) and s.endswith(b):
            s = s[1:-1].strip()
    s = re.sub(r"\s+", "", s)  # 中文口语不需要空格
    if len(s) > max_chars:
        s = s[:max_chars].rstrip("，。、,.") + "。"
    return s


def draft_reply(
    lines: list[dict],
    settings: Settings,
    ask: str = "",
    max_chars: int = 120,
    spoken: list[str] | None = None,
    scenario: str = "",
) -> dict:
    """根据最近几轮对话，给出一句可以直接念的回复。

    `spoken` 是「助理自己刚才已经说出去的话」——上一轮播报的台词。
    把它带进上下文，模型才知道聊到哪儿了，不会每轮都重新自我介绍。
    `scenario` 决定检索哪个场景包（开会 / 面试 / 谈判 / 电话），空则用默认档。
    返回 {"text": ..., "model": ..., "usage": {...}, "context_lines": n}
    """
    from datetime import datetime

    from summarize import LLMClient

    started = time.perf_counter()
    context = format_lines(lines)
    if not context and not ask.strip():
        return {"text": "", "model": "", "usage": {}, "context_lines": 0,
                "error": "还没有对话内容"}

    user = []
    now = datetime.now()
    user.append(f"现在时间：{now:%Y年%m月%d日} {_WEEKDAY[now.weekday()]} {now:%H:%M}")
    hometown = str(getattr(settings, "assistant_hometown", "") or "").strip()
    if hometown:
        user.append(f"说话人的家乡：{hometown}（方言用词参考，不一定每句都用）")

    # 实时信息按需现查：只有问到天气才发一次请求，其余零成本。
    # 查不到就什么都不加，模型会按「手上没有」处理，不会编。
    try:
        from tools import maybe_weather

        live = maybe_weather(" ".join([context, ask]), getattr(settings, "assistant_city", ""))
        if live:
            user.append("刚查到的天气（实时数据，可以直接用）：\n" + live)
    except Exception:
        pass
    # 联网检索（可选能力，2026-10-22 接入）。
    #
    # 三个约束，都写在这里而不是散在别处：
    #   ① **没配 key 就完全不工作** —— enabled() 假 → maybe_search 立刻返回空，
    #      行为与接入前完全一致。所以这是个纯增量能力。
    #   ② **只发关键词，不发上下文** —— maybe_search 内部用 extract_query
    #      剥掉指代/客套/语气词，再截到 60 字；含身份证/合同号等敏感词的整句不发。
    #      原始对话永远不出本机。
    #   ③ **超时短**（默认 0.9s）—— 代答预算 1~3 秒，查得慢比查不到更伤体验。
    #
    # ⚠️ 只接在**代答**，**不接提词**：提词目标 <1s，加检索会到 2s 以上，
    # 用户卡住时等 2 秒等于没有。纪要（30s 级）才是最适合接检索的地方。
    try:
        from websearch import maybe_search

        web = maybe_search(ask or context)
        if web:
            user.append(web)
    except Exception:
        pass
    # 个人知识：检索不到就不加，检索出错也静默跳过——知识库不该成为代答的故障点。
    try:
        from kb_bridge import context_for

        kb = context_for(lines, ask, scenario=scenario)
        if kb:
            user.append(kb)
    except Exception:
        pass
    if spoken:
        said = "\n".join(f"你：{x}" for x in spoken[-4:] if str(x).strip())
        if said:
            user.append("你自己刚才已经说过的（对方听到了，不要重复）：\n" + said)
    if context:
        user.append("最近的对话（按时间顺序，越靠后越新）：\n" + context)
    if ask.strip():
        user.append("现在需要你回应的内容：\n" + ask.strip())
    user.append("请只输出你要说的那一句话。")

    client = LLMClient(settings)
    text = client.chat(
        [
            {"role": "system", "content": build_system_prompt(hometown)},
            {"role": "user", "content": "\n\n".join(user)},
        ],
        # 160 不够：模型会思考时 reasoning 一次就吃掉 100~250 token，正文被挤没
        # （2026-10-22 实测，提词那边同样的坑）。答案短靠 prompt 约束，不靠掐 token。
        max_tokens=1024,
        # 用户喊完「助理」在等着听，不能让它思考：实测能省 30% 延迟。
        reasoning=False,
    )
    out = clean_reply(text, max_chars)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info("代答草稿：%s（上下文 %d 句 / 自己说过 %d 句 / 思考 %dms）",
                out[:60], len(lines or []), len(spoken or []), elapsed_ms)
    return {
        "text": out,
        "model": settings.llm_model,
        "usage": dict(client.usage),
        "context_lines": len(lines or []),
        "elapsed_ms": elapsed_ms,
    }
