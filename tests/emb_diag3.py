"""声纹 v3：验证「只取高质量窗口」后，跨录音能不能认出同一个人。

对比三种取窗策略、两两跨录音的质心相似度：
  1) 现状 gate = max(0.002, median*ratio)     —— 现有逻辑
  2) 相对门限 = max_rms * 0.25
  3) 相对门限 + 只取 topK 高能窗

用法：python tests/emb_diag3.py
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


def cos(a, b):
    return float(norm(a) @ norm(b))


def main() -> int:
    settings = cli_settings()
    engine = FunASREngine(settings)
    root = Path(os.getenv("EMB_ROOT") or (Path.home() / "data/tenants/me/processed"))

    clips = [
        ("1145-左", root / "2026-01-01_会议_1000/会议_1000.wav", 0),
        ("1326-左", root / "2026-01-01_会议_1030/会议_1030.wav", 0),
        ("1145-右", root / "2026-01-01_会议_1000/会议_1000.wav", 1),
        ("1309-左", root / "2026-01-01_会议_1100/会议_1100.wav", 0),
        ("1336-左", root / "2026-01-01_会议_1130/会议_1130.wav", 0),
    ]

    win, hop = 3.0, 1.5
    pools: dict[str, list[tuple[float, np.ndarray]]] = {}
    for label, path, ch in clips:
        if not path.is_file():
            continue
        pcm, sr = sf.read(str(path), dtype="float32")
        if pcm.ndim > 1:
            pcm = pcm[:, ch]
        pcm = np.ascontiguousarray(pcm, dtype="float32")
        size, step = int(win * sr), int(hop * sr)
        rows = []
        for st in range(0, max(1, len(pcm) - size), step):
            seg = pcm[st: st + size]
            r = float(np.sqrt(np.mean(np.square(seg))))
            if r > 1e-5:
                rows.append((r, st, seg))
        if len(rows) < 3:
            continue
        rmss = np.array([r[0] for r in rows])
        med, mx = float(np.median(rmss)), float(rmss.max())
        print(f"\n{label}: {len(pcm)/sr:.1f}s  窗 {len(rows)}  RMS 中位 {med:.4f}  max {mx:.4f}")
        print(f"   现状 gate=max(0.002, med*0.5)={max(0.002, med*0.5):.4f} → 保留 {(rmss >= max(0.002, med*0.5)).sum()} 窗")
        print(f"   相对 gate=max*0.25={mx*0.25:.4f} → 保留 {(rmss >= mx*0.25).sum()} 窗")

        # 三种策略的质心
        def centroid(sel_rows):
            embs = [norm(engine.embed(r[2])) for r in sel_rows]
            return norm(np.mean(embs, axis=0)), len(embs)

        cur = [r for r in rows if r[0] >= max(0.002, med * 0.5)]
        rel = [r for r in rows if r[0] >= mx * 0.25]
        top = sorted(rows, key=lambda x: x[0], reverse=True)[:6]
        pools[label] = {
            "现状": centroid(cur),
            "相对": centroid(rel),
            "top6": centroid(top),
        }

    keys = [k for k in ["1145-左", "1326-左", "1309-左", "1336-左"] if k in pools]
    print("\n" + "=" * 66)
    print("跨录音同人质心相似度（这几段左声道都应是同一个人）")
    print("=" * 66)
    for a in range(len(keys)):
        for b in range(a + 1, len(keys)):
            ka, kb = keys[a], keys[b]
            cells = []
            for mode in ("现状", "相对", "top6"):
                ea, na = pools[ka][mode]
                eb, nb = pools[kb][mode]
                cells.append(f"{mode} {cos(ea, eb):.3f}({na}/{nb}窗)")
            print(f"  {ka} vs {kb}: " + "   ".join(cells))

    if "1145-右" in pools:
        print("\n  交叉对照（右声道=对方，应显著更低）：")
        for k in keys:
            print(f"    1145-右 vs {k}: 相对 {cos(pools['1145-右']['相对'][0], pools[k]['相对'][0]):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
