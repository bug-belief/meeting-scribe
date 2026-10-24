"""音色语料池：把每次录音里「确定是你」的干净片段攒起来，用来重建参考音。

为什么是「攒语料」而不是「训练」
--------------------------------
CosyVoice2 是 zero-shot 克隆：**它不训练你的声音**，每次合成都是拿一段 3~30 秒的
参考音（prompt.wav + prompt.txt）现场提取音色。所以「越用越像」的真实含义是：

    参考音越干净、越覆盖你说话的常态 → 合成越像

而不是模型被训练得越来越像。这里做的就是把每次录音里的好片段攒起来，
再从池子里挑最合适的一段拼成新的参考音，替换掉旧的那条。

一个必须守住的点
----------------
**参考音的文本必须和音频逐字对应。** CosyVoice 靠它对齐音素，文本错了音色就漂。
所以这里只做「整段搬运」——不裁剪、不改写 ASR 文本，宁可少攒也不拼错。

目录（按租户隔离）
------------------
    data/tenants/<owner>/voice_corpus/
        <场景>/                      默认 / 开会 / 方言
            index.json               每段的元数据
            <设备>_<归档>_<序号>.wav   16k 单声道片段（<=9 秒）

用法
----
    python voice_corpus.py collect                 # 扫全部归档，收进池子
    python voice_corpus.py list                    # 看池子里有什么
    python voice_corpus.py build --name 张三      # 生成候选参考音（不替换）
    python voice_corpus.py build --name 张三 --install   # 替换掉现在的音色
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from config import Settings, cli_settings, ensure_dirs

logger = logging.getLogger("voice_corpus")

ASSISTANT_SPEAKER = 9001
MIN_SEG_SEC = 2.0      # 太短的片段音素覆盖不够
MAX_SEG_SEC = 9.0      # 太长的片段情绪起伏大，克隆反而不稳
TARGET_SEC = 12.0      # 重建参考音的目标总长
GAP_SEC = 0.25         # 片段之间的静音，避免连读串音


# ---------------------------------------------------------------- 基础工具

def _slug(text: str, fallback: str = "unknown") -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(text or "").strip())
    return (s.strip("_")[:40] or fallback)


def _corpus_root(settings: Settings) -> Path:
    return settings.tenant_root / settings.owner / "voice_corpus"


def _index_path(settings: Settings, profile: str) -> Path:
    return _corpus_root(settings) / _slug(profile, "默认") / "index.json"


def _load_index(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": []}


def _save_index(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _own_name(settings: Settings) -> str:
    """默认取声纹库里第一个人名（通常就是本人）。"""
    try:
        from speakers import SpeakerStore

        store = SpeakerStore(settings.speaker_store, settings.speaker_max_templates)
        names = sorted(store.people)
        if names:
            return names[0]
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 挑片段

def _speech_rms(data: np.ndarray, sr: int, start: float, end: float) -> float:
    a = max(0, int(start * sr))
    b = min(len(data), int(end * sr))
    if b - a < int(0.1 * sr):
        return 0.0
    block = data[a:b]
    return float(np.sqrt(np.mean(block ** 2)))


def _overlap(a1: float, a2: float, b1: float, b2: float) -> float:
    return max(0.0, min(a2, b2) - max(a1, b1))


_ENGINE = None


def _engine(settings: Settings):
    """声纹模型复用一份，collect 要逐段核验，不能每段都加载一遍。"""
    global _ENGINE
    if _ENGINE is None:
        from diarize import FunASREngine

        _ENGINE = FunASREngine(settings)
    return _ENGINE


def _speaker_ok(settings: Settings, name: str, clip: np.ndarray) -> tuple[bool, float]:
    """声纹复核：标签说是本人，还得声音真的是本人。

    实测尺度：同人跨语域最低约 0.30，异人 0.11。取 0.28，既能挡住
    「聚类把对方并进我这一簇」的误标，又不会把念稿/方言这些场景挡掉。
    """
    try:
        from speakers import SpeakerStore

        store = SpeakerStore(settings.speaker_store, settings.speaker_max_templates)
        if name not in store.people:
            return True, 0.0
        emb = _engine(settings).embed(np.ascontiguousarray(clip, dtype="float32"))
        rows = [c for c in store.scores(emb) if c.name == name]
        if not rows:
            return True, 0.0
        score = float(rows[0].score)
        return score >= 0.28, score
    except Exception as exc:
        logger.debug("声纹复核跳过：%s", exc)
        return True, 0.0


def _candidates(folder: Path, name: str, settings: Settings) -> list[dict]:
    """从一份归档里挑出「确定是本人、干净、够长」的段。"""
    tpath = folder / "transcript.json"
    if not tpath.is_file():
        return []
    try:
        data = json.loads(tpath.read_text(encoding="utf-8"))
    except Exception:
        return []

    names = {str(k): str(v) for k, v in (data.get("speaker_names") or {}).items()}
    events = data.get("assistant_events") or []
    segs = data.get("segments") or []
    if not segs:
        return []

    wav = folder / (data.get("source") or "")
    if not wav.is_file():
        hits = sorted(folder.glob("*.wav"))
        if not hits:
            return []
        wav = hits[0]

    try:
        audio, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    except Exception as exc:
        logger.warning("读不了 %s：%s", wav.name, exc)
        return []

    # 双声道：左声道是本人的麦克风。单声道直接用。
    mono = audio[:, 0] if audio.shape[1] > 1 else audio[:, 0]
    dual = audio.shape[1] > 1

    rms_all = [_speech_rms(mono, sr, s["start"], s["end"]) for s in segs]
    positives = [r for r in rms_all if r > 1e-5]
    if not positives:
        return []
    loud_ref = float(np.percentile(positives, 90))

    meta_path = folder / "meta.json"
    device = ""
    if meta_path.is_file():
        try:
            device = str(json.loads(meta_path.read_text(encoding="utf-8")).get("mic") or "")
        except Exception:
            device = ""

    out = []
    for seg, rms in zip(segs, rms_all):
        spk = int(seg.get("speaker", -1))
        if spk == ASSISTANT_SPEAKER:
            continue
        if names.get(str(spk)) != name:
            continue
        dur = float(seg.get("end", 0)) - float(seg.get("start", 0))
        if not (MIN_SEG_SEC <= dur <= MAX_SEG_SEC):
            continue
        text = str(seg.get("text") or "").strip()
        if len(text) < 4:
            continue
        # 助理正在说话的时间段不要（会混进克隆音）
        if any(_overlap(seg["start"], seg["end"], float(e.get("start") or 0),
                        float(e.get("end") or 0)) >= 0.15 * dur for e in events):
            continue
        if rms < loud_ref * 0.25:
            continue
        a = max(0, int(seg["start"] * sr))
        b = min(len(mono), int(seg["end"] * sr))
        ok, score = _speaker_ok(settings, name, mono[a:b])
        if not ok:
            logger.info("跳过 %s@%.1fs：声纹 %.2f 不像本人", folder.name, seg["start"], score)
            continue
        out.append({
            "archive": folder.name,
            "sha256": str(data.get("sha256") or folder.name),
            "speaker_score": round(score, 3),
            "audio": str(wav),
            "start": round(float(seg["start"]), 2),
            "end": round(float(seg["end"]), 2),
            "duration": round(dur, 2),
            "rms": round(rms, 5),
            "text": text,
            "device": device,
            "dual": dual,
            "channel": 0 if dual else None,
        })
    return out


def _cut(audio: Path, start: float, duration: float, out: Path,
         channel: int | None, ffmpeg_bin: str = "ffmpeg") -> bool:
    af = f"pan=mono|c0=c{channel}" if channel is not None else "pan=mono|c0=c0"
    cmd = [
        ffmpeg_bin, "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(audio), "-af", af, "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le", str(out),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return out.is_file() and out.stat().st_size > 2000
    except Exception as exc:
        logger.warning("切片失败 %s@%.1fs：%s", audio.name, start, exc)
        return False


# ---------------------------------------------------------------- 命令

def collect(settings: Settings, name: str = "", profile: str = "默认",
            limit: int = 0, only: str = "") -> dict:
    """扫 processed 下的归档，把合格片段收进语料池。可重复执行（按来源去重）。

    `only`：只看归档名里包含该字符串的目录（例如 `--only 1426` 只收那一段方言录音）。
    """
    ensure_dirs(settings)
    name = (name or "").strip() or _own_name(settings)
    if not name:
        return {"ok": False, "error": "声纹库里还没有人，先认领一次再来"}

    idx_path = _index_path(settings, profile)
    index = _load_index(idx_path)
    seen = {(i.get("sha256") or i["archive"], round(float(i["start"]), 2)) for i in index["items"]}

    root = _corpus_root(settings) / _slug(profile, "默认")
    root.mkdir(parents=True, exist_ok=True)

    folders = sorted([p for p in settings.processed_dir.iterdir() if p.is_dir()],
                     key=lambda p: p.name, reverse=True)
    if only:
        folders = [p for p in folders if only in p.name]
    if limit:
        folders = folders[:limit]

    added, skipped = [], 0
    for folder in folders:
        if folder.name.startswith("_"):
            continue
        for cand in _candidates(folder, name, settings):
            key = (cand["sha256"], round(cand["start"], 2))
            if key in seen:
                skipped += 1
                continue
            dev = _slug(cand["device"], "未知设备")
            out = root / f"{dev}_{cand['archive']}_{int(cand['start'] * 100):06d}.wav"
            if not _cut(Path(cand["audio"]), cand["start"], cand["duration"], out, cand["channel"]):
                continue
            item = {
                "file": out.name,
                "sha256": cand.get("sha256", ""),
                "speaker_score": cand.get("speaker_score", 0.0),
                "profile": profile,
                "device": cand["device"] or "未知设备",
                "archive": cand["archive"],
                "start": cand["start"],
                "end": cand["end"],
                "duration": cand["duration"],
                "rms": cand["rms"],
                "text": cand["text"],
                "added": datetime.now().isoformat(timespec="seconds"),
            }
            index["items"].append(item)
            seen.add(key)
            added.append(item)

    _save_index(idx_path, index)
    logger.info("语料池 %s：新增 %d 段，跳过 %d 段，共 %d 段",
                profile, len(added), skipped, len(index["items"]))
    return {"ok": True, "name": name, "profile": profile,
            "added": len(added), "skipped": skipped, "total": len(index["items"])}


def build(settings: Settings, name: str, profile: str = "默认",
          voices_dir: Path | None = None, install: bool = False,
          device: str = "", target: float = TARGET_SEC,
          concat: bool = False) -> dict:
    """从语料池挑参考音。默认只产出候选，不动现有音色。

    为什么默认「只用一段」
    ----------------------
    2026-10-19 实测：把 3 段共 15.66 秒拼起来，反而不如一条 3.1 秒的自然录音像。
    原因是拼接口的停顿、语速、音量都不连续，CosyVoice 会把这种「抖」也学进去。
    参考音不是越长越好，**一段连贯、干净、语速正常的话**最稳。

    所以默认策略是「单段优先」：只在池子里挑一条最合适的整段，不剪不拼。
    池子里确实没有 >=3 秒的段时，才退回拼接（也可以用 --concat 强制拼接）。
    """
    idx_path = _index_path(settings, profile)
    index = _load_index(idx_path)
    items = [i for i in index["items"] if not device or device in str(i.get("device") or "")]
    if not items:
        return {"ok": False, "error": f"语料池是空的（场景 {profile}），先跑 collect"}

    # 单段打分：4.5~9 秒最理想（音素够、情绪稳），3 秒以上可用，越响通常越干净。
    def _single_rank(i: dict):
        d = float(i.get("duration") or 0)
        fit = 1.0 if 4.5 <= d <= MAX_SEG_SEC else (0.8 if d >= 3.0 else 0.5)
        return (fit, min(d, MAX_SEG_SEC), float(i.get("rms") or 0))

    chosen, total = [], 0.0
    strategy = "single"
    if not concat:
        best = max(items, key=_single_rank)
        if float(best.get("duration") or 0) >= 3.0:
            chosen = [best]
            total = float(best.get("duration") or 0)
        else:
            strategy = "concat"     # 没有像样的单段，只能拼

    if not chosen:
        strategy = "concat"
        # 拼接模式：中长段优先，同一次录音最多取 2 句，避免参考音里全是同一个语境
        def _rank(i: dict):
            d = float(i.get("duration") or 0)
            return (min(d, MAX_SEG_SEC), float(i.get("rms") or 0))

        ranked = sorted(items, key=_rank, reverse=True)
        per_archive: dict[str, int] = {}
        for it in ranked:
            src = it.get("sha256") or it.get("archive") or ""
            if per_archive.get(src, 0) >= 2:
                continue
            d = float(it.get("duration") or 0)
            if total + d > target * 1.35:
                continue
            chosen.append(it)
            per_archive[src] = per_archive.get(src, 0) + 1
            total += d
            if total >= target:
                break
    if not chosen:
        return {"ok": False, "error": "没有可用的片段"}

    chunks, texts = [], []
    root = idx_path.parent
    for it in chosen:
        path = root / it["file"]
        if not path.is_file():
            continue
        try:
            pcm, sr = sf.read(str(path), dtype="float32")
        except Exception:
            continue
        if sr != 16000:
            continue
        chunks.append(pcm)
        if strategy == "concat":
            chunks.append(np.zeros(int(GAP_SEC * 16000), dtype="float32"))
        texts.append(str(it["text"]).strip())

    if not chunks:
        return {"ok": False, "error": "片段都读不出来"}

    audio = np.concatenate(chunks).astype(np.float32)
    peak = float(np.max(np.abs(audio))) or 1.0
    audio = audio / peak * 0.70          # 留 3dB 余量，避免削波

    # 文本必须和音频一一对应：整段搬运，只在句间补标点
    parts = []
    for t in texts:
        t = t.strip()
        if not t:
            continue
        parts.append(t if t[-1] in "。！？，、；：.!?," else t + "，")
    prompt_text = "".join(parts).rstrip("，,、。.!？?") + "。"

    voices = voices_dir or (Path.home() / "tts" / "voices")
    folder = voices / name / profile
    folder.mkdir(parents=True, exist_ok=True)
    suffix = "" if install else ".candidate"
    wav_out = folder / f"prompt{suffix}.wav"
    txt_out = folder / f"prompt{suffix}.txt"
    sf.write(str(wav_out), audio, 16000, subtype="PCM_16")
    txt_out.write_text(prompt_text, encoding="utf-8")

    backup = ""
    if install:
        old_wav = folder / "prompt.wav"
        if old_wav.is_file():
            bak = folder / f"prompt.bak-{datetime.now():%Y%m%d-%H%M%S}.wav"
            shutil.copy2(old_wav, bak)
            backup = bak.name
        meta = folder / "meta.json"
        # owner 不归这个脚本管：它只重建参考音，不该顺手改归属。
        # 但它是整个覆写 meta.json 的，不显式带过来，重建一次音色就变成
        # 「owner 为空 = 谁都能用」——所以沿用旧值，没有才留空。
        prev: dict = {}
        if meta.is_file():
            try:
                prev = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                prev = {}
        payload = {
            "name": name,
            "speaker": name,
            "profile": profile,
            "owner": str(prev.get("owner") or ""),
            "built_from": {
                "strategy": strategy,
                "items": len(chosen),
                "seconds": round(len(audio) / 16000, 2),
                "devices": sorted({str(i.get("device") or "") for i in chosen}),
                "archives": sorted({str(i.get("archive") or "") for i in chosen}),
                "at": datetime.now().isoformat(timespec="seconds"),
            },
        }
        meta.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    return {
        "ok": True,
        "voice": name,
        "profile": profile,
        "installed": bool(install),
        "backup": backup,
        "segments": len(chosen),
        "strategy": strategy,
        "seconds": round(len(audio) / 16000, 2),
        "wav": str(wav_out),
        "prompt": prompt_text,
        "sources": [
            {"archive": i["archive"], "start": i["start"], "dur": i["duration"],
             "device": i.get("device") or "", "text": i["text"][:30]}
            for i in chosen
        ],
    }


def add_file(settings: Settings, name: str, profile: str, audio: Path,
             text: str, device: str = "手动导入") -> dict:
    """手动把一段「音频 + 逐字稿」加进语料池（用于长段落录音没有 2~9s 小段的情况）。

    文本必须和音频逐字对应；调用方负责保证（voice_register 自动转写的那份就满足）。
    """
    audio = Path(audio).expanduser()
    if not audio.is_file():
        return {"ok": False, "error": f"找不到音频：{audio}"}
    text = str(text or "").strip()
    if len(text) < 4:
        return {"ok": False, "error": "逐字稿太短，参考音文本必须和音频对应"}

    idx_path = _index_path(settings, profile)
    index = _load_index(idx_path)
    root = idx_path.parent
    root.mkdir(parents=True, exist_ok=True)

    dur = _duration(audio)
    if dur < 2.0:
        return {"ok": False, "error": f"音频只有 {dur:.1f}s，太短"}

    out = root / f"{_slug(device, '手动')}_{_slug(audio.stem, 'clip')}.wav"
    if not _cut(audio, 0.0, min(dur, MAX_SEG_SEC * 2), out, None):
        return {"ok": False, "error": "转码失败"}

    item = {
        "file": out.name,
        "sha256": hashlib.sha256(str(audio.resolve()).encode()).hexdigest()[:16],
        "speaker_score": 1.0,
        "profile": profile,
        "device": device,
        "archive": "手动导入",
        "start": 0.0,
        "end": round(min(dur, MAX_SEG_SEC * 2), 2),
        "duration": round(min(dur, MAX_SEG_SEC * 2), 2),
        "rms": round(_speech_rms_from_file(out), 5),
        "text": text,
        "added": datetime.now().isoformat(timespec="seconds"),
    }
    index["items"].append(item)
    _save_index(idx_path, index)
    logger.info("语料池 %s：手动加入 1 段 %.1fs", profile, item["duration"])
    return {"ok": True, "name": name, "profile": profile,
            "added": 1, "total": len(index["items"]), "item": item}


def _duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out or 0)
    except Exception:
        return 0.0


def _speech_rms_from_file(path: Path) -> float:
    try:
        pcm, sr = sf.read(str(path), dtype="float32")
        return float(np.sqrt(np.mean(pcm ** 2))) if len(pcm) else 0.0
    except Exception:
        return 0.0


def list_corpus(settings: Settings, profile: str = "") -> dict:
    root = _corpus_root(settings)
    out = {}
    if not root.is_dir():
        return out
    for prof_dir in sorted(root.iterdir()):
        if not prof_dir.is_dir():
            continue
        if profile and prof_dir.name != _slug(profile, "默认"):
            continue
        index = _load_index(prof_dir / "index.json")
        items = index.get("items") or []
        devices: dict[str, float] = {}
        for i in items:
            d = str(i.get("device") or "未知设备")
            devices[d] = devices.get(d, 0.0) + float(i.get("duration") or 0)
        out[prof_dir.name] = {
            "segments": len(items),
            "seconds": round(sum(float(i.get("duration") or 0) for i in items), 1),
            "devices": {k: round(v, 1) for k, v in sorted(devices.items())},
        }
    return out


def collect_from_archive(folder: Path, settings: Settings, name: str = "",
                         profile: str = "默认") -> dict:
    """给 pipeline 用的单归档版本：失败不影响主流程。"""
    try:
        if not name:
            name = _own_name(settings)
        if not name:
            return {"ok": False, "error": "no-name"}

        idx_path = _index_path(settings, profile)
        index = _load_index(idx_path)
        seen = {(i.get("sha256") or i["archive"], round(float(i["start"]), 2)) for i in index["items"]}
        root = idx_path.parent
        root.mkdir(parents=True, exist_ok=True)

        n = 0
        for cand in _candidates(folder, name, settings):
            key = (cand["sha256"], round(cand["start"], 2))
            if key in seen:
                continue
            dev = _slug(cand["device"], "未知设备")
            out = root / f"{dev}_{cand['archive']}_{int(cand['start'] * 100):06d}.wav"
            if not _cut(Path(cand["audio"]), cand["start"], cand["duration"], out, cand["channel"]):
                continue
            index["items"].append({
                "file": out.name, "sha256": cand.get("sha256", ""),
                "speaker_score": cand.get("speaker_score", 0.0), "profile": profile,
                "device": cand["device"] or "未知设备", "archive": cand["archive"],
                "start": cand["start"], "end": cand["end"], "duration": cand["duration"],
                "rms": cand["rms"], "text": cand["text"],
                "added": datetime.now().isoformat(timespec="seconds"),
            })
            n += 1
        if n:
            _save_index(idx_path, index)
        return {"ok": True, "added": n, "total": len(index["items"])}
    except Exception as exc:
        logger.warning("音色语料收集失败（不影响转写）：%s", exc)
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="音色语料池：攒干净片段，重建参考音")
    # --name / --profile 放在子命令前后都能用（argparse 默认只认子命令前的位置）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", default="默认", help="场景：默认 / 开会 / 方言")
    common.add_argument("--name", default="", help="说话人名（默认取声纹库第一个人）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_col = sub.add_parser("collect", parents=[common], help="扫描归档，收进语料池")
    p_col.add_argument("--limit", type=int, default=0, help="只扫最近 N 个归档")
    p_col.add_argument("--only", default="", help="只扫名字包含该字串的归档（如 1426）")

    sub.add_parser("list", parents=[common], help="看语料池现状")

    p_add = sub.add_parser("add", parents=[common], help="手动加入一段（音频 + 逐字稿）")
    p_add.add_argument("--audio", required=True, help="音频文件（wav/mp3/m4a）")
    p_add.add_argument("--text", required=True, help="和音频逐字对应的文本")

    p_build = sub.add_parser("build", parents=[common], help="拼一条参考音")
    p_build.add_argument("--install", action="store_true", help="替换现有音色（会备份旧版）")
    p_build.add_argument("--device", default="", help="只用某个设备的片段")
    p_build.add_argument("--target", type=float, default=TARGET_SEC, help="目标总秒数（仅拼接模式）")
    p_build.add_argument("--concat", action="store_true",
                         help="强制拼接多段（默认单段优先；拼接容易把接口停顿也学进去）")
    p_build.add_argument("--voices", default="", help="音色根目录（默认 ~/tts/voices）")

    args = ap.parse_args(argv)
    settings = cli_settings()

    if args.cmd == "collect":
        print(json.dumps(collect(settings, args.name, args.profile, args.limit, args.only),
                         ensure_ascii=False, indent=1))
    elif args.cmd == "add":
        name = args.name or _own_name(settings)
        print(json.dumps(add_file(settings, name, args.profile, Path(args.audio), args.text),
                         ensure_ascii=False, indent=1))
    elif args.cmd == "list":
        print(json.dumps(list_corpus(settings, "" if args.profile == "默认" else args.profile),
                         ensure_ascii=False, indent=1))
    else:
        name = args.name or _own_name(settings)
        if not name:
            print("❌ 声纹库里还没有人，先认领一次")
            return 1
        voices = Path(args.voices).expanduser() if args.voices else None
        r = build(settings, name, args.profile, voices, args.install,
                  args.device, args.target, args.concat)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        if r.get("ok") and not r.get("installed"):
            print("\nℹ️  这只是候选，没有替换现用音色。满意就加 --install。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
