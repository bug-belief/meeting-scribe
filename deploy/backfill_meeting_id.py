"""给已有归档补 meeting_id（一次性迁移脚本，可重复执行）。

新归档在 pipeline.archive 里自动分配 ID；这个脚本只处理「加 ID 之前」的老目录。
ID 规则与 pipeline 一致：M + 归档日期 + 归档时间 + 音频 sha256 前 4 位。
已有 ID 的目录原样跳过。

用法：
    python deploy/backfill_meeting_id.py --dry-run
    python deploy/backfill_meeting_id.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import _stamp_meeting_id  # noqa: E402
from utils import read_json, write_json  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="给已有归档补 meeting_id")
    ap.add_argument("--root", default=str(Path.home() / "data" / "tenants"),
                    help="租户根目录（默认 ~/data/tenants）")
    ap.add_argument("--dry-run", action="store_true", help="只看会改哪些，不落盘")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"❌ 找不到目录：{root}")
        return 1

    done = skipped = 0
    for tenant in sorted(p for p in root.iterdir() if p.is_dir()):
        processed = tenant / "processed"
        if not processed.is_dir():
            continue
        for folder in sorted(p for p in processed.iterdir()
                             if p.is_dir() and not p.name.startswith("_")):
            meta_path = folder / "meta.json"
            if not meta_path.is_file():
                continue
            meta = read_json(meta_path, {})
            if meta.get("meeting_id"):
                skipped += 1
                continue

            stamp = str(meta.get("archived_at") or "")
            try:
                when = datetime.fromisoformat(stamp)
            except Exception:
                when = datetime.fromtimestamp(folder.stat().st_mtime)
            digest = str(meta.get("sha256") or "")
            mid = f"M{when:%Y%m%d-%H%M%S}-{(digest[:4] or '0000')}"
            print(f"{folder.name:40s} → {mid}")
            if not args.dry_run:
                meta["meeting_id"] = mid
                meta.setdefault("owner", tenant.name)
                meta.setdefault("title", Path(str(meta.get("audio") or folder.name)).stem)
                write_json(meta_path, meta)
                _stamp_meeting_id(folder, mid)
            done += 1

    print(f"\n{'将补' if args.dry_run else '已补'} {done} 个，跳过 {skipped} 个（已有 ID）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
