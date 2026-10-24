"""验证「接一句」：真实会话进行中，让服务端用实时上下文生成回复。

流程：连 WS → 推一段 16k 单声道 wav（模拟麦克风）→ 等实时行落定
     → POST /api/assistant/reply {session} → 打印模型给出的稿子。

    python tests/ws_reply.py --wav /tmp/seg16k.wav --token xxx
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.request
import wave

FRAME_BYTES = 19200  # 600ms @ 16k/s16/单声道


def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.read().decode()[:300]}


async def run(ws_url: str, api: str, token: str, wav: str) -> int:
    import websockets

    with wave.open(wav, "rb") as fh:
        if fh.getframerate() != 16000 or fh.getnchannels() != 1:
            print("❌ 要 16k 单声道 wav")
            return 2
        data = fh.readframes(fh.getnframes())

    sep = "&" if "?" in ws_url else "?"
    async with websockets.connect(f"{ws_url}{sep}k={token}", max_size=None, ping_interval=25) as ws:
        ready = json.loads(await ws.recv())
        sid = ready.get("session")
        print(f"ready: session={sid}")
        await ws.send(json.dumps({"type": "start", "source": "mic", "channels": 1}))
        print("ack:", json.loads(await ws.recv()))

        for i in range(0, len(data), FRAME_BYTES):
            await ws.send(data[i:i + FRAME_BYTES])
            await asyncio.sleep(0.05)

        print("推流完毕，等实时行落定…")
        await asyncio.sleep(4)

        # 会话还在，服务端能拿到 LiveSession.lines
        reply = await asyncio.to_thread(
            post_json, f"{api}/api/assistant/reply?k={token}", {"session": sid})
        print("代答：", json.dumps(reply, ensure_ascii=False)[:400])

        # 反面：不存在的会话应当 404
        bad = await asyncio.to_thread(
            post_json, f"{api}/api/assistant/reply?k={token}", {"session": "not-a-session"})
        print("不存在的会话：", bad.get("status"), bad.get("error", "")[:60])

        await ws.send(json.dumps({"type": "stop"}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=300)
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") == "done":
                print("收尾:", msg.get("ok"), msg.get("archive"))
                break
        return 0 if reply.get("ok") else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--ws", default="ws://127.0.0.1:8848/ws/live")
    ap.add_argument("--api", default="http://127.0.0.1:8848")
    ap.add_argument("--token", default="")
    args = ap.parse_args(argv)
    return asyncio.run(run(args.ws, args.api, args.token, args.wav))


if __name__ == "__main__":
    raise SystemExit(main())
