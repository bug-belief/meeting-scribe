"""声纹对照诊断 v2

三个对照，用来定位「同人相似度只有 0.2」到底是模型/代码问题，还是样本问题：
  A. TTS 干净合成：同一音色、两段不同文本 → 模型应该认得出是同一人
  B. 真实录音：只取能量最高的若干 3s 窗（排除静音/噪声窗）→ 看同人稳定性
  C. 库内模板两两

用法：python tests/emb_diag2.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import cli_settings  # noqa: E402
from diarize import FunASREngine  # noqa: E402

TTS = "http://127.0.0.1:9880"


def norm(v):
    a = np.asarray(v, dtype="float32").reshape(-1)
    n = float(np.linalg.norm(a))
    return a / n if n > 0 else a


def cos(a, b):
    return float(norm(a) @ norm(b))


def tts(text: str, voice: str = "alice") -> Path:
    import urllib.request

    body = json.dumps({"text": text, "voice": voice}).encode("utf-8")
    req = urllib.request.Request(f"{TTS}/tts", data=body, headers={"Content-Type": "application/json"})
    out = Path("/tmp/_diag_tts.wav")
    with urllib.request.urlopen(req, timeout=180) as resp:
        out.write_bytes(resp.read())
    return out


def load_mono(path: Path, ch: int | None = None):
    pcm, sr = sf.read(str(path), dtype="float32")
    if pcm.ndim > 1:
        pcm = pcm[:, ch] if ch is not None else pcm.mean(axis=1)
    return np.ascontiguousarray(pcm, dtype="float32"), sr


def rms(x):
    return float(np.sqrt(np.mean(np.square(x)))) if x.size else 0.0


def main() -> int:
    settings = cli_settings()
    engine = FunASREngine(settings)

    # ---------- A. TTS 干净样本 ----------
    print("== A. TTS 同音色、不同文本（干净信号，检验模型/代码）")
    try:
        p1 = tts("这是一段用来检验声纹稳定性的测试语音，第一句。")
        p2 = tts("下午三点开会记得带电脑和那份合同文件的复印件。")
        a1, _ = load_mono(p1)
        a2, _ = load_mono(p2)
        e1, e2 = engine.embed(a1), engine.embed(a2)
        print(f"   A1 {len(a1)/16000:.1f}s  A2 {len(a2)/16000:.1f}s  → cos {cos(e1, e2):.3f}")
        print("   （同一音色，期望 ≥0.75；若远低于此，说明 embed 或模型用法有问题）")
    except Exception as exc:
        print(f"   跳过（TTS 不可用）：{exc}")

    # ---------- B. 真实录音，按能量挑窗 ----------
    root = Path(os.getenv("EMB_ROOT") or (Path.home() / "data/tenants/me/processed"))
    cases = [
        ("会议_1000 左声道", root / "2026-01-01_会议_1000/会议_1000.wav", 0),
        ("会议_1030 左声道", root / "2026-01-01_会议_1030/会议_1030.wav", 0),
        ("电话录音 单声道", root / "2026-01-01_电话录音-样本", None),
    ]
    for label, path, ch in cases:
        if not path.exists():
            continue
        if path.is_dir():
            wavs = sorted(path.glob("*.wav"))
            if not wavs:
                continue
            path = wavs[0]
        pcm, sr = load_mono(path, ch)
        total = len(pcm) / sr
        # 3s 窗、1.5s 步长，按 rms 排序
        wins = []
        step = int(1.5 * sr)
        size = int(3.0 * sr)
        for st in range(0, max(1, len(pcm) - size), step):
            clip = pcm[st: st + size]
            r = rms(clip)
            if r > 1e-4:
                wins.append((r, st, clip))
        wins.sort(key=lambda x: x[0], reverse=True)
        print(f"\n== B. {label}  {total:.1f}s  有效窗 {len(wins)} 个")
        if len(wins) < 2:
            print("   窗口不足")
            continue
        rms_all = [w[0] for w in wins]
        print(f"   窗 RMS：max {max(rms_all):.4f} / 中位 {np.median(rms_all):.4f}")
        top = wins[:5]
        embs = [engine.embed(w[2]) for w in top]
        print("   能量 top5 窗两两 cos：")
        sims = []
        for i in range(len(embs)):
            for j in range(i + 1, len(embs)):
                s = cos(embs[i], embs[j])
                sims.append(s)
                print(f"      w{i}(rms {top[i][0]:.4f}) vs w{j}(rms {top[j][0]:.4f}) = {s:.3f}")
        print(f"   → 均值 {sum(sims)/len(sims):.3f}  min {min(sims):.3f}  max {max(sims):.3f}")

        # 把 top5 平均成质心，再和最长一段比，模拟“簇质心 vs 单窗”
        centroid = np.mean([norm(e) for e in embs], axis=0)
        print(f"   top5 质心 vs 各窗：{[round(cos(centroid, e), 3) for e in embs]}")

    # ---------- C. 库内模板 ----------
    store = json.loads(Path(settings.speaker_store).read_text(encoding="utf-8"))
    for name, p in store.get("people", {}).items():
        ts = p.get("templates", [])
        print(f"\n== C. 库内 {name}  {len(ts)} 条")
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                print(f"   {ts[i].get('channel'):8s}({ts[i].get('duration',0):5.1f}s) vs "
                      f"{ts[j].get('channel'):8s}({ts[j].get('duration',0):5.1f}s) = {cos(ts[i]['emb'], ts[j]['emb']):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
