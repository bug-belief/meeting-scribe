"""提词器：在实时对话里给使用者一句「你可以这么接」。

和 `assistant.draft_reply` 的分工：

- 代答（draft_reply）：写出一整句能直接念出去的台词，进 TTS 队列。
- 提词（本模块）：给出极短的提示，**由使用者自己说**。不合成、不出声。

所以这里的输出刻意不写成完整句子——写成句子，人就只会照着念，反而更僵。
另外它比代答便宜一个量级：输出 token 少，且不触发 TTS（实测 TTS 才是成本大头）。

不主动轮询：前端在「一句定稿」之后防抖 1.2 秒才发一次请求，
对方还在说的时候不发，省下的都是真金白银。
"""

from __future__ import annotations

import logging
import re
import time

from config import Settings

logger = logging.getLogger("prompter")

SYSTEM_PROMPT = """你是用户的随身提词器。他正在电话或语音里跟人实时对话，
你要在他需要开口的时候，给他一句极短的提示。

硬性要求：
1. **不是台词，是提示**。写成便签式的几个字，不要「我觉得」「您可以」这种完整句子。
2. 最多 2 条，每条不超过 16 个字。宁可只给一条，也不要凑数。
3. 只给方向：回答什么、补哪个数、追问什么、先稳住/先别答应。
4. 对话里没出现过的数字、金额、时间、承诺，一律提示「先确认」，绝对不许编。
5. 不要寒暄，不要「好的」「明白了」这类空话，不要复述对方原话。
6. 只输出提示本身，每条一行，不要编号、不要引号、不要任何解释。
7. **每条前面必须带一个标签**，从 `[接]` `[问]` `[核]` 里选一个——这是给用户
   一眼分清「该说什么」的，标错了比多给一条更糟：
   - `[接]`：给**内容**，用户照着说出去就行（回答要点、观点、话术）。
     对方刚问了一个能答的问题，答案一律放这里（比如「[接] 周五下午两点」）。
   - `[问]`：给**该问对方的话**，是用户要对对方说的提问。
     不能用来装答案，也不等于「这条在回答别人的提问」。
   - `[核]`：让用户先去查证/确认、别急着承诺，核完再答。
   - ❌ 最常见的错：「主流模型：先确认哪家」——这既不是答案，也不像提问，还标了 `[核]`。
     要么给一个真答案 `[接] 先讲 GPT / Claude 这几家`，
     要么直接问清 `[问] 你关心哪类任务`。

**判断要有依据，不要模棱两可**（2026-10-21 实测教训：同一个问题「回滚和压缩哪个便宜」
连问四次给了四个相反结论，这种提示等于没提）。
- 先给一句定论，再给判断依据，让用户自己能核验；
- 依据必须是对话里出现过的条件（比如「要重跑就压缩」），不要凭空引入新前提；
- 确实分情况时，把分界线写出来（「能丢就回滚，要续就压缩」），不要只说「看情况」。"""

# 模型偶尔会带的编号、破折号、项目符号
_LEAD_RE = re.compile(r"^\s*(?:[-*·•]|\d+[.、)）]|[（(]\d+[)）])\s*")
# 模型按 SYSTEM_PROMPT 第 7 条给的标签。同时容忍全角括号、方头括号、漏括号——
# 模型经常写成「接：」「【问】」「[问] 」三种混着来。
# ⚠️ 反例（第一版踩的）：`[：:]?` 让「问清交期」里的「问」被当成标签吃掉，
# 提示就成了「清交期」——**裸字标签必须要求后面有分隔符**。
# `(?![问清题候])` 挡掉「问清/问题/问候」这类以「问」开头却不是标签的词。
_TAG_RE = re.compile(
    r"^\s*(?:[\[【(（]\s*(接|问|核)\s*[\]】)）]"
    r"|(接|问|核)(?![问清题候])\s*[：:、.,，]"
    r")\s*")
_TAG_KIND = {"接": "say", "问": "ask", "核": "check"}
_QUOTE_PAIRS = (("“", "”"), ('"', '"'), ("「", "」"), ("『", "』"), ("'", "'"))
_MAX_ITEMS = 3
_MAX_CHARS = 40


def clean_items(text: str, limit: int = _MAX_ITEMS) -> list[dict]:
    """把模型输出擦成若干条干净提示。

    只保留前 `limit` 条；每条去掉编号/引号/句末标点，超过 40 字直接截断——
    提词长了就没人看，还不如不显示。

    条目的 kind 由模型自己的标签决定（`[接]`/`[问]`/`[核]`），擦掉标签后
    再回落到关键词猜测：模型经常不标，两句都要能跑。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for raw in str(text or "").replace("\r", "\n").split("\n"):
        line = _LEAD_RE.sub("", raw).strip()
        if not line:
            continue
        # 标签要在擦编号之后、擦引号之前取：模型会写成 `1. [接] xxx` 或 `[接] “xxx”`
        tag_kind = ""
        tag = _TAG_RE.match(line)
        if tag:
            tag_kind = _TAG_KIND[tag.group(1) or tag.group(2)]
            line = line[tag.end():].strip()
            if not line:
                continue
        for a, b in _QUOTE_PAIRS:
            if len(line) > 1 and line.startswith(a) and line.endswith(b):
                line = line[1:-1].strip()
        line = line.strip(" 　\t，。；、,.;:：!！?？")
        if not line:
            continue
        if len(line) > _MAX_CHARS:
            line = line[:_MAX_CHARS].rstrip("，。、,.") + "…"
        key = re.sub(r"\s+", "", line)
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": line, "kind": tag_kind or _kind_of(line)})
        if len(out) >= limit:
            break
    return out


# 「X：Y」形式的冒号前引导语。冒号前的部分只是个题目，该不该判成「提问」
# 取决于冒号后的正文——「主流模型：先确认哪家」冒号前没问号，却是个提问。
_COLON_RE = re.compile(r"^[^：:]{1,10}[：:]\s*")


def _kind_of(text: str) -> str:
    """粗略分类，前端按这个上色（追问 / 确认 / 回答 / 接话）。

    只在模型没打标签时用（见 `clean_items`），所以宁可保守：
    判不准就 `say`，让用户自己读那句提示。
    """
    if any(word in text for word in ("别答应", "先别说")):
        return "check"
    # 冒号前的引导语不参与判断。原文整句扫关键词时，
    # 「主流模型：先确认哪家」因为含「确认」被判成 check——
    # 可这句话是在**让用户去问对方**，界面上顶个「核」，
    # 用户就真以为答完了、只是要核一下。
    body = _COLON_RE.sub("", text)
    # 「先确认X」后面跟着 哪/多少/什么 这类疑问词时，「确认」是想让对方说清，
    # 不是自己回去查证 → ask。这一条必须在 check 之前判。
    if any(w in body for w in ("哪家", "哪个", "哪方面", "多少", "几个", "多久",
                               "什么", "是不是", "有没有")):
        return "ask"
    # 「问清 / 问一下 / 追问」是**去问对方**，ask；单写「确认」才是回去核实，check。
    # （2026-10-28：「先问清上线时间」原来被 check 抢走，界面上顶个「核」，
    #   用户看着像「已经答了、只待核对」，实际一个字没答。）
    if any(word in body for word in ("问清", "问一下", "追问")):
        return "ask"
    if any(word in body for word in ("确认", "核实")):
        return "check"
    if any(word in body for word in ("问", "？", "?")):
        return "ask"
    return "say"


def suggest(
    lines: list[dict],
    settings: Settings,
    ask: str = "",
    spoken: list[str] | None = None,
    max_items: int = _MAX_ITEMS,
    scenario: str = "meeting",
) -> dict:
    """根据最近几轮对话给提词。返回 {"items": [...], "usage": {...}, ...}。

    默认走 `meeting` 场景：提词天生是「坐在会议里」用的，宁窄勿宽——
    面试场里冒出客户项目的细节比少给一条提示糟得多。
    """
    from datetime import datetime

    from assistant import format_lines
    from summarize import LLMClient

    started = time.perf_counter()
    # 提词只看最近几句：给整段历史既贵又会让提示变笼统
    context = format_lines(lines, limit=10)
    if not context and not ask.strip():
        return {"items": [], "model": "", "usage": {}, "context_lines": 0,
                "error": "还没有对话内容"}

    parts: list[str] = []
    now = datetime.now()
    weekday = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
    parts.append(f"现在时间：{now:%Y年%m月%d日} {weekday[now.weekday()]} {now:%H:%M}")

    try:
        from tools import maybe_weather

        live = maybe_weather(" ".join([context, ask]), getattr(settings, "assistant_city", ""))
        if live:
            parts.append("刚查到的天气（可直接用作提示里的数字）：\n" + live)
    except Exception:
        pass

    # 提词比代答更吃「事实」——要给的是能接得住的具体信息，不是空话。
    # 检索不到就不加，出错静默跳过。
    try:
        from kb_bridge import context_for

        kb = context_for(lines, ask, scenario=scenario)
        if kb:
            parts.append(kb)
    except Exception:
        pass

    if spoken:
        said = "\n".join(f"你：{x}" for x in spoken[-3:] if str(x).strip())
        if said:
            parts.append("你已经说过的（别重复）：\n" + said)
    if context:
        parts.append("最近的对话（越靠后越新）：\n" + context)
    if ask.strip():
        parts.append("他现在想问的是：\n" + ask.strip())
    parts.append("请只输出给他的话术提示，最多 2 行，每行以 [接] / [问] / [核] 开头。")

    client = LLMClient(settings)
    text = client.chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
        # ⚠️⚠️ max_tokens 必须 ≥1024，否则必然截断 —— 原因不是「答案太长」，是**模型会思考**。
        #
        # 2026-10-22 实测（同一问题连问多次，抓原始响应）：
        #   completion_tokens: 256
        #   ├─ reasoning_tokens: 248   ← 思考吃掉 97%
        #   └─ content: "结论：多数场景下压缩更"   ← 只剩 8 个 token 写正文
        #   finish_reason: "length"
        #
        # 「API 返回空内容」也是同一个原因：思考把它全吃光，content 直接是空的。
        # 原来设 80，等于几乎必然失败 —— 日志里那些「提词失败：API 返回空内容」
        # 就是这个，跟内容过滤毫无关系。
        #
        # 试过三种关闭方式，**只有 reasoning_effort 有效**（2026-10-22 补测）：
        #   reasoning_effort="none"           → reasoning 0，958ms   ✅ 快了 30%
        #   thinking={"type":"disabled"}      → reasoning 1763 字，content 0 字 ❌ 反而更糟
        #   enable_thinking=False             → reasoning 250，content 只剩 9 字 ❌
        #   chat_template_kwargs              → content 0 字 ❌
        # 注意项目 .env 里 LLM_THINKING=off 原来走的是 thinking=disabled 那条**最差**路径，
        # 所以「已经在关思考」的配置其实一直在帮倒忙。现在统一改走 reasoning_effort。
        #
        # 实测各档耗时（1024 反而比 256 快，因为不用重试）：
        #   256 → 2353ms（常截断）｜1024 → 1926ms｜2048 → 2048ms
        # 「短」靠 prompt 约束，不靠掐 token。
        max_tokens=1024,
        # 提词是给人照着说的：同一个问题必须给同一个答案。
        # 原来用全局 0.3，同一个问题连问四次得到四个相反结论（见 chat 的注释）。
        temperature=0.0,
        # 用户正在等着开口，不能让它思考：实测 1372ms → 958ms。
        reasoning=False,
    )
    items = clean_items(text, max_items)
    # 注音版本一起下发：前端切换开关时不用再请求一次，也不再多花 token
    for item in items:
        item["phonetic"] = annotate(item["text"])
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info("提词：%s（%d 句上下文 / %dms）",
                " ｜ ".join(i["text"] for i in items)[:70], len(lines or []), elapsed_ms)
    return {
        "items": items,
        "model": settings.llm_model,
        "usage": dict(client.usage),
        "context_lines": len(lines or []),
        "elapsed_ms": elapsed_ms,
    }


# --------------------------------------------------------------------------- #
# 英文注音：查字典，不调模型
# --------------------------------------------------------------------------- #
# 中文里夹英文，念出来容易卡。这里给常见词的近似读法——查表是 0 成本，
# 让模型去「注音」既贵又不可控（同一个词能给三种念法）。
PHONETIC: dict[str, str] = {
    "api": "A-P-I",
    "url": "U-R-L",
    "app": "阿普",
    "bug": "巴格",
    "email": "伊妹儿",
    "mail": "妹儿",
    "server": "瑟佛",
    "service": "瑟维斯",
    "token": "偷肯",
    "model": "莫豆",
    "prompt": "普荣普特",
    "demo": "呆摩",
    "design": "底赞",
    "meeting": "米停",
    "review": "瑞 Viu",
    "deploy": "底普洛伊",
    "deadline": "呆德赖恩",
    "budget": "巴吉特",
    "cost": "扣斯特",
    "price": "普赖斯",
    "quote": "阔特",
    "contract": "康吹克特",
    "okr": "O-K-R",
    "kpi": "K-P-I",
    "roi": "R-O-I",
    "prd": "P-R-D",
    "ui": "U-I",
    "ux": "U-X",
    "pdf": "P-D-F",
    "excel": "伊克塞尔",
    "word": "沃德",
    "ppt": "P-P-T",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]*")


def annotate(text: str) -> str:
    """给中文里的英文词补注音。

    规则（按顺序）：
    1. 全大写、长度 ≤ 5 的缩写（如 ASAP / CEO）跳过——念字母本身就够清楚，
       再注音反而绕。
    2. 命中字典才注，命中不了的不注。宁缺勿滥：乱注比不注更难念。
    3. 同一个词在一句里只注第一次，避免刷屏。
    """
    done: set[str] = set()

    def repl(match: re.Match) -> str:
        word = match.group(0)
        bare = word.rstrip(".,;:)!?")
        tail = word[len(bare):]
        low = bare.lower()
        if bare.isupper() and len(bare) <= 5:
            return word
        if low in done:
            return word
        hint = PHONETIC.get(low)
        if not hint:
            return word
        done.add(low)
        return f"{bare}（{hint}）{tail}"

    text = _WORD_RE.sub(repl, str(text or ""))
    # 注音括号后面常留一个空格（原文「api 什么时候」），中文之间那个空格多余
    return re.sub(r"）[ \t]+(?=[\u4e00-\u9fff])", "）", text)
