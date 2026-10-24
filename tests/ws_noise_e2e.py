"""端到端验证：底噪触发的「幻听」不会进入实时面板。

素材就是出事的那段录音（M20260101-100000-bbbb）。实测它的麦克风声道：
    96~116s / 140~152s / 196~216s  只有底噪（RMS 0.0015 上下），却被流式模型
    解成了「的的的的」「亲我现在」，面板上看起来像用户说了没说过的话。
    0~6s / 74~77s                  真人说话（RMS 0.02+）。

跑法（服务器上，需要 WEB_TOKEN）：
    .venv/bin/python tests/ws_noise_e2e.py --token $WEB_TOKEN \
        --wav ~/data/tenants/me/processed/2026-01-01_会议_0930/会议_0930.wav \
        --start 140 --end 152 --expect 0
    .venv/bin/python tests/ws_noise_e2e.py --token $WEB_TOKEN \
        --wav .../会议_0930.wav --start 0 --end 6 --expect 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from array import array

SAMPLE_RATE = 16000
FRAME_SEC = 0.6


def read_channel(path: str, start: float, end: float) -> bytes:
    """取左声道（麦克风）在 [start, end) 的一段，转成 PCM16 mono bytes。"""
    with wave.open(path, "rb") as fh:
        ch = fh.getnchannels()
        assert fh.getframerate() == SAMPLE_RATE, f"要 16k，实际 {fh.getframerate()}"
        fh.setpos(int(start * SAMPLE_RATE))
        frames = fh.readframes(int((end - start) * SAMPLE_RATE))
        data = array("h")
        data.frombytes(frames)
    if ch > 1:
        data = data[0::ch]          # 只留左声道
    return data.tobytes()


async def run(ws_url: str, token: str, pcm: bytes):
    import websockets

    sep = "&" if "?" in ws_url else "?"
    lines: list[str] = []
    drops: list[str] = []
    quiet: list[str] = []
    async with websockets.connect(f"{ws_url}{sep}k={token}", max_size=None) as ws:
        json.loads(await ws.recv())                      # ready
        await ws.send(json.dumps({"type": "start", "source": "mic", "channels": 1}))
        await ws.recv()                                  # source ack

        # 后台读：websockets 17 没有 recv_nowait()，用异步迭代器收。
        # 之前用 recv_nowait + except 吞异常，脚本会「0 行 = 通过」假绿。
        async def reader():
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("type") == "line":
                        lines.append(str(msg.get("text") or ""))
                    elif msg.get("type") == "drop":
                        drops.append(str(msg.get("reason") or ""))
                    elif msg.get("type") == "partial" and msg.get("quiet"):
                        quiet.append(str(msg.get("rms") or ""))
            except Exception:
                pass

        task = asyncio.create_task(reader())

        # 尾巴补 2 秒数字静音：服务端靠「连续静音」定稿，
        # 不补的话最后一句永远等不到 silence，测试会误判成「没识别出来」。
        pcm = pcm + b"\x00" * (int(2.0 * SAMPLE_RATE) * 2)
        block = int(FRAME_SEC * SAMPLE_RATE * 2)
        for off in range(0, len(pcm), block):
            await ws.send(pcm[off:off + block])
            await asyncio.sleep(0.05)                    # ~12 倍速：再快就让 ASR 追不上

        # 收尾：等队列里的块跑完 + 静音断句（0.75s）走完
        await asyncio.sleep(6.0)
        task.cancel()
    return len(lines), lines, drops, quiet


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default="ws://127.0.0.1:8848/ws/live")
    ap.add_argument("--token", required=True)
    ap.add_argument("--wav", required=True)
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--expect", type=int, required=True, help="期望的定稿行数")
    args = ap.parse_args()

    pcm = read_channel(args.wav, args.start, args.end)
    n, lines, drops, quiet = asyncio.run(run(args.ws, args.token, pcm))
    print(f"区间 {args.start}-{args.end}s：{n} 行（期望 {args.expect}），"
          f"丢弃 {len(drops)} 次，被闸门压掉的字幕 {len(quiet)} 次")
    for text in lines:
        print("   line:", text)
    ok = n == args.expect
    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
