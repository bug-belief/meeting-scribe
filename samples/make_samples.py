#!/usr/bin/env python3
"""生成演示用音频：让「clone 下来能不能跑通」有个现成的输入。

为什么不把生成的音频直接入库
----------------------------
真正能验证转写的样例必须是**人声**，而人声有两种都不适合入库：

- 真人录音 → 隐私问题
- 云端 TTS（如 edge-tts 走微软免费接口）→ **再分发授权不明确**。
  微软官方口径是「付费档才明确允许再分发合成音频」，免费接口没说。
  开源仓库不该赌这个。

所以仓库里只有**生成脚本**，音频由使用者在本机现生成：

    python3 samples/make_samples.py            # 默认：纯合成音（离线、零依赖、无授权问题）
    python3 samples/make_samples.py --speech   # 额外：用 edge-tts 生成人声（需联网 + 已装 edge-tts）

两种产物用途不同，别混
----------------------
1. **合成音**（`demo-dual.wav`）——左正弦 / 右三角，各说各的。
   它**故意不是语音**：ASR 转不出字，但能验证三件事：
   - 双声道分轨认得「左一路、右一路」
   - 声纹聚类能分出两簇
   - 整条管线（切段 → 转写 → 归并 → 出稿）不崩

   ⚠️ 不要拿它判断「转写准不准」——它本来就没有字。

2. **人声**（`demo-speech-*.wav`）——真正能验证识别质量与说话人归属。
   只在你本机生成、不入库（`.gitignore` 已挡）。

怎么用合成音验证分轨
--------------------
    # 直接看分轨结果（不需要模型）
    python3 -c "from diarize import _channel_activity; ..."   # 见 samples/README.md

    # 走上传管线（需要本地 ASR 或云端 ASR 配好 key）
    curl -F file=@samples/demo-dual.wav 'http://127.0.0.1:8848/api/upload?k=<口令>'
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import wave
from pathlib import Path

SAMPLE_RATE = 16000          # 与 live_engine.SAMPLE_RATE 一致，别改
HERE = Path(__file__).resolve().parent


def _sine(freq: float, sec: float, amp: float = 0.25, rate: int = SAMPLE_RATE) -> list[float]:
    n = int(sec * rate)
    return [amp * math.sin(2 * math.pi * freq * i / rate) for i in range(n)]


def _triangle(freq: float, sec: float, amp: float = 0.25, rate: int = SAMPLE_RATE) -> list[float]:
    n = int(sec * rate)
    out = []
    period = rate / freq
    for i in range(n):
        phase = (i % period) / period
        out.append(amp * (4 * abs(phase - 0.5) - 1))
    return out


def _silence(sec: float, rate: int = SAMPLE_RATE) -> list[float]:
    return [0.0] * int(sec * rate)


def _tone_burst(freq: float, sec: float, shape: str = "sine", amp: float = 0.25) -> list[float]:
    """带 10ms 淡入淡出的音，避免切片处的爆音被当成语音起止。"""
    core = _sine(freq, sec, amp) if shape == "sine" else _triangle(freq, sec, amp)
    fade = int(0.01 * SAMPLE_RATE)
    for i in range(min(fade, len(core))):
        w = i / fade
        core[i] *= w
        core[-1 - i] *= w
    return core


def build_dual() -> tuple[list[float], list[float]]:
    """左右两路交替，每段 1.5 秒，中间留 0.5 秒静音 —— 模拟两人轮流说话。

    两个长度都不是随便定的：
    - **必须有静音间隔**：分轨判定依赖「这一段有没有能量」，两路一直响就分不出轮次。
    - **每路总时长要够**：`live_engine.peer_channel_alive()` 要观察满 8 秒才肯下结论
      （`PEER_PROBE_MIN_SAMPLES`），右路只响 2.4 秒的话永远判不出「对方存在」，
      会被退化成 Speaker 2/3。所以每路各 3 段 × 1.5s = 4.5s，加上间隔总长 12 秒。
    """
    left: list[float] = []
    right: list[float] = []
    SEG, GAP = 1.5, 0.5
    pattern = [
        ("L", "sine", 220.0),
        ("R", "triangle", 330.0),
        ("L", "sine", 247.0),
        ("R", "triangle", 294.0),
        ("L", "sine", 262.0),
        ("R", "triangle", 350.0),
    ]
    for side, shape, freq in pattern:
        tone = _tone_burst(freq, SEG, shape)
        gap = _silence(GAP)
        if side == "L":
            left.extend(tone + gap)
            right.extend(_silence(SEG + GAP))
        else:
            left.extend(_silence(SEG + GAP))
            right.extend(tone + gap)
    # 末尾补 1 秒静音：与 ws 测试同样的道理 —— 不定稿是因为没等到静音
    left.extend(_silence(1.0))
    right.extend(_silence(1.0))
    return left, right


def write_stereo_wav(path: Path, left: list[float], right: list[float],
                     rate: int = SAMPLE_RATE) -> None:
    n = min(len(left), len(right))
    frames = bytearray()
    for i in range(n):
        def q(v: float) -> int:
            return max(-32768, min(32767, int(round(v * 32767))))

        frames += struct.pack("<hh", q(left[i]), q(right[i]))
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(bytes(frames))


def write_mono_wav(path: Path, data: list[float], rate: int = SAMPLE_RATE) -> None:
    frames = bytearray()
    for v in data:
        frames += struct.pack("<h", max(-32768, min(32767, int(round(v * 32767)))))
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(bytes(frames))


SPEECH_LINES = [
    ("demo-speech-a.wav", "zh-CN-XiaoxiaoNeural",
     "你好，我是第一个说话的人。这句话用来测试转写和说话人识别。"),
    ("demo-speech-b.wav", "zh-CN-YunxiNeural",
     "你好，我是第二个说话的人。这句话应该被识别成另一个人。"),
]


def make_speech() -> int:
    """用 edge-tts 现生成人声。**不入库**（授权不明确，见文件头）。"""
    try:
        import asyncio

        import edge_tts
    except ImportError:
        print("未安装 edge-tts。装：pip install edge-tts（需联网，走微软免费接口）",
              file=sys.stderr)
        return 1

    async def _one(text: str, voice: str, out: Path) -> None:
        await edge_tts.Communicate(text, voice).save(str(out))

    for name, voice, text in SPEECH_LINES:
        out = HERE / name
        try:
            asyncio.run(_one(text, voice, out))
        except Exception as exc:
            print(f"生成 {name} 失败：{exc}", file=sys.stderr)
            return 1
        print(f"  {name}  {out.stat().st_size // 1024} KB  voice={voice}")
    print("\n⚠️ 这些是云端 TTS 合成的音频，**授权不明确，不要提交进仓库**。")
    print("   .gitignore 已挡 *.mp3 / *.wav 之外的 speech 产物；确认一遍 git status。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成演示音频（默认纯合成音，无外部依赖）")
    ap.add_argument("--speech", action="store_true",
                    help="额外用 edge-tts 生成人声（需联网；产物不入库）")
    args = ap.parse_args(argv)

    print("1) 合成双声道 demo-dual.wav")
    left, right = build_dual()
    out = HERE / "demo-dual.wav"
    write_stereo_wav(out, left, right)
    sec = len(left) / SAMPLE_RATE
    print(f"   {out.name}  {out.stat().st_size // 1024} KB  {sec:.1f}s  16kHz/16bit/立体声")
    print("   左路：220/247/262 Hz 正弦 ×3 段；右路：330/294/350 Hz 三角波 ×3 段，各 1.5s 交替")

    print("\n2) 单声道纯音 demo-mono.wav（测单路链路用）")
    mono = _tone_burst(440.0, 3.0) + _silence(1.0)
    mout = HERE / "demo-mono.wav"
    write_mono_wav(mout, mono)
    print(f"   {mout.name}  {mout.stat().st_size // 1024} KB  4.0s  16kHz/16bit/单声道")

    if args.speech:
        print("\n3) 人声（edge-tts）")
        rc = make_speech()
        if rc:
            return rc
    else:
        print("\n（要人声样例加 --speech，需要联网并已安装 edge-tts）")

    print("\n完成。这两个 WAV 是**合成的，不是语音**：用来验证分轨/聚类/管线不崩，")
    print("不用来判断识别准不准 —— 它们本来就没有字。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
