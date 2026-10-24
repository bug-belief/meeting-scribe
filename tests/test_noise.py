"""噪声幻听闸门的回归测试。

来源：2026-10-19 会话 M20260101-100000-bbbb。用户在双路分轨下对话，
面板上却不断冒出「的就是我个的的的的」「我的的的的的的」「亲我现在」
这类他没说过的话，助理还据此回了一句「听着有点乱，你再说一遍要问啥？」。

实测那段录音的麦克风底噪 RMS 只有 0.0007~0.0018，真人说话 ≥0.02，
所以「能量闸门」能把绝大部分挡掉；剩下能量偏高（截图音效、按键）的，
靠形态判断（单字刷屏 / 整句复读）兜住。

跑法（服务器上）：
    .venv/bin/python tests/test_noise.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import SETTINGS  # noqa: E402

# O-11 C-1 起，噪声闸门与会话类搬到了 live_engine.py（server.py 不再导出它们）
from live_engine import (  # noqa: E402
    NOISE_FLOOR_MIN,
    LiveSession,
    looks_like_noise,
)

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


print("[1] 真实噪声行必须被判掉")
NOISE = [
    "就是我个的的的的",
    "我的的的的的的",
    "小等等就是好的的的的一的的是是是是的的的的是是是是",
    "们的的的的的的的",
    "我我",
    "嗯嗯",
    "的是的是的是的是",
]
for text in NOISE:
    check(f"噪声：{text[:14]}", looks_like_noise(text) is True)

print("[2] 真实说过的话不能被误杀")
SPEECH = [
    "云助理。看一下明天杭州天气怎么样。",
    "明天上海天气怎么样？",
    "一加一等于几。",
    "语音助理。",
    "二百四十八乘以三百五十六等于多少？",
    "好吧，今天先到这里吧。",
    "我",              # 单字回答也是话
    "好",
    "哈哈哈",          # 三个哈：还没到刷屏阈值
    "行，那就先这样，有事再喊我。",
]
for text in SPEECH:
    ok = looks_like_noise(text) is False
    check(f"真话：{text[:14]}", ok)

print("[3] 本底噪声估计")
_MADE = []


def _session():
    """建一个真实会话对象（会落一个空 raw.pcm），测完统一删掉。"""
    s = LiveSession(SETTINGS)
    _MADE.append(s)
    return s


s = _session()
check("没样本时用绝对下限", s.rms_floor() == NOISE_FLOOR_MIN, str(s.rms_floor()))
# 实测底噪 0.0007：中位数 × 4 = 0.0028 → 仍取绝对下限
for _ in range(50):
    s.note_rms(0.0007)
check("安静环境 → 0.0035", abs(s.rms_floor() - NOISE_FLOOR_MIN) < 1e-9, str(s.rms_floor()))
# 底噪更低的专业麦克风：不该把闸门压到 0，否则静音也能过
s2 = _session()
for _ in range(50):
    s2.note_rms(0.0002)
check("极安静也不低于下限", s2.rms_floor() >= NOISE_FLOOR_MIN, str(s2.rms_floor()))
# 嘈杂环境（风扇、空调）：闸门跟着抬高，但不至于高到吃掉正常说话
s3 = _session()
for _ in range(50):
    s3.note_rms(0.003)
check("嘈杂环境抬到 0.012", abs(s3.rms_floor() - 0.012) < 1e-6, str(s3.rms_floor()))
check("抬高后仍低于说话（0.02）", s3.rms_floor() < 0.02, str(s3.rms_floor()))

print("[4] 峰值记账")
s4 = _session()
for v in (0.001, 0.03, 0.002):
    s4.note_rms(v)
check("line_peak 取最大", abs(s4.line_peak - 0.03) < 1e-9, str(s4.line_peak))
s4.reset_line()
check("reset 后归零", s4.line_peak == 0.0 and s4.pending == "", f"{s4.line_peak} {s4.pending!r}")

import shutil  # noqa: E402

for sess in _MADE:
    try:
        sess.raw.close()
    except Exception:
        pass
    shutil.rmtree(sess.dir, ignore_errors=True)

print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
raise SystemExit(0 if FAIL == 0 else 1)
