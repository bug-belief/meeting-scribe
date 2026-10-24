"""按归档里的音频重新生成一份转写稿 / 纪要（不新建目录）。

用途：修正逻辑升级后，把老归档「重跑一遍」——最典型的是助理台词纠字
（2026-10-19 之前只换标签、不换文本，ASR 会把「11到17度」写成「一七度」）。

会做的事：
1. 读 meta.json（音频名、是否双路、助理播报时间线、归属人）
2. 用当前版本重新跑 transcribe（含说话人 / 声纹 / 助理时间线）
3. 重新生成纪要
4. 覆盖 archive 里的 transcript.md / transcript.json / report.md，保留原音频和 speakers.json
5. 重新盖上 meeting_id（如果 meta 里还没有）

用法：
    python deploy/refresh_archive.py --dir ~/data/tenants/me/processed/2026-01-01_会议_0900
    python deploy/refresh_archive.py --dir <archive> --no-report
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import SETTINGS, ensure_dirs, for_owner  # noqa: E402
from pipeline import _stamp_meeting_id  # noqa: E402
from summarize import summarize_transcript  # noqa: E402
from transcribe import transcribe_file  # noqa: E402
from utils import read_json, setup_logging  # noqa: E402


def owner_of(folder: Path) -> str:
    parts = folder.resolve().parts
    if "tenants" in parts:
        i = parts.index("tenants")
        if i + 1 < len(parts):
            return parts[i + 1]
    return SETTINGS.default_owner


def ensure_meeting_id(meta: dict, meta_path: Path, folder: Path, audio: Path, owner: str) -> str:
    """元信息里有就用，没有就按归档时间补一个（老归档）。"""
    mid = str(meta.get("meeting_id") or "")
    if mid:
        return mid
    from datetime import datetime

    from utils import write_json

    when = datetime.fromtimestamp(folder.stat().st_mtime)
    mid = f"M{when:%Y%m%d-%H%M%S}-{str(meta.get('sha256') or '')[:4]}"
    meta["meeting_id"] = mid
    meta.setdefault("owner", owner)
    meta.setdefault("title", audio.stem)
    write_json(meta_path, meta)
    return mid


def main() -> int:
    ap = argparse.ArgumentParser(description="重跑某个归档的转写 / 纪要")
    ap.add_argument("--dir", required=True, help="归档目录")
    ap.add_argument("--no-report", action="store_true", help="只重跑转写，不重新生成纪要")
    args = ap.parse_args()

    folder = Path(args.dir).expanduser().resolve()
    meta_path = folder / "meta.json"
    if not meta_path.is_file():
        print(f"❌ 不是归档目录（没有 meta.json）：{folder}")
        return 1

    meta = read_json(meta_path, {})
    audio = folder / Path(str(meta.get("audio") or "")).name
    if not audio.is_file():
        hits = sorted(folder.glob("*.wav")) + sorted(folder.glob("*.m4a"))
        if not hits:
            print("❌ 归档里找不到音频")
            return 1
        audio = hits[0]

    owner = owner_of(folder)
    settings = for_owner(owner, SETTINGS)
    ensure_dirs(settings)
    setup_logging(settings.logs_dir, settings.log_level, "refresh")

    events = meta.get("assistant_events") or []
    dual = bool(meta.get("dual"))
    # meeting_id 先定下来：成本块要以它为锚，不能等收尾才算
    mid = ensure_meeting_id(meta, meta_path, folder, audio, owner)
    print(f"重跑：{folder.name}（owner={owner} / dual={dual} / 播报 {len(events)} 条 / {mid}）")

    settings.work_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="refresh-", dir=str(settings.work_dir)))
    try:
        transcribe_file(audio, tmp, settings, dual=dual, events=events)
        for name in ("transcript.md", "transcript.json"):
            src = tmp / name
            if src.is_file():
                shutil.copy2(src, folder / name)
                print(f"  已更新 {name}")
        if not args.no_report:
            out_report = tmp / "report.md"
            summarize_transcript(tmp / "transcript.md", out_report, settings)
            if out_report.is_file():
                shutil.copy2(out_report, folder / "report.md")
                print("  已更新 report.md")
        # 成本：把 tmp 期间的记账改挂到 meeting_id，再写进纪要末尾
        from costs import append_to_report, relabel

        moved = relabel(tmp.name, mid)
        append_to_report(folder / "report.md", mid)
        if moved:
            print(f"  成本记录改挂 {moved} 条 → {mid}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    _stamp_meeting_id(folder, mid)
    print(f"完成，meeting_id={mid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
