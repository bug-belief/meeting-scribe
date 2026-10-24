"""助理时间线自检：不加载模型，纯时间区间运算。

    python tests/test_assistant.py

覆盖三种情况：
1. 重叠足够 → 整句判给助理（不能冒充真人）
2. 助理的话没进录音 → 按播报日志补进稿子，且只补实际播出的字数
3. 被打断后作废的事件 → 一个字都不进稿子
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transcribe import (  # noqa: E402
    ASSISTANT_LABEL,
    ASSISTANT_LABEL_SYS,
    ASSISTANT_SPEAKER,
    Segment,
    Transcript,
    apply_assistant_timeline,
    assistant_label,
    merge_turns,
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def segs_text(segments) -> list[tuple[float, int, str]]:
    return [(round(s.start, 1), s.speaker, s.text) for s in segments]


def main() -> int:
    people = [
        Segment(start=3.2, end=5.5, speaker=0, text="喂喂。能听到吗？"),
        Segment(start=14.7, end=17.1, speaker=0, text="试一下试一下语音发送。"),
    ]
    events = [
        # 助理说的话，录音里完全没有（合成音走虚拟声卡，不回环）
        {"id": "u1", "start": 10.0, "end": 14.0, "spoken_chars": 19,
         "text": "我现在不方便说话，先让我的声音替我说。", "state": "done"},
        # 与 14.7~17.1 重叠 2.0s / 2.4s ≈ 83% → 整句判给助理
        {"id": "u2", "start": 15.0, "end": 17.0, "spoken_chars": 3,
         "text": "你能听见吗？", "state": "partial"},
        # 被打断后作废：不该出现在任何地方
        {"id": "u3", "start": 20.0, "end": 22.0, "spoken_chars": 0,
         "text": "这句没播出去。", "state": "cancelled"},
    ]

    out, marked, injected = apply_assistant_timeline(people, events)

    print("结果：")
    for row in segs_text(out):
        print("   ", row)
    print(f"  改写 {marked} 句，补充 {injected} 句")

    check("一共 3 段（2 段人声 + 1 段补进来的助理台词）", len(out) == 3, f"实际 {len(out)}")
    check("第 2 句被判定为助理", out[1].speaker == ASSISTANT_SPEAKER, str(out[1].speaker))
    check("第 1 句仍是真人", out[0].speaker == 0, str(out[0].speaker))
    check("补进来的台词按时间排在中间", out[1].start == 10.0, str(out[1].start))
    check("补进来的文本截到实际播出字数（19 字）",
          len(out[1].text) == 19, f"{len(out[1].text)}：{out[1].text}")
    check("作废的那句没进来",
          all("没播出去" not in s.text for s in out))

    # 边界：没有事件时原样返回
    same, m0, i0 = apply_assistant_timeline(people, [])
    check("无事件时不改动", m0 == 0 and i0 == 0 and len(same) == 2)

    # 纠字：ASR 会把克隆音听错（11到17度 → 一七度，69% → 六和九）。
    # 播报日志里存的是原文，必须用它替换 ASR 文本，而不是只换个标签。
    typo = [Segment(30.0, 35.0, 0, "明天有小雨阴天到一七度降雨概率六和九")]
    exact = "明天有小雨，11到17度，降雨概率69%"
    ev_typo = [{"id": "t", "start": 30.0, "end": 35.0, "text": exact,
                "spoken_chars": len(exact), "state": "done"}]
    fixed, m1, i1 = apply_assistant_timeline(typo, ev_typo)
    check("助理段用播报原文纠字", fixed[0].text == exact, f"实际：{fixed[0].text}")
    check("纠字后仍然标成助理", fixed[0].speaker == ASSISTANT_SPEAKER and m1 == 1)
    check("纠字不会重复补一段", i1 == 0 and len(fixed) == 1, f"injected={i1} n={len(fixed)}")

    # 被打断：只保留真正说出去的那几个字
    cut = [Segment(40.0, 44.0, 0, "嗯明天记得带伞今天阴天")]
    ev_cut = [{"id": "c", "start": 40.0, "end": 44.0,
               "text": "嗯，明天记得带伞，今天阴天16到22度。", "spoken_chars": 8,
               "state": "partial"}]
    cut_out, _, _ = apply_assistant_timeline(cut, ev_cut)
    check("打断后按 spoken_chars 截断", cut_out[0].text == "嗯，明天记得带伞", f"实际：{cut_out[0].text}")

    # 一句播报被 ASR 切成几段时不能每段都写整句（否则稿子里同一句出现 N 遍）
    multi = [
        Segment(30.0, 33.0, 0, "明天有小雨"),
        Segment(33.2, 35.5, 0, "11到17度"),
        Segment(35.6, 37.6, 0, "降雨概率69%"),
    ]
    ev_multi = [{"id": "m", "start": 30.0, "end": 37.6, "text": exact,
                 "spoken_chars": len(exact), "state": "done"}]
    m_out, m_marked, m_inj = apply_assistant_timeline(multi, ev_multi)
    joined = "".join(s.text for s in m_out)
    check("多段时不会每段都写整句", sum(1 for s in m_out if s.text == exact) == 0,
          str([s.text for s in m_out]))
    check("多段拼起来接近原文", abs(len(joined) - len(exact)) <= 4,
          f"{len(joined)} vs {len(exact)}：{joined}")
    check("多段全部标成助理且不重复补", m_marked == 3 and m_inj == 0,
          f"marked={m_marked} injected={m_inj}")

    # 顺序：必须先标时间线、再合并同说话人的相邻句
    # 否则助理那句会和前后人声并成一段，整段被判成助理（把真人的话也吃掉）
    near = [
        Segment(0.0, 3.0, 0, "我先说一句"),
        Segment(3.2, 6.0, 0, "这是助理念的台词"),
        Segment(6.2, 9.0, 0, "我又接着说"),
    ]
    ev = [{"id": "x", "kind": "tts", "text": "这是助理念的台词",
           "start": 3.2, "end": 6.0, "spoken_chars": 8, "state": "done"}]
    marked_segs, _, _ = apply_assistant_timeline(near, ev)
    merged = merge_turns(marked_segs)
    kinds = [s.speaker for s in merged]
    check("先标时间线再合并：助理段独立",
          ASSISTANT_SPEAKER in kinds, str(kinds))
    check("先标时间线再合并：前后人声没被吃掉",
          sum(1 for k in kinds if k == 0) >= 2, str(kinds))
    check("助理段文本没被合并进人声",
          any("助理念的台词" in s.text for s in merged if s.speaker == ASSISTANT_SPEAKER),
          str([(s.speaker, s.text) for s in merged]))

    # 说话人数不把助理算成人
    t = Transcript(source=Path("a.wav"), duration=10, backend="test",
                   segments=[Segment(0, 1, 0, "a"), Segment(1, 2, ASSISTANT_SPEAKER, "b")])
    check("speaker_count 不含助理", t.speaker_count == 1, str(t.speaker_count))

    # 标签跟着音色走：克隆音色是「我的声音」，sys: 开头是「电脑声音」
    check("克隆音色 → 我的声音",
          assistant_label([{"kind": "tts", "voice": "alice"}]) == ASSISTANT_LABEL)
    check("电脑音色 → 电脑声音",
          assistant_label([{"kind": "tts", "voice": "sys:yunxi"}]) == ASSISTANT_LABEL_SYS)
    check("旧归档没有 label 字段也能判",
          assistant_label([{"kind": "tts", "voice": "sys:xiaoxiao"}]) == ASSISTANT_LABEL_SYS)
    check("label 优先于 voice",
          assistant_label([{"kind": "tts", "voice": "sys:yunxi", "label": ASSISTANT_LABEL}])
          == ASSISTANT_LABEL)
    check("空事件 → 回退默认",
          assistant_label([]) == ASSISTANT_LABEL)

    # 回归：M20260101-100000-bbbb 的真实事故
    # 一段播报在录音里被 ASR 切成两截（41.87-42.83、43.55-44.38），
    # 中间有 0.7 秒空档，旧逻辑按「单段重叠 ≥30%」判定不算覆盖 → 又整句补了一遍，
    # 合并后稿子里就成了「嗯，你说，我听着呢。嗯，你说，我听着呢。」
    ev2 = [{"id": "dup", "kind": "tts", "text": "嗯，你说，我听着呢。",
            "start": 41.4, "end": 44.7, "spoken_chars": 10, "state": "done"}]
    split_segs = [
        Segment(41.87, 42.83, 1, "嗯，你说。"),
        Segment(43.55, 44.38, 1, "我听着呢。"),
    ]
    out2, marked2, injected2 = apply_assistant_timeline(split_segs, ev2)
    check("被切成两截时不重复补", injected2 == 0, f"injected={injected2}")
    merged2 = merge_turns(out2)
    check("合并后不复读", len(merged2) == 1
          and merged2[0].text.count("我听着呢") == 1, str([s.text for s in merged2]))
    check("合并后文本完整", merged2[0].text.replace("，", "，") in
          ("嗯，你说，我听着呢。", "嗯，你说，我听着呢"), merged2[0].text)

    # 真的没录进来（合成音只走了虚拟声卡）：还是要补，而且只补一条
    out3, _, injected3 = apply_assistant_timeline(
        [Segment(0.0, 2.0, 0, "你好")], ev2)
    check("没录进来时补一条", injected3 == 1, str(injected3))
    check("补的文本是日志原文",
          any(s.speaker == ASSISTANT_SPEAKER and s.text == "嗯，你说，我听着呢。"
              for s in out3), str([(s.speaker, s.text) for s in out3]))

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
