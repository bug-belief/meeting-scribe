"""端到端验证：助理播报的声音不会被当成「对方」。

构造一段双声道音频：
    左声道 = 你说 8 秒，然后静音
    右声道 = 静音 10 秒，然后放助理的合成音 4.4 秒，再静音（模拟 CABLE 回环）
同时按真实时间上报 tts_begin / tts_end。
预期：左声道的话正常出「我」，右声道的合成音**不产生任何 line**。

    python tests/ws_echo_e2e.py --user-wav a.wav --tts-wav b.wav --token xxx
"""

from __future__ import annotations

import os
import argparse
import asyncio
import json
import wave
from array import array

SAMPLE_RATE = 16000
FRAME_SEC = 0.6


def read_mono(path: str, max_sec: float) -> array:
    with wave.open(path, "rb") as fh:
        assert fh.getframerate() == SAMPLE_RATE and fh.getnchannels() == 1, "要 16k 单声道"
        frames = fh.readframes(int(max_sec * SAMPLE_RATE))
    data = array("h")
    data.frombytes(frames)
    return data


def build_stereo(user: array, tts: array, total_sec: float = 21.0) -> bytes:
    n = int(total_sec * SAMPLE_RATE)
    left = array("h", [0]) * n
    right = array("h", [0]) * n
    for i, v in enumerate(user[: min(len(user), 8 * SAMPLE_RATE)]):
        left[i] = v
    start = 10 * SAMPLE_RATE
    for i, v in enumerate(tts):
        if start + i < n:
            right[start + i] = v
    out = array("h", [0]) * (n * 2)
    out[0::2] = left
    out[1::2] = right
    return out.tobytes()


async def run(ws_url: str, token: str, stereo: bytes, tts_seconds: float) -> int:
    import websockets

    sep = "&" if "?" in ws_url else "?"
    lines = []
    async with websockets.connect(f"{ws_url}{sep}k={token}", max_size=None) as ws:
        ready = json.loads(await ws.recv())
        print("ready:", ready.get("session"))
        await ws.send(json.dumps({"type": "start", "source": "dual", "channels": 2}))
        print("ack:", json.loads(await ws.recv()))

        # 后台读：websockets 17 没有 recv_nowait()，必须用异步迭代器收。
        # （之前用 recv_nowait + try/except，异常被吞掉 → 脚本永远「0 行」假绿。）
        async def reader():
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("type") == "line":
                        lines.append(msg)
                        print("line:", round(msg.get("start", 0), 1),
                              msg.get("who"), str(msg.get("text", ""))[:30])
            except Exception:
                pass

        task = asyncio.create_task(reader())

        block = int(FRAME_SEC * SAMPLE_RATE * 2 * 2)
        sent = 0.0
        began = ended = False
        for off in range(0, len(stereo), block):
            await ws.send(stereo[off:off + block])
            sent += FRAME_SEC
            if not began and sent >= 10.0:
                await ws.send(json.dumps({
                    "type": "tts_begin", "id": "e2e", "text": "明天有小雨，11到17度，降雨概率69%",
                    "voice": os.getenv("VOICE", "alice"), "at": 10.0}))
                began = True
            if not ended and sent >= 10.0 + tts_seconds:
                await ws.send(json.dumps({
                    "type": "tts_end", "id": "e2e", "at": 10.0 + tts_seconds,
                    "spoken_chars": 20, "state": "done"}))
                ended = True
            await asyncio.sleep(0.03)

        print("推流完毕，等识别收尾…")
        await asyncio.sleep(8)
        task.cancel()

        peer = [x for x in lines if x.get("who") == "对方"]
        mine = [x for x in lines if x.get("who") == "我"]
        print(f"\n行数：我 {len(mine)} / 对方 {len(peer)}")
        ok = not peer and bool(mine)
        print("结果：", "✅ 合成音没被当成对方" if ok else "❌ 仍有回声被标成对方")
        return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default="ws://127.0.0.1:8848/ws/live")
    ap.add_argument("--token", required=True)
    ap.add_argument("--user-wav", required=True)
    ap.add_argument("--tts-wav", required=True)
    args = ap.parse_args()

    user = read_mono(args.user_wav, 8.0)
    tts = read_mono(args.tts_wav, 30.0)
    stereo = build_stereo(user, tts)
    return asyncio.run(run(args.ws, args.token, stereo, len(tts) / SAMPLE_RATE))


if __name__ == "__main__":
    raise SystemExit(main())
