"""整段回放：把真实录音的麦克风声道按 13 倍速推给实时服务，看噪声行有没有被闸门挡住。

不是自动化测试，是排查脚本（tests/ws_noise_e2e.py 只喂短片段，模型没有上下文，
复现不出「连续解码 + 静音续写」的幻听）。
"""

import asyncio
import json
import os
import sys
import wave
from array import array
from pathlib import Path

SAMPLE_RATE = 16000
PATH = sys.argv[1] if len(sys.argv) > 1 else str(
    Path.home() / "data/tenants/me/processed/2026-01-01_会议_0930/会议_0930.wav")


async def main() -> None:
    import websockets

    token = os.environ["WEB_TOKEN"]
    with wave.open(PATH, "rb") as fh:
        ch = fh.getnchannels()
        data = array("h")
        data.frombytes(fh.readframes(fh.getnframes()))
        data = data[0::ch]
    pcm = data.tobytes() + b"\x00" * (2 * SAMPLE_RATE * 2)

    lines: list[dict] = []
    drops: list[dict] = []
    quiet = 0

    async with websockets.connect(
        f"ws://127.0.0.1:8848/ws/live?k={token}", max_size=None
    ) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "start", "source": "mic", "channels": 1}))
        await ws.recv()

        async def reader() -> None:
            nonlocal quiet
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                kind = msg.get("type")
                if kind == "line":
                    lines.append(msg)
                elif kind == "drop":
                    drops.append(msg)
                elif kind == "partial" and msg.get("quiet"):
                    quiet += 1

        task = asyncio.create_task(reader())
        for off in range(0, len(pcm), 19200):
            await ws.send(pcm[off:off + 19200])
            await asyncio.sleep(0.045)
        await asyncio.sleep(8)
        task.cancel()

    print(f"=== lines {len(lines)} / drops {len(drops)} / quiet {quiet}")
    for msg in lines:
        print(f"  {msg.get('start')}-{msg.get('end')} {msg.get('text')}")


asyncio.run(main())
