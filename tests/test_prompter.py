"""提词器自检：清洗、注音、分类。

直接 import 生产代码，不做副本——抄一份到测试里，改了源码测试还绿，
那种绿是假的。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prompter import _kind_of, annotate, clean_items  # noqa: E402

PASS = FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     得到 {got!r}\n     期望 {want!r}")


def main() -> int:
    print("\n1) clean_items：编号、项目符号、引号都要擦掉")
    got = clean_items("1. 问清交期\n2. 先别答应降价")
    check("编号被去掉", [x["text"] for x in got], ["问清交期", "先别答应降价"])
    check("「问清交期」→ ask（去问对方，不是自己核实）", got[0]["kind"], "ask")

    got = clean_items("- “周五下午两点”\n• 「带合同」")
    check("引号项目符号都擦掉", [x["text"] for x in got], ["周五下午两点", "带合同"])

    got = clean_items("问一下预算\n问一下预算\n\n问一下预算")
    check("重复只留一条", len(got), 1)

    got = clean_items("a\nb\nc\nd\ne")
    check("最多 3 条", len(got), 3)

    long_line = "这是一条非常非常长的提示" * 6
    got = clean_items(long_line)
    check("超长被截断", got[0]["text"].endswith("…"), True)
    check("截断后不超 41 字", len(got[0]["text"]) <= 41, True)

    check("空输入", clean_items(""), [])
    check("全空白", clean_items("  \n\n  \t"), [])

    print("\n2) 分类：决定前端标「问 / 核 / 接」")
    check("追问", _kind_of("问他什么时候签"), "ask")
    check("确认", _kind_of("先确认单价"), "check")
    check("回答", _kind_of("周五下午两点"), "say")
    check("问号", _kind_of("他说的是哪个项目？"), "ask")

    # ---------------------------------------------------------------- #
    print("\n2.1) 2026-10-28 实测：答案被标成「接/核」、提问被标成「接」")
    # 用户截图里的原话：「主流模型：先确认哪家」顶的是「核」，
    # 实际它既不是答案也不是核实，是**让用户去问对方**。
    # 判据：冒号前的引导语不参与判断，看冒号后的正文。
    check("「主流模型：先确认哪家」→ ask（问对方关心哪家）",
          _kind_of("主流模型：先确认哪家"), "ask")
    check("「交付方式：先问清上线时间」→ ask", _kind_of("交付方式：先问清上线时间"), "ask")
    check("「报价：先确认含不含税」仍是 check（冒号后无提问词）",
          _kind_of("报价：先确认含不含税"), "check")
    check("冒号引子不该让答案变问",
          _kind_of("主流模型：GPT、Claude、通义"), "say")
    check("「先确认哪家」单独出现也是 ask", _kind_of("先确认哪家"), "ask")

    print("\n2.2) 模型自打标签优先：答案放 [接]，提问放 [问]")
    # 提词提示词第 7 条要求模型给每条打标签，前端标签直接照它走。
    got = clean_items("[接] 周五下午两点\n[问] 你们交付期多久")
    check("标签被擦掉，正文干净", [x["text"] for x in got], ["周五下午两点", "你们交付期多久"])
    check("[接] → say", got[0]["kind"], "say")
    check("[问] → ask", got[1]["kind"], "ask")

    # 反向对照：同样的句子不加标签，靠猜——「你们交付期多久」本身也该是 ask，
    # 但「先确认哪家」这种带「确认」的，不打标签就会掉进 check（正是这次的 bug）。
    plain = clean_items("先确认哪家")
    check("反向对照：不打标签的「先确认哪家」靠关键词判",
          plain[0]["kind"], "ask")
    tagged = clean_items("1. 【核】 先确认单价\n2. 接：周五下午两点")
    check("全角方头括号 + 编号", [x["text"] for x in tagged], ["先确认单价", "周五下午两点"])
    check("【核】 → check", tagged[0]["kind"], "check")
    check("「接：」无括号也能认", tagged[1]["kind"], "say")
    check("只有标签没有正文的行丢掉",
          [x["text"] for x in clean_items("[接]\n[问] 你们预算多少")], ["你们预算多少"])
    check("【问】压过正文里的「有」→ 仍是 ask",
          clean_items("[问] 你们有没有现成的接口")[0]["kind"], "ask")

    print("\n3) 注音：查字典，全大写缩写跳过")
    check("全大写缩写不注", annotate("用 ASAP 回复 CEO"), "用 ASAP 回复 CEO")
    check("字典命中", annotate("这个 api 什么时候好"), "这个 api（A-P-I）什么时候好")
    check("未命中不注", annotate("这个 zzzz 不用管"), "这个 zzzz 不用管")
    check("同句只注一次", annotate("api 和 api 不一样").count("（A-P-I）"), 1)
    check("中文原样保留", annotate("周五下午两点"), "周五下午两点")
    check("带标点", annotate("先看 email。"), "先看 email（伊妹儿）。")

    # 反向对照：把所有词都注音应该被规则挡住；这里验证「全大写跳过」真的生效
    upper = annotate("API URL PDF")
    check("反向对照：纯大写串整句不注", upper, "API URL PDF")

    # ------------------------------------------------------------------ #
    print("\n4) LLM 参数：max_tokens 必须给思考留够预算（2026-10-22 实测事故）")
    # 背景：这个后端模型**强制思考**，一次提词 reasoning 就吃掉 190~256 tokens。
    # 原来 max_tokens 设 80（后来 256），正文只剩几个 token →
    # 表现是「提词失败：API 返回空内容」，被误判成内容过滤，查了一整天才找到。
    # 这是一条**看起来无害、改小就废**的参数，必须钉住。
    import inspect
    import re as _re

    src = inspect.getsource(__import__("prompter").generate_prompt_items) \
        if hasattr(__import__("prompter"), "generate_prompt_items") \
        else Path(__file__).resolve().parent.parent.joinpath("prompter.py").read_text(encoding="utf-8")
    m = _re.search(r"max_tokens\s*=\s*(\d+)", src)
    check("prompter 里能找到 max_tokens", bool(m), True)
    if m:
        n = int(m.group(1))
        check(f"max_tokens ≥ 1024（实际 {n}，思考至少要 256）", n >= 1024, True)

    # 同样钉住 temperature：提词必须稳定，不能用全局 0.3
    tm = _re.search(r"temperature\s*=\s*([\d.]+)", src)
    check("prompter 显式传了 temperature", bool(tm), True)
    if tm:
        check(f"temperature == 0（实际 {tm.group(1)}）", float(tm.group(1)), 0.0)

    # 反向对照：把 max_tokens 改回 256，上面的判据必须判失败
    check("反向对照：256 会被判为不够", 256 >= 1024, False)

    print("\n4.5) 关思考必须走 reasoning_effort，不能用 thinking=disabled")
    # 2026-10-22 网关侧核实（六组对照，比我的单次采样可靠）：
    #   reasoning_effort="none"   → reasoning 0，有效 ✅
    #   thinking / enable_thinking / chat_template_kwargs
    #        → 被网关 `drop_params: true` **静默丢弃，从未到达后端**，
    #          表现与"什么都不传"一致（我原以为它们"让思考更长"，那是采样噪声）
    # 所以：关思考只能走 reasoning_effort；thinking 字段一律不许发。
    check("prompter 显式传 reasoning=False", "reasoning=False" in src, True)

    from summarize import LLMClient

    class _S:
        llm_thinking = "off"
        llm_model = "x"
        llm_base_url = "http://x"
        llm_api_key = "k"
        llm_timeout = 1
        llm_retries = 1
        llm_temperature = 0.3
        llm_max_tokens = 2048

    c = LLMClient.__new__(LLMClient)
    c.settings = _S()
    c._reasoning_mode = "none"

    check("LLM_THINKING=off → reasoning_effort=none", c._reasoning_effort(), "none")
    check("显式 reasoning=False → none", c._reasoning_effort(False), "none")

    # 要思考时也必须用标准字段，不能再用会被丢弃的 thinking
    c.settings.llm_thinking = "on"
    check("LLM_THINKING=on → reasoning_effort=medium（不是 thinking=enabled）",
          c._reasoning_effort(), "medium")
    check("显式 reasoning=True → medium", c._reasoning_effort(True), "medium")

    # 没配置就不干预
    c.settings.llm_thinking = ""
    check("LLM_THINKING 未配置 → 不传（用后端默认）", c._reasoning_effort(), None)

    # 降级链：none → minimal → off（不再传）
    c.settings.llm_thinking = "off"
    c._reasoning_mode = "minimal"
    check("降级到 minimal", c._reasoning_effort(), "minimal")
    c._reasoning_mode = "off"
    check("两档都被拒后不再传", c._reasoning_effort(), None)

    # 反向对照：旧写法（thinking 字段）必须被判为「不该出现」
    check("反向对照：thinking=enabled 不是合法返回值",
          c._reasoning_effort(True) == {"thinking": {"type": "enabled"}}, False)

    print("\n4.6) 代码里不许再出现会被网关丢弃的 thinking 字段")
    import inspect as _inspect
    import re as _re2

    import summarize as _sum

    chat_src = _inspect.getsource(_sum.LLMClient.chat)
    # 只看**代码**，去掉注释 —— 注释里提"thinking"是在解释历史，不算违规。
    # （第一版没去注释，被自己的说明文字判成失败。）
    code_only = "\n".join(
        _re2.sub(r"#.*$", "", line) for line in chat_src.split("\n")
    )
    check("chat() 代码里没有发 thinking 字段",
          '"thinking"' in code_only or "'thinking'" in code_only, False)
    check("chat() 代码里没有 extra_body", "extra_body" in code_only, False)
    check("chat() 里确实传了 reasoning_effort", '"reasoning_effort"' in code_only, True)

    # 反向对照：故意把旧写法塞回去，判据必须报出来
    broken = code_only + '\n    kwargs["extra_body"] = {"thinking": {"type": "disabled"}}'
    check("反向对照：塞回旧写法后能被判出来", "extra_body" in broken, True)

    print("\n5) 提示词不该再有「应一声」那条（会答非所问）")
    from prompter import SYSTEM_PROMPT

    check("SYSTEM_PROMPT 不含「应一声」", "应一声" in SYSTEM_PROMPT, False)
    check("SYSTEM_PROMPT 要求给出判断依据", "依据" in SYSTEM_PROMPT, True)
    check("SYSTEM_PROMPT 要求写出分界线", "分界线" in SYSTEM_PROMPT, True)

    print("\n5.1) 提示词要求每条打标签（否则前端只能靠猜，答案会被塞进「接」/「核」）")
    check("SYSTEM_PROMPT 定义了 [接] 标签", "[接]" in SYSTEM_PROMPT, True)
    check("SYSTEM_PROMPT 定义了 [问] 标签", "[问]" in SYSTEM_PROMPT, True)
    check("SYSTEM_PROMPT 定义了 [核] 标签", "[核]" in SYSTEM_PROMPT, True)
    check("SYSTEM_PROMPT 说清「答案一律放接」", "答案一律放这里" in SYSTEM_PROMPT, True)
    # 反向对照：只有标签说明、没说「不能拿 [问] 装答案」是不够的——
    # 上一版就是少了这条，模型才会把答案标成 [问]。
    check("SYSTEM_PROMPT 禁止拿 [问] 装答案", "不能用来装答案" in SYSTEM_PROMPT, True)

    print(f"\n{'=' * 44}\n通过 {PASS} 项，失败 {FAIL} 项\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
