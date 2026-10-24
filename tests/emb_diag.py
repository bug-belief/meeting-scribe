"""声纹稳定性诊断：同一段录音前后半 vs 库里跨样本，判断「差异」是场景还是样本质量。

用法：python tests/emb_diag.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import cli_settings  # noqa: E402
from diarize import FunASREngine  # noqa: E402


def norm(v):
    a = np.asarray(v, dtype="float32").reshape(-1)
    n = float(np.linalg.norm(a))
    return a / n if n > 0 else a


def pick_channel(pcm, ch):
    if pcm.ndim == 1:
        return pcm
    return pcm[:, ch]


def seg_emb(engine, pcm, sr, start, dur, min_energy=True):
    clip = pcm[int(start * sr): int((start + dur) * sr)]
    clip = np.ascontiguousarray(clip, dtype="float32")
    if min_energy:
        # 掐掉头尾 0.15s 的静音判定，避免全是噪声
        peak = float(np.max(np.abs(clip))) if clip.size else 0.0
        if peak < 1e-4:
            return None
    return engine.embed(clip)


def main() -> int:
    settings = cli_settings()
    engine = FunASREngine(settings)
    # 默认 <家目录>/data/tenants/<owner>/processed；换机器/换目录用 EMB_ROOT 覆盖
    root = Path(os.getenv("EMB_ROOT") or (Path.home() / "data/tenants/me/processed"))

    cases = [
        ("会议_1000 左(我)", root / "2026-01-01_会议_1000", "会议_1000.wav", 0),
        ("电话录音 单声道", root / "2026-01-01_电话录音-样本", None, None),
    ]

    for label, folder, fname, ch in cases:
        if not folder.is_dir():
            continue
        if fname is None:
            wavs = sorted(folder.glob("*.wav"))
            if not wavs:
                continue
            wav = wavs[0]
        else:
            wav = folder / fname
        if not wav.is_file():
            continue
        pcm, sr = sf.read(str(wav), dtype="float32")
        if pcm.ndim > 1 and ch is not None:
            pcm = pick_channel(pcm, ch)
        elif pcm.ndim > 1:
            pcm = pcm.mean(axis=1)
        total = len(pcm) / sr
        print(f"\n== {label}  {wav.name}  {total:.1f}s  sr={sr}")
        if total < 8:
            print("   太短，跳过")
            continue

        # 同段不同时长：3s / 8s / 20s / 前半 vs 后半
        for d in (3.0, 8.0, 20.0):
            if total >= d * 2:
                a = seg_emb(engine, pcm, sr, 0, d)
                b = seg_emb(engine, pcm, sr, total - d, d)
                if a is not None and b is not None:
                    print(f"   前后各 {d:4.1f}s  → cos {float(norm(a) @ norm(b)):.3f}")
        half = total / 2
        a = seg_emb(engine, pcm, sr, 0, half)
        b = seg_emb(engine, pcm, sr, half, half)
        if a is not None and b is not None:
            print(f"   前半 vs 后半 ({half:.1f}s each) → cos {float(norm(a) @ norm(b)):.3f}")
        # 3s 窗口之间的抖动：取 5 个 3s 窗两两比
        wins = []
        for i in range(5):
            st = i * 3.0
            if st + 3.0 <= total:
                e = seg_emb(engine, pcm, sr, st, 3.0)
                if e is not None:
                    wins.append(norm(e))
        if len(wins) >= 2:
            sims = [float(wins[i] @ wins[j]) for i in range(len(wins)) for j in range(i + 1, len(wins))]
            print(f"   5 个 3s 窗两两：min {min(sims):.3f} / 均值 {sum(sims)/len(sims):.3f} / max {max(sims):.3f}")

    # 库里现有模板两两
    import json
    store = json.loads(Path(settings.speaker_store).read_text(encoding="utf-8"))
    for name, p in store.get("people", {}).items():
        ts = p.get("templates", [])
        print(f"\n== 库内 {name}  {len(ts)} 条")
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                s = float(norm(ts[i]["emb"]) @ norm(ts[j]["emb"]))
                print(f"   {ts[i].get('channel'):8s}({ts[i].get('duration',0):5.1f}s) vs "
                      f"{ts[j].get('channel'):8s}({ts[j].get('duration',0):5.1f}s) = {s:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
