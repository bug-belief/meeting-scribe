"""验证助理播报会进入会话上下文（且不把内部字段暴露给前端）。

流程：连 WS → start → 推 0.5 秒静音 → tts_begin / tts_end → ping 拿 snapshot，
检查 lines 里有一条「助理（我的声音）」，文本是日志原文，且没有 `_eid`。

    python tests/ws_tts_context.py --token xxx
"""

from __future__ import annotations

import os
import argparse
import asyncio
import json


async def run(ws_url: str, token: str, voice: str = os.getenv("VOICE", "alice"),
              expect: str = "助理（我的声音）") -> int:
    import websockets

    sep = "&" if "?" in ws_url else "?"
    async with websockets.connect(f"{ws_url}{sep}k={token}", max_size=None) as ws:
        ready = json.loads(await ws.recv())
        assert ready.get("type") == "ready", ready
        await ws.send(json.dumps({"type": "start", "source": "mic", "channels": 1}))
        print("ack:", json.loads(await ws.recv()))

        # 0.5 秒静音（16k / s16）
        await ws.send(b"\x00" * 16000)
        await asyncio.sleep(0.2)

        text = "明天有小雨，11到17度，降雨概率69%"
        await ws.send(json.dumps({"type": "tts_begin", "id": "t1", "text": text,
                                  "voice": voice, "at": 0.2}))
        await ws.send(json.dumps({"type": "tts_end", "id": "t1", "at": 0.8,
                                  "spoken_chars": len(text), "state": "done"}))
        await ws.send(json.dumps({"type": "ping"}))
        snap = json.loads(await ws.recv())
        print("snapshot lines:", json.dumps(snap.get("lines"), ensure_ascii=False)[:400])

        lines = snap.get("lines") or []
        got = [x for x in lines if x.get("who") == expect]
        ok = bool(got) and got[0].get("text") == text and "_eid" not in got[0]
        print("\n结果：", "✅ 上下文里是日志原文，内部字段已清掉" if ok else "❌ 不符合预期")
        await ws.send(json.dumps({"type": "stop"}))
        try:
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("type") in {"done", "error"}:
                    break
        except Exception:
            pass
        return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default="ws://127.0.0.1:8848/ws/live")
    ap.add_argument("--token", required=True)
    ap.add_argument("--voice", default=os.getenv("VOICE", "alice"),
                    help="tts_begin 上报的音色 id（跑自己的实例时传你的音色名）")
    ap.add_argument("--expect", default="助理（我的声音）", help="期望的标签")
    args = ap.parse_args()
    return asyncio.run(run(args.ws, args.token, args.voice, args.expect))


if __name__ == "__main__":
    raise SystemExit(main())
