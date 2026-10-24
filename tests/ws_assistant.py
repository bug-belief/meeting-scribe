"""验证「助理时间线」在真实会话里的落盘效果。

把一段 16k 单声道 wav 当麦克风推给服务，中途插入一条助理播报事件
（tts_begin / tts_end），停止后检查归档里：

    1. meta.json 的 assistant_events 是否记下了这条
    2. transcript.md 里是否出现「助理（我的声音）」段落

    python tests/ws_assistant.py --wav /tmp/seg16k.wav --token xxx
"""

from __future__ import annotations

import os
import argparse
import asyncio
import json
import wave
from pathlib import Path

FRAME_BYTES = 19200  # 600ms @ 16k/s16/单声道

ASSISTANT_TEXT = "这是助理说的测试台词，用来验证时间线标注是不是对的。"


async def run(url: str, wav: str, token: str) -> int:
    import websockets

    with wave.open(wav, "rb") as fh:
        if fh.getframerate() != 16000 or fh.getsampwidth() != 2 or fh.getnchannels() != 1:
            print(f"❌ 需要 16k/s16/单声道 wav，实际 {fh.getframerate()}Hz "
                  f"{fh.getnchannels()}ch {fh.getsampwidth() * 8}bit")
            return 2
        data = fh.readframes(fh.getnframes())

    full = f"{url}?k={token}" if token and "?" not in url else url
    blocks = [data[i:i + FRAME_BYTES] for i in range(0, len(data), FRAME_BYTES)]

    async with websockets.connect(full, max_size=None, ping_interval=25) as ws:
        ready = json.loads(await ws.recv())
        print(f"ready: session={ready.get('session')}")
        await ws.send(json.dumps({"type": "start", "source": "mic", "channels": 1}))
        print("ack:", json.loads(await ws.recv()))

        sent = 0.0
        begin_at = None
        end_at = 0.0
        for i, block in enumerate(blocks):
            # 第 2 块开始「助理开口」，第 4 块结束
            if i == 1:
                begin_at = sent
                await ws.send(json.dumps({
                    "type": "tts_begin", "id": "demo1",
                    "text": ASSISTANT_TEXT, "voice": os.getenv("VOICE", "alice"), "at": round(begin_at, 2),
                }))
                print(f"→ tts_begin @ {begin_at:.1f}s")
            if i == 4:
                end_at = sent
                await ws.send(json.dumps({
                    "type": "tts_end", "id": "demo1", "at": round(end_at, 2),
                    "spoken_chars": len(ASSISTANT_TEXT), "state": "done",
                }))
                print(f"→ tts_end   @ {end_at:.1f}s")
            await ws.send(block)
            sent += len(block) / 2 / 16000
            await asyncio.sleep(0.05)  # 快进推流，别真等 3 秒

        if begin_at is None:
            print("音频太短，没有触发事件")
            return 2

        await ws.send(json.dumps({"type": "stop"}))
        print("已发送 stop，等收尾（要跑 ASR + 纪要）…")

        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=300)
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") in {"progress", "finalizing"}:
                print("  ", msg.get("type"), msg.get("stage") or "")
                continue
            if msg.get("type") != "done":
                continue
            print("done:", msg.get("ok"), msg.get("archive"))
            arch = Path(str(msg.get("archive") or ""))
            meta = arch / "meta.json"
            if meta.is_file():
                info = json.loads(meta.read_text(encoding="utf-8"))
                evs = info.get("assistant_events") or []
                print(f"meta.assistant_events = {json.dumps(evs, ensure_ascii=False)[:300]}")
            md = arch / "transcript.md"
            if md.is_file():
                text = md.read_text(encoding="utf-8")
                hit = "助理（我的声音）" in text
                print(f"transcript 含「助理（我的声音）」：{hit}")
                for line in text.splitlines():
                    if "助理" in line:
                        print("   ", line[:80])
            return 0 if msg.get("ok") else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--url", default="ws://127.0.0.1:8848/ws/live")
    ap.add_argument("--token", default="")
    args = ap.parse_args(argv)
    return asyncio.run(run(args.url, args.wav, args.token))


if __name__ == "__main__":
    raise SystemExit(main())
