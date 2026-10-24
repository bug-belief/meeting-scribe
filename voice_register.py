#!/usr/bin/env python3
"""把一段录音里的声音登记成「音色」，供 tts_server.py 做 zero-shot 克隆。

用法示例
--------
    # 从双声道录音里取左声道（= 我的麦克风）第 3~11 秒
    python voice_register.py --name zhangsan \
        --audio ~/data/processed/2026-01-01_会议_1000/mix.wav \
        --channel left --start 3 --end 11

    # 已经有干净的 16k 单声道片段，直接给文字
    python voice_register.py --name zhangsan --audio me.wav --text "这是我录的一句话。"

文字不填时会自动调用转写服务的 `/api/asr/quick` 转一遍（需要服务在跑）。
参考音 3~10 秒最好，超过 30 秒 CosyVoice 会截断，音色反而不稳。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

DEFAULT_VOICES = Path.home() / "tts" / "voices"

# O-14：服务端口从 Settings 读，不再写死 8848 / 9880。
# 拿不到配置时（比如只复制了单个脚本到别的机器）退回历史默认值，保持可用。
try:
    from config import SETTINGS as _SETTINGS

    _WEB_PORT = _SETTINGS.web_port
    _TTS_PORT = _SETTINGS.tts_port
except Exception:  # noqa: BLE001 - 缺依赖/缺配置文件都不应让 CLI 起不来
    _WEB_PORT = 8848
    _TTS_PORT = 9880


def sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_duration(path: Path, ffprobe: str = "ffprobe") -> float:
    out = sh([ffprobe, "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float((out.stdout or "0").strip())
    except ValueError:
        return 0.0


def extract(audio: Path, start: float, end: float, channel: str, dst: Path,
            ffmpeg: str = "ffmpeg") -> None:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if end > start:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", str(audio)]
    if channel == "left":
        cmd += ["-af", "pan=mono|c0=c0"]
    elif channel == "right":
        cmd += ["-af", "pan=mono|c0=c1"]
    cmd += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)]
    proc = sh(cmd)
    if proc.returncode != 0 or not dst.is_file():
        raise SystemExit(f"ffmpeg 抽取失败：\n{(proc.stderr or '')[-600:]}")


def asr_quick(wav: Path, base: str, token: str = "") -> str:
    url = base.rstrip("/") + "/api/asr/quick"
    if token:
        url += f"?k={token}"
    data = wav.read_bytes()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "audio/wav"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise SystemExit(f"调用转写服务失败（{url}）：{exc}")
    text = (payload.get("text") or "").strip()
    if not text:
        raise SystemExit(f"转写没出字：{payload}")
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="登记 zero-shot 音色")
    ap.add_argument("--name", help="音色名（也是说话人名），之后合成时用这个名字")
    ap.add_argument("--profile", default="", help="场景/语域：开会、日常、老家方言…不填=沿用老布局")
    ap.add_argument("--audio", help="来源音频（wav/mp3/m4a 都行）")
    ap.add_argument("--start", type=float, default=0.0, help="起点秒，默认 0")
    ap.add_argument("--end", type=float, default=0.0, help="终点秒，0 表示到结尾")
    ap.add_argument("--channel", choices=["mix", "left", "right"], default="mix",
                    help="立体声取哪一路；mix=直接下混")
    ap.add_argument("--text", default="", help="这段音频的逐字稿；不填则自动转写")
    ap.add_argument("--asr", default=f"http://127.0.0.1:{_WEB_PORT}", help="转写服务地址")
    ap.add_argument("--token", default=os.environ.get("WEB_TOKEN", ""), help="转写服务口令")
    ap.add_argument("--voices", default=str(DEFAULT_VOICES), help="音色根目录")
    ap.add_argument("--owner", default=os.environ.get("TENANT_OWNER") or "me",
                    help="音色归属人（默认 me）。克隆音色只有本人能用，"
                         "所以这条必须是真的主人，别图省事留空")
    ap.add_argument("--tts", default=f"http://127.0.0.1:{_TTS_PORT}", help="合成服务地址，用于热加载")
    ap.add_argument("--list", action="store_true", help="列出已登记音色")
    args = ap.parse_args(argv)

    voices_root = Path(args.voices).expanduser()
    if args.list:
        rows = []
        if voices_root.is_dir():
            # 两层：<speaker>/<profile>/prompt.txt
            for f in sorted(voices_root.glob("*/*/prompt.txt")):
                rows.append((f"{f.parent.parent.name}@{f.parent.name}", f))
            # 一层：<name>/prompt.txt
            for f in sorted(voices_root.glob("*/prompt.txt")):
                rows.append((f.parent.name, f))
        for vid, f in rows:
            print(f"{vid}\t{f.read_text(encoding='utf-8').strip()[:40]}")
        return 0

    if not args.name or not args.audio:
        ap.error("需要 --name 和 --audio")

    src = Path(args.audio).expanduser()
    if not src.is_file():
        raise SystemExit(f"找不到音频：{src}")

    speaker = args.name.strip()
    profile = (args.profile or "").strip()
    folder = (voices_root / speaker / profile) if profile else (voices_root / speaker)
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / "prompt.wav"
    extract(src, args.start, args.end or 0.0, args.channel, wav)

    seconds = probe_duration(wav)
    if seconds < 2.0:
        wav.unlink(missing_ok=True)
        raise SystemExit(f"截出来只有 {seconds:.1f}s，太短；把 --start/--end 调宽一点")
    if seconds > 30.0:
        print(f"[warn] 参考音 {seconds:.1f}s，CosyVoice 只用前 30s，建议截到 3~10s", file=sys.stderr)

    text = args.text.strip()
    if not text:
        print(f"[info] 参考音 {seconds:.1f}s，调用 {args.asr} 转写…")
        text = asr_quick(wav, args.asr, args.token)
    (folder / "prompt.txt").write_text(text + "\n", encoding="utf-8")

    voice_id = speaker if (not profile or profile == "默认") else f"{speaker}@{profile}"
    meta = {
        "name": voice_id,
        "speaker": speaker,
        "profile": profile or "默认",
        "owner": args.owner,
        "source": str(src),
        "start": args.start,
        "end": args.end,
        "channel": args.channel,
        "seconds": round(seconds, 2),
        "text": text,
    }
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                      encoding="utf-8")

    print(f"[ok] 音色 {voice_id} 已登记：{wav}（{seconds:.1f}s，场景 {meta['profile']}）")
    print(f"     逐字稿：{text}")

    try:
        req = urllib.request.Request(f"{args.tts.rstrip('/')}/reload", data=b"{}",
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            print(f"[ok] 合成服务已热加载：{resp.read().decode('utf-8')}")
    except Exception as exc:
        print(f"[warn] 合成服务没热加载（{exc}），重启 tts 服务即可生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
