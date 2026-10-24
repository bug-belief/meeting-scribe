"""实时面板的「这句是谁说的」：右声道静音时不硬标。

**2026-10-21 实测背景**：电话外放时对方的声音从房间进麦克风，
右声道（系统声音）全程只有约 0.4s 底噪（共约 170s）。

原来的判定是「这句左能量 >= 右能量 → 标「我」」。右声道恒静时这个条件恒成立，
于是对方说的话也全部标成「我」——面板看起来像我一个人在自言自语。

改法：先问一句「对方那一路到底有没有录到」。
  有 → 照旧按左右能量标
  没有/还看不出来 → 不标 who
前端 `addLine` 见 who 为空就不出标签；`isSelf()` 对空 who 按「可能是我」放行，
所以唤醒助理的逻辑不受影响（有意降级，注释见 app.js isSelf）。

本测试直接打 LiveSession 的判定接口，不碰 WebSocket 与 ASR。
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
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
    import live_engine  # noqa: F401  (仅为确认依赖可导入)
    from config import SETTINGS
    from live_engine import PEER_PROBE_MIN_SAMPLES, SAMPLE_RATE, LiveSession
except ImportError as exc:  # pragma: no cover
    print(f"  SKIP 缺依赖：{exc}")
    sys.exit(0)


def new_session():
    d = Path(tempfile.mkdtemp())
    st = replace(SETTINGS, base_dir=d, work_dir=d / "work", owner="me")
    (d / "work").mkdir(parents=True, exist_ok=True)
    return LiveSession(st)


# ---------------------------------------------------------------------------
print("\n1) 样本不够时不下结论（None），不硬标")
s = new_session()
check("刚开会话 → 判断不了", s.peer_channel_alive(), None)
# 只喂 7 秒，仍不够
s.note_peer_level(0.0, SAMPLE_RATE * 7)
check("7 秒（< 8 秒门槛）→ 仍判断不了", s.peer_channel_alive(), None)
check("记录下的最大能量为 0", s.peer_max_rms, 0.0)

# ---------------------------------------------------------------------------
print("\n2) 右声道全程静音 → 判定为「没有」")
s = new_session()
s.note_peer_level(0.0008, SAMPLE_RATE * 60)      # 实测底噪量级
s.note_peer_level(0.0012, SAMPLE_RATE * 60)
check("底噪 0.0012 → 右声道不存在", s.peer_channel_alive(), False)
check("最大能量被正确记录", round(s.peer_max_rms, 4), 0.0012)

# ---------------------------------------------------------------------------
print("\n3) 右声道有真实人声 → 判定为「有」")
s = new_session()
s.note_peer_level(0.0009, SAMPLE_RATE * 5)
s.note_peer_level(0.05, SAMPLE_RATE * 5)         # 有人说话
check("一句人声之后 → 右声道存在", s.peer_channel_alive(), True)

# ---------------------------------------------------------------------------
print("\n4) 门限边界：0.003（与前端 watchPeerLevel 同口径）")
s = new_session()
s.note_peer_level(0.003, SAMPLE_RATE * 8)
check("恰好 0.003 → 判定存在（> 才为真？这里取 >）", s.peer_channel_alive(), False)
s = new_session()
s.note_peer_level(0.0031, SAMPLE_RATE * 8)
check("0.0031 → 判定存在", s.peer_channel_alive(), True)

# ---------------------------------------------------------------------------
print("\n5) 累计语义：先静后响，应当翻转为「有」")
s = new_session()
s.note_peer_level(0.0, SAMPLE_RATE * 20)
check("20 秒静音 → 暂判没有", s.peer_channel_alive(), False)
s.note_peer_level(0.02, SAMPLE_RATE * 1)
check("之后有人说了一句 → 翻转为有", s.peer_channel_alive(), True)
check("确认是取历史最大而非最近值", round(s.peer_max_rms, 3), 0.02)

# ---------------------------------------------------------------------------
print("\n6) 反向对照：阈值口径若被改宽（>0.05），真实人声会被误判为静音")
s = new_session()
s.note_peer_level(0.02, SAMPLE_RATE * 10)
check_true("0.02 的人声按现行口径算「有」（0.02 > 0.003）", s.peer_channel_alive() is True)
wrong_gate = 0.02 > 0.05          # 假设有人把门槛改成 0.05
check_true("同样的声音在 0.05 门槛下会被判成静音 —— 说明 0.003 在承重",
           wrong_gate is False, f"0.02 > 0.05 = {wrong_gate}")

# ---------------------------------------------------------------------------
print("\n7) 与前端口径一致性（前端也是 0.003 / 8 秒）")
app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
check_true("前端 watchPeerLevel 里仍有 0.003 门槛", "0.003" in app_js,
           "找不到 0.003，两处口径可能已漂移")
check_true("前端仍是 secs < 8 才开始判", "secs < 8" in app_js,
           "找不到 secs < 8")
check_true("后端 8 秒门槛 = SAMPLE_RATE * 8",
           PEER_PROBE_MIN_SAMPLES == SAMPLE_RATE * 8)

print("\n" + "=" * 40)
print(f"通过 {16 - FAILED} 项，失败 {FAILED} 项（共 16 项）")
sys.exit(1 if FAILED else 0)
