"""实时回声判定：助理自己的声音不能当成人声。

    python tests/test_live_echo.py

只验证时间窗 + 声道能量这两条规则，不加载模型、不建 WebSocket。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_engine import LiveSession  # noqa: E402  （O-11 C-1 起会话类搬到这里）

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def make(events: list[dict], channels: int = 2, duration: float = 100.0) -> LiveSession:
    s = LiveSession.__new__(LiveSession)   # 跳过 __init__（不建文件、不写盘）
    s.events = events
    s.channels = channels
    s.samples = int(duration * 16000)   # duration 是只读属性，靠 samples 推
    return s


def main() -> int:
    ev = [{"id": "a", "start": 10.0, "end": 14.0, "text": "在呢，你说。", "state": "done"}]

    s = make(ev)
    check("右声道 + 完全落在播报区间 → 判为回声", s.is_echo(10.5, 13.5, True) is True)
    check("左声道为主（用户插话）→ 不判回声", s.is_echo(10.5, 13.5, False) is False)
    check("区间不重叠 → 不判回声", s.is_echo(20.0, 22.0, True) is False)
    check("只重叠半秒 → 不判回声", s.is_echo(13.5, 16.0, True) is False)
    check("右声道 + 重叠 ≥1 秒 → 判回声（容忍时间戳偏移）",
          s.is_echo(13.0, 16.0, True) is True)

    single = make(ev, channels=1)
    check("单声道时只看时间（无法分左右）", single.is_echo(10.5, 13.5, False) is True)

    speaking = make([{"id": "b", "start": 30.0, "end": 0.0, "state": "speaking"}])
    check("播报中：窗口从现在开到当前时长", speaking.tts_window() == (30.0, 100.0),
          str(speaking.tts_window()))
    check("播报中右声道重叠 → 回声", speaking.is_echo(31.0, 33.0, True) is True)

    idle = make([])
    check("没有播报时没有窗口", idle.tts_window() is None)
    check("没有播报时不判回声", idle.is_echo(10.0, 12.0, True) is False)

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
