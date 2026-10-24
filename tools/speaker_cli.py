"""声纹库命令行：登记、查看、确认、改名。

常用流程
--------
1. 跑完一次转写，看看识别成什么样：
       python tools/speaker_cli.py show --dir processed/2026-01-01_电话录音
2. 确认某个簇是谁（会用该簇的 embedding 入库，并改写转写稿标签）：
       python tools/speaker_cli.py label --dir processed/2026-01-01_电话录音 --cluster 1 --name 张三
3. 直接拿一段干净的单人录音入库：
       python tools/speaker_cli.py enroll --name 张三 --audio ~/samples/zhangsan.m4a --channel close
4. 看看某段录音像谁：
       python tools/speaker_cli.py who --audio ~/samples/test.m4a
5. 维护：
       python tools/speaker_cli.py list
       python tools/speaker_cli.py rename --old 张三 --new 张老师
       python tools/speaker_cli.py forget --name 张三
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 本文件躺在 tools/ 下，项目根不在 sys.path 里，得自己补上，
# 否则 `from config / speakers / utils` 全部 ImportError（这几行必须在 import 前执行）。
# 注：ruff 的 E402 对 `sys.path` 修改有内置豁免，这里不需要 noqa（已实测对照）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import cli_settings, ensure_dirs
from speakers import AUTO_MARGIN, CHANNEL_THRESHOLDS, SpeakerStore, channel_for_audio
from utils import (
    PipelineError,
    fmt_hms,
    get_logger,
    probe_audio_info,
    safe_stem,
    setup_logging,
    to_wav16k,
)

logger = get_logger("speaker")


def _store(settings) -> SpeakerStore:
    return SpeakerStore(settings.speaker_store, settings.speaker_max_templates)


def _embed_audio(audio: Path, settings, max_sec: float = 60.0, min_ratio: float = 0.25):
    """音频 → (归一化声纹, 时长秒)。

    2026-10-19 修：以前直接取前 60s 整段平均，静音和对方的声音一起搅进去，
    造出过一条「和谁都不像」的脏模板（现库内 phone 75.6s 那条，vs 同人 0.21~0.33）。
    现在走 VAD → 3s 窗 → 相对能量门限 → 按能量加权平均，只让真正的语音进质心。
    """
    import numpy as np
    import soundfile as sf

    from diarize import FunASREngine

    audio = Path(audio).expanduser()
    if not audio.is_file():
        raise PipelineError(f"音频不存在：{audio}")
    wav = to_wav16k(audio, settings.work_dir / "_enroll" / f"{safe_stem(audio.stem)}.wav", settings.ffmpeg_bin)
    pcm, sr = sf.read(str(wav), dtype="float32")
    if pcm.ndim > 1:
        pcm = pcm.mean(axis=1)
    pcm = np.ascontiguousarray(pcm, dtype="float32")
    engine = FunASREngine(settings)
    if sr != 16000:
        raise PipelineError(f"内部转码异常，采样率 {sr} != 16000")
    total = len(pcm) / sr
    limit = min(int(max_sec * sr), len(pcm))

    # 1) 语音区间（VAD 失败就退化成整段，但后面仍有能量门限兜底）
    regions: list[tuple[int, int]] = []
    try:
        regions = engine.speech_regions(wav)
    except Exception:
        regions = []
    limit_ms = int(limit * 1000 / sr)
    spans = [(a, min(b, limit_ms)) for a, b in regions if a < limit_ms] or [(0, limit_ms)]

    # 2) 3s 窗 + 能量
    size, hop = int(3.0 * sr), int(1.5 * sr)
    rows: list[tuple[float, int, int]] = []
    for a, b in spans:
        st, end = int(a * sr / 1000), int(b * sr / 1000)
        if end - st < int(0.6 * sr):
            continue
        if end - st <= int(size * 1.5):
            seg = pcm[st:end]
            rows.append((float(np.sqrt(np.mean(seg**2))), st, end))
            continue
        for s in range(st, end - size + 1, hop):
            seg = pcm[s: s + size]
            rows.append((float(np.sqrt(np.mean(seg**2))), s, s + size))
    if not rows:
        return engine.embed(pcm[:limit]), min(total, max_sec)

    peak = max(r[0] for r in rows)
    gate = max(0.001, peak * min_ratio)
    sel = [r for r in rows if r[0] >= gate] or rows

    # 3) 按能量加权平均（弱窗权重低，但仍贡献一点上下文）
    vecs, weights = [], []
    for energy, s, e in sel:
        vecs.append(engine.embed(pcm[s:e]))
        weights.append(max(energy, 1e-6))
    mat = np.vstack(vecs)
    w = np.asarray(weights, dtype="float32")
    w = w / w.sum()
    cent = np.average(mat, axis=0, weights=w)
    n = float(np.linalg.norm(cent))
    return (cent / n if n > 0 else cent), min(total, max_sec)


def _guess_channel(audio: Path, settings, override: str | None = None) -> str:
    """优先用命令行指定，其次 .env，最后按原始采样率/码率自动判定。"""
    if override:
        return override
    if settings.speaker_channel and settings.speaker_channel != "unknown":
        return settings.speaker_channel
    info = probe_audio_info(Path(audio).expanduser(), settings.ffprobe_bin)
    return channel_for_audio(info.get("sample_rate"), info.get("bitrate_kbps"))


def _read_assignments(folder: Path) -> dict:
    path = folder / "speakers.json"
    if not path.is_file():
        raise PipelineError(f"目录里没有 speakers.json（先跑一次转写）：{folder}")
    return json.loads(path.read_text(encoding="utf-8"))


def _rewrite_markdown(folder: Path, mapping: dict[int, str]) -> bool:
    """把 transcript.md 里的 `Speaker N` 换成名字（实现已挪到 speakers.py 复用）。"""
    from speakers import rewrite_transcript_labels

    return rewrite_transcript_labels(folder, mapping)


def cmd_list(args, settings) -> int:
    store = _store(settings)
    rows = store.summary()
    print(f"声纹库：{store.path}")
    if not rows:
        print("  （空）登记方式：tools/speaker_cli.py enroll --name 张三 --audio a.m4a")
        return 0
    print(f"  共 {len(rows)} 人")
    for row in rows:
        channels = ",".join(row["channels"]) or "-"
        profiles = ",".join(row.get("profiles") or ["默认"])
        print(f"  {row['name']:<12} {row['templates']} 条模板  [信道 {channels}]  [场景 {profiles}]"
              f"  最近 {row['last'] or '-'}")
    return 0


def cmd_show(args, settings) -> int:
    folder = Path(args.dir).expanduser().resolve()
    data = _read_assignments(folder)
    print(f"目录：{folder}")
    print(f"信道：{data.get('channel', 'unknown')}（阈值 自动 {CHANNEL_THRESHOLDS.get(data.get('channel', 'unknown'), CHANNEL_THRESHOLDS['unknown'])['auto']:.2f}）")
    items = data.get("assignments") or []
    if not items:
        print("  （本次没有声纹识别结果）")
        return 0
    for item in items:
        label = f"Speaker {item['cluster'] + 1}"
        name = item.get("name")
        status = item.get("status", "unknown")
        head = f"  {label:<10} → {name}（{status} {item.get('score', 0):.2f}）" if name \
            else f"  {label:<10} → 未知（最高 {item.get('score', 0):.2f}）"
        print(head)
        for cand in item.get("candidates") or []:
            mark = "★" if cand["score"] >= 0.6 else " "
            print(f"       {mark} 候选 {cand['name']:<10} {cand['score']:.3f}"
                  f"  [{cand.get('channel', '-')} / {cand.get('profile', '默认')}]")
        print(f"       时长 {item.get('duration', 0):.1f}s / {item.get('windows', 0)} 个声纹窗口")
    print("\n确认命令：python tools/speaker_cli.py label --dir <此目录> --cluster <N> --name <人名>")
    return 0


def cmd_label(args, settings) -> int:
    folder = Path(args.dir).expanduser().resolve()
    data = _read_assignments(folder)
    target = next((a for a in data.get("assignments", []) if int(a["cluster"]) == args.cluster), None)
    if target is None:
        print(f"没找到 cluster={args.cluster}，可选：{[a['cluster'] for a in data.get('assignments', [])]}")
        return 2
    emb = target.get("embedding")
    if not emb:
        print("该簇没有 embedding，无法入库")
        return 2

    store = _store(settings)
    result = store.add(
        args.name,
        emb,
        channel=args.channel or data.get("channel", "unknown"),
        source=str(folder),
        duration=float(target.get("duration") or 0),
        allow_short=args.force,
        profile=getattr(args, "profile", None),
    )
    print(f"{'✅ 已登记' if result['added'] else 'ℹ️  已更新'}：{args.name}"
          f"［{result.get('profile', '默认')}］（{result['reason']}，共 {result['templates']} 条模板）")

    # 回写本次识别结果 + 改写转写稿
    target["name"] = args.name
    target["profile"] = result.get("profile", "默认")
    target["status"] = "confirmed"
    target["score"] = 1.0
    (folder / "speakers.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    mapping = {int(a["cluster"]): a["name"] for a in data.get("assignments", []) if a.get("name")}
    if args.rewrite and _rewrite_markdown(folder, mapping):
        print("✅ transcript.md 说话人标签已更新")
    return 0


def cmd_enroll(args, settings) -> int:
    emb, dur = _embed_audio(Path(args.audio), settings, max_sec=args.max_sec)
    store = _store(settings)
    channel = _guess_channel(Path(args.audio), settings, args.channel)
    result = store.add(
        args.name, emb, channel=channel,
        source=str(Path(args.audio).expanduser()),
        duration=dur, allow_short=args.force,
        profile=getattr(args, "profile", None),
    )
    print(f"{'✅ 已登记' if result['added'] else 'ℹ️  已更新'}：{args.name}"
          f"［{result.get('profile', '默认')}］（{result['reason']}；样本 {fmt_hms(dur)} / 信道 {channel}；"
          f"共 {result['templates']} 条模板）")
    return 0


def cmd_who(args, settings) -> int:
    emb, dur = _embed_audio(Path(args.audio), settings, max_sec=args.max_sec)
    store = _store(settings)
    channel = _guess_channel(Path(args.audio), settings, args.channel)
    guess = store.identify(0, emb, channel=channel, duration=dur)
    print(f"样本：{fmt_hms(dur)}｜信道 {channel}｜阈值 {CHANNEL_THRESHOLDS[channel]['auto']:.2f}")
    print(f"结论：{guess.name or '未知'}（{guess.status}，最高相似 {guess.score:.3f}）")
    print(f"     场景 {guess.profile}｜与第二名差 {guess.margin:.2f}（自动命名要求 ≥{AUTO_MARGIN:.2f}）")
    for cand in guess.candidates:
        print(f"  {cand.name:<12} {cand.score:.3f}  [{cand.channel} / {cand.profile}]")
    return 0


def cmd_rename(args, settings) -> int:
    store = _store(settings)
    if not store.rename(args.old, args.new):
        print(f"失败：没有 {args.old}，或新名字为空")
        return 2
    print(f"✅ {args.old} → {args.new}")
    return 0


def cmd_forget(args, settings) -> int:
    store = _store(settings)
    if args.index is not None:
        ok = store.remove_template(args.name, args.index)
        print("✅ 已删除该条模板" if ok else "失败：索引不存在")
        return 0 if ok else 2
    ok = store.remove_person(args.name)
    print("✅ 已删除整个人的声纹" if ok else "失败：没有这个人")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="声纹库管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出库里的人")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="查看某次转写的识别结果")
    p.add_argument("--dir", required=True, help="归档目录（含 speakers.json）")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("label", help="确认某个簇是谁，并入声纹库")
    p.add_argument("--dir", required=True)
    p.add_argument("--cluster", type=int, required=True, help="从 0 开始，对应 Speaker N 的 N-1")
    p.add_argument("--name", required=True)
    p.add_argument("--channel", choices=list(CHANNEL_THRESHOLDS), help="覆盖信道判定")
    p.add_argument("--profile", default="默认", help="场景/语域：默认、日常、开会、方言…")
    p.add_argument("--rewrite", action="store_true", default=True, help="同步改写 transcript.md（默认开）")
    p.add_argument("--no-rewrite", dest="rewrite", action="store_false")
    p.add_argument("--force", action="store_true", help="样本过短也允许入库")
    p.set_defaults(func=cmd_label)

    p = sub.add_parser("enroll", help="用一段单人录音直接入库")
    p.add_argument("--name", required=True)
    p.add_argument("--audio", required=True)
    p.add_argument("--channel", choices=list(CHANNEL_THRESHOLDS))
    p.add_argument("--profile", default="默认", help="场景/语域：默认、日常、开会、方言…")
    p.add_argument("--max-sec", type=float, default=60.0, help="最多取前 N 秒（默认 60）")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_enroll)

    p = sub.add_parser("who", help="这段录音像谁")
    p.add_argument("--audio", required=True)
    p.add_argument("--channel", choices=list(CHANNEL_THRESHOLDS))
    p.add_argument("--max-sec", type=float, default=60.0)
    p.set_defaults(func=cmd_who)

    p = sub.add_parser("rename", help="改名（同名会合并）")
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("forget", help="删除某人的声纹")
    p.add_argument("--name", required=True)
    p.add_argument("--index", type=int, help="只删第 N 条模板")
    p.set_defaults(func=cmd_forget)

    args = parser.parse_args(argv)
    settings = cli_settings()
    ensure_dirs(settings)
    setup_logging(settings.logs_dir, settings.log_level, "speaker")
    try:
        return args.func(args, settings)
    except PipelineError as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
