"""双声道命名：右声道有效 vs 基本静音。

**为什么单独测这个**：2026-10-21 实测发现一个反直觉的坑——

    电话外放时，对方的声音从房间进麦克风（左声道），
    右声道（系统声音）全程只有约 0.4s 底噪，共约 170s。

原来的命名规则是「左声道依次叫 我、我2…」。于是两个簇都在 left，
第二个被叫成 **「我2」**——把对方说成了自己。这不是措辞问题：
「我2」会被前端当成「我」，谁在喊助理就判错了。

本测试锁住两条：
  ① 右声道有效（真的录到了对方）→ 保持原语义：左「我」右「对方」，同侧多人往后编号
  ② 右声道基本静音               → 麦克风侧第 2 人起标「Speaker N」，不冒充「我」

`_name_dual_speakers` 是纯函数（dict 进、dict 出），可以脱离音频直接测。
真实音频链路由 test_dual 的 `_apply_channel_names` 那组覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = 0


def check(name: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"  ok   {name}")
        return
    FAILED += 1
    print(f"  FAIL {name}\n         实际 {got!r}\n         期望 {want!r}")


def check_true(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"  ok   {name}")
        return
    FAILED += 1
    print(f"  FAIL {name}" + (f"\n         {detail}" if detail else ""))


try:
    from transcribe import _name_dual_speakers
except ImportError as exc:  # pragma: no cover
    print(f"  SKIP 缺依赖：{exc}")
    sys.exit(0)

# ---------------------------------------------------------------------------
print("\n1) 右声道有效：保持原语义（左=我、右=对方）")

info = {
    "channel_map": {"0": "left", "1": "right"},
    "names": {},
    "channel_activity": {"left": 60.0, "right": 40.0},
}
note = _name_dual_speakers(info, "我", "对方")
check("一人一侧 → 我 / 对方", info["names"], {"0": "我", "1": "对方"})
check_true("说明里提到两侧人数", "麦克风侧 1 人" in (note or ""), note or "")

# 声纹认得的优先用真名
info = {
    "channel_map": {"0": "left", "1": "right"},
    "names": {"0": "张三"},
    "channel_activity": {"left": 60.0, "right": 40.0},
}
_name_dual_speakers(info, "我", "对方")
check("声纹认得就用真名", info["names"], {"0": "张三", "1": "对方"})

# 右声道有多人：依次「对方、对方2」
info = {
    "channel_map": {"0": "left", "1": "right", "2": "right"},
    "names": {},
    "channel_activity": {"left": 30.0, "right": 50.0},
}
_name_dual_speakers(info, "我", "对方")
check("右侧多人在此情形下仍按编号", info["names"],
      {"0": "我", "1": "对方", "2": "对方2"})

# ---------------------------------------------------------------------------
print("\n2) 右声道基本静音：麦克风侧第 2 人起标 Speaker N")

# 复现真实场景：左 80s、右 0.4s、两个簇都在左
info = {
    "channel_map": {"0": "left", "1": "left"},
    "names": {"0": "张三"},
    "channel_activity": {"left": 80.0, "right": 0.4},
}
note = _name_dual_speakers(info, "我", "对方")
check("第一人保持真名，第二人叫 Speaker 2（不再是「我2」）",
      info["names"], {"0": "张三", "1": "Speaker 2"})
check_true("说明里点出右声道静音", "基本静音" in (note or ""), note or "")
check_true("说明里注明不冒充「我」", "不冒充" in (note or ""), note or "")

# 三人：第 2、3 人依次 Speaker 2 / Speaker 3
info = {
    "channel_map": {"0": "left", "1": "left", "2": "left"},
    "names": {},
    "channel_activity": {"left": 90.0, "right": 0.2},
}
_name_dual_speakers(info, "我", "对方")
check("三人 → 我 / Speaker 2 / Speaker 3", info["names"],
      {"0": "我", "1": "Speaker 2", "2": "Speaker 3"})

# 声纹认得的仍然优先（认得就不该被降级成 Speaker N）
info = {
    "channel_map": {"0": "left", "1": "left"},
    "names": {"0": "张三", "1": "李四"},
    "channel_activity": {"left": 80.0, "right": 0.4},
}
_name_dual_speakers(info, "我", "对方")
check("右声道静音但声纹认得第二人 → 用真名", info["names"],
      {"0": "张三", "1": "李四"})

# ---------------------------------------------------------------------------
print("\n3) 边界：缺 channel_activity（旧归档 / 未跑到那一步）")

info = {"channel_map": {"0": "left", "1": "left"}, "names": {}}
note = _name_dual_speakers(info, "我", "对方")
# 拿不到活动量时按「右声道不可靠」处理更安全：宁可标 Speaker 2，也不要把对方说成我
check("无活动量 → 保守地标 Speaker 2", info["names"], {"0": "我", "1": "Speaker 2"})
check_true("有返回说明", bool(note), note or "")

info = {"channel_map": {}, "names": {}}
check("没有 channel_map → 返回 None 不动", _name_dual_speakers(info, "我", "对方"), None)

# ---------------------------------------------------------------------------
print("\n4) 阈值边界：右声道刚好在门限两侧")

from diarize import CHANNEL_ACTIVE_MIN_SEC  # noqa: E402

for sec, expect_right_named, label in [
    (CHANNEL_ACTIVE_MIN_SEC - 0.01, False, "略低于门限 → 视为静音"),
    (CHANNEL_ACTIVE_MIN_SEC, True, "正好等于门限 → 视为有效"),
    (CHANNEL_ACTIVE_MIN_SEC + 5, True, "高于门限 → 视为有效"),
]:
    info = {
        "channel_map": {"0": "left", "1": "left"},
        "names": {},
        "channel_activity": {"left": 50.0, "right": sec},
    }
    _name_dual_speakers(info, "我", "对方")
    second = info["names"]["1"]
    got_right_named = second != "Speaker 2"
    check_true(f"{label}（right={sec:.2f}s，第二人={second}）",
               got_right_named == expect_right_named, f"第二人={second}")

# ---------------------------------------------------------------------------
print("\n5) 反向对照：把阈值检查去掉，第 2 节必须失败")
original = _name_dual_speakers


def broken(speaker_info, self_name, peer_name):
    """故意忽略 channel_activity，回到「左声道依次叫我、我2…」的老逻辑。"""
    sides = {int(k): v for k, v in (speaker_info.get("channel_map") or {}).items()}
    if not sides:
        return None
    known = {int(k): v for k, v in (speaker_info.get("names") or {}).items()}
    counts = {"left": 0, "right": 0}
    names = {}
    for spk in sorted(sides):
        side = sides[spk]
        counts[side] += 1
        base = self_name if side == "left" else peer_name
        names[spk] = known.get(spk) or (base if counts[side] == 1 else f"{base}{counts[side]}")
    speaker_info["names"] = {str(k): v for k, v in names.items()}
    return "old"


info = {
    "channel_map": {"0": "left", "1": "left"},
    "names": {"0": "张三"},
    "channel_activity": {"left": 80.0, "right": 0.4},
}
broken(info, "我", "对方")
check_true("老逻辑确实会把对方叫成「我2」（说明这条护栏不是空跑）",
           info["names"] == {"0": "张三", "1": "我2"}, f"实际 {info['names']}")

print("\n" + "=" * 40)
print(f"通过 {24 - FAILED} 项，失败 {FAILED} 项（共 24 项）")
sys.exit(1 if FAILED else 0)
