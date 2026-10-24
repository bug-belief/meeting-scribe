"""WebSocket 冒烟测试：把一段 16k wav 当作麦克风推给 server.py，看全链路是否通。

    python tests/ws_smoke.py --wav /tmp/test16k.wav [--url ws://127.0.0.1:8848/ws/live] [--realtime]
                             [--source mic|system|dual]

--realtime 会按 0.6s 一块真实速度推送，模拟真人说话节奏。
wav 是双声道时自动按「双路分轨」模式推流（左=麦克风、右=系统声音）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave

FRAME_BYTES = 19200  # 600 ms @ 16k / s16 / 单声道（双声道时 ×2）


async def run(url: str, wav: str, realtime: bool, source: str) -> int:
    import websockets

    with wave.open(wav, "rb") as fh:
        if fh.getframerate() != 16000 or fh.getsampwidth() != 2:
            print(f"❌ 需要 16k/s16 的 wav，实际 {fh.getframerate()}Hz "
                  f"{fh.getnchannels()}ch {fh.getsampwidth() * 8}bit")
            return 2
        channels = fh.getnchannels()
        data = fh.readframes(fh.getnframes())

    if source == "auto":
        source = "dual" if channels == 2 else "mic"
    chunk_bytes = FRAME_BYTES * channels

    messages: list[dict] = []
    async with websockets.connect(url, max_size=None, ping_interval=25) as ws:
        ready = json.loads(await ws.recv())
        print(f"ready: session={ready.get('session')} chunk={ready.get('chunk_ms')}ms "
              f"sources={ready.get('sources')}")

        # 先声明采集方式，服务端据此决定声道数与切块大小
        await ws.send(json.dumps({"type": "start", "source": source, "channels": channels}))
        ack = json.loads(await ws.recv())
        print(f"ack: source={ack.get('source')} channels={ack.get('channels')}")
        if ack.get("type") != "source":
            print(f"❌ 没收到 source 确认：{ack}")
            return 1

        finished = asyncio.Event()

        async def reader():
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    messages.append(msg)
                    kind = msg.get("type")
                    if kind == "partial":
                        text = (msg.get("text") or "")[-30:]
                        print(f"\r   … {msg.get('silent_ms', 0):>4}ms 静音 | {text}", end="", flush=True)
                    elif kind == "line":
                        print(f"\n   ▸ [{msg.get('start')}s] {msg.get('text')}")
                    elif kind in {"finalizing", "error"}:
                        print(f"\n[{kind}] {msg.get('message', '')}")
                    elif kind == "done":
                        print(f"\n[done] ok={msg.get('ok')} archive={msg.get('archive')}")
                        print(f"       source={msg.get('source')} channels={msg.get('channels')} "
                              f"transcript {len(msg.get('transcript') or '')} 字 / "
                              f"report {len(msg.get('report') or '')} 字 / 耗时 {msg.get('elapsed')}s")
                        finished.set()
                        return
            except Exception as exc:
                print(f"\n[reader 结束] {type(exc).__name__}: {exc}")
                finished.set()

        task = asyncio.create_task(reader())

        total = (len(data) + chunk_bytes - 1) // chunk_bytes
        for i in range(total):
            await ws.send(data[i * chunk_bytes:(i + 1) * chunk_bytes])
            if realtime:
                await asyncio.sleep(0.6)
        print(f"\n已推送 {total} 块（{len(data) / (32000 * channels):.1f}s 音频，{channels} 声道），发送 stop…")
        await ws.send(json.dumps({"type": "stop"}))

        try:
            await asyncio.wait_for(finished.wait(), timeout=600)
        except asyncio.TimeoutError:
            print("❌ 超时：600s 内没有收到 done")
            task.cancel()
            return 1
        task.cancel()

    done = next((m for m in messages if m.get("type") == "done"), None)
    lines = [m for m in messages if m.get("type") == "line"]
    print(f"\n小结：{len(lines)} 行实时字幕 / done.ok={bool(done and done.get('ok'))}")
    if done and done.get("transcript"):
        print("-" * 60)
        print("\n".join(done["transcript"].splitlines()[:12]))
    return 0 if done and done.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8848/ws/live")
    parser.add_argument("--wav", required=True)
    parser.add_argument("--realtime", action="store_true")
    parser.add_argument("--source", default="auto", choices=["auto", "mic", "system", "dual"])
    args = parser.parse_args()
    return asyncio.run(run(args.url, args.wav, args.realtime, args.source))


if __name__ == "__main__":
    sys.exit(main())
