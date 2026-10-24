"""端到端量一次「说完了 → 听到第一声」的等待时间。

流程和页面完全一致：
    推 6 秒真实语音（「云助理，看一下明天杭州天气怎么样」）+ 静音尾巴
    → 等 line（静音断句定稿）
    → POST /api/assistant/reply（服务端跑 DeepSeek）
    → 按前端的切句规则切出首句，POST /api/tts 合成
把三段耗时分别打出来，用来判断该优化哪一段。
"""

import asyncio
import json
import os
import sys
import time
import wave
from array import array
from pathlib import Path

SAMPLE_RATE = 16000
PATH = (sys.argv[1] if len(sys.argv) > 1 else
        str(Path.home() / "data/tenants/me/processed/2026-01-01_会议_0930/会议_0930.wav"))
START, END = (float(sys.argv[2]), float(sys.argv[3])) if len(sys.argv) > 3 else (0.0, 6.0)

MAX_UNIT = 16


def split_script(text: str) -> list[str]:
    """和 web/app.js 的 splitScript 同一套规则（首句再切一刀）。"""
    import re
    raw = re.sub(r"\s+", " ", text or "").strip()
    if not raw:
        return []
    parts = re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", raw)
    out = []
    for p in parts:
        s = p.strip()
        if not s:
            continue
        if len(s) <= MAX_UNIT:
            out.append(s)
        else:
            out.extend(q.strip() for q in re.findall(r"[^，,、]+[，,、]?", s) if q.strip())
    merged: list[str] = []
    for s in out:
        if merged and len(merged[-1] + s) <= MAX_UNIT:
            merged[-1] += s
        else:
            merged.append(s)
    if merged and len(merged[0]) > 10:
        m = re.match(r"^[\s\S]{4,10}[，,、]", merged[0])
        if m:
            rest = merged[0][len(m.group(0)):]
            if rest and len(rest) <= MAX_UNIT:
                merged[0:1] = [m.group(0), rest]
    return merged


async def main() -> None:
    import urllib.request

    import websockets

    token = os.environ["WEB_TOKEN"]
    with wave.open(PATH, "rb") as fh:
        ch = fh.getnchannels()
        fh.setpos(int(START * SAMPLE_RATE))
        data = array("h")
        data.frombytes(fh.readframes(int((END - START) * SAMPLE_RATE)))
        data = data[0::ch]
    pcm = data.tobytes() + b"\x00" * (int(1.5 * SAMPLE_RATE) * 2)

    url = f"ws://127.0.0.1:8848/ws/live?k={token}"
    async with websockets.connect(url, max_size=None) as ws:
        ready = json.loads(await ws.recv())
        session = str(ready.get("session") or "")
        await ws.send(json.dumps({"type": "start", "source": "mic", "channels": 1}))
        await ws.recv()
        line = None
        cue = None

        async def reader() -> None:
            nonlocal line, cue
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "line" and line is None:
                    line = msg
                    cue = time.time()

        task = asyncio.create_task(reader())
        for off in range(0, len(pcm), 19200):
            await ws.send(pcm[off:off + 19200])
            await asyncio.sleep(0.05)
        sent_done = time.time()
        while line is None:
            await asyncio.sleep(0.05)
        drain = cue - sent_done
        print(f"会话 {session}｜定稿行：{line.get('text')}")
        print(f"  推流结束 → 定稿：{drain * 1000:.0f} ms"
              f"（真机还会多 0.75s 静音断句 + 最多 1 个 600ms 块）")
        tail = 0.75

        t0 = time.time()
        req = urllib.request.Request(
            f"http://127.0.0.1:8848/api/assistant/reply?k={token}",
            data=json.dumps({"session": session}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data2 = json.loads(resp.read())
        think = time.time() - t0
        print(f"  代答（DeepSeek）：{think * 1000:.0f} ms → {data2.get('text')}")

        units = split_script(data2.get("text") or "")
        print(f"  切句：{units}")
        t1 = time.time()
        body = json.dumps({"text": units[0], "voice": "alice"}).encode()
        req2 = urllib.request.Request(
            "http://127.0.0.1:9880/tts", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req2, timeout=120) as resp:
            wav = resp.read()
        synth = time.time() - t1
        print(f"  首句合成：{synth * 1000:.0f} ms（{len(wav)} 字节）")
        print(f"\n合计等待 ≈ {(tail + think + synth) * 1000:.0f} ms"
              f"（ASR {tail * 1000:.0f} + 模型 {think * 1000:.0f} + 合成 {synth * 1000:.0f}）")
        task.cancel()


asyncio.run(main())
