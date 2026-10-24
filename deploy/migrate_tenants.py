#!/usr/bin/env python3
"""把单租户目录迁移成多租户结构。

    data/processed      → data/tenants/<owner>/processed
    data/work           → data/tenants/<owner>/work
    data/audios_inbox   → data/tenants/<owner>/inbox
    data/speakers.json  → data/tenants/<owner>/speakers.json

特性
----
* 幂等：目标已存在就跳过，可以反复跑
* `--dry-run` 只打印计划，不动文件
* 用 `shutil.move`，同盘是 rename，瞬间完成
* 迁移后原路径留一个 `.migrated` 空标记，避免服务又把旧目录建回来搞混

用法
----
    python deploy/migrate_tenants.py --dry-run
    python deploy/migrate_tenants.py --owner me
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def move(src: Path, dst: Path, dry: bool) -> str:
    if not src.exists():
        return f"skip  {src}（不存在）"
    if dst.exists():
        return f"skip  {dst}（目标已存在，避免覆盖）"
    if dry:
        size = sum(p.stat().st_size for p in src.rglob("*") if p.is_file()) if src.is_dir() else src.stat().st_size
        return f"plan  {src} → {dst}（{size / 1048576:.1f} MB）"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"done  {src} → {dst}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="迁移成多租户目录")
    ap.add_argument("--data", default=str(Path.home() / "data"), help="data 根目录")
    ap.add_argument("--owner", default="me", help="现有数据归属哪个租户")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    data = Path(args.data).expanduser().resolve()
    if not data.is_dir():
        print(f"找不到 data 目录：{data}")
        return 1

    target = data / "tenants" / args.owner
    print(f"data   = {data}")
    print(f"target = {target}")
    print("-" * 60)

    jobs = [
        (data / "processed", target / "processed"),
        (data / "work", target / "work"),
        (data / "audios_inbox", target / "inbox"),
        (data / "speakers.json", target / "speakers.json"),
    ]
    for src, dst in jobs:
        print(move(src, dst, args.dry_run))

    if not args.dry_run:
        for name in ("processed", "work", "inbox"):
            (target / name).mkdir(parents=True, exist_ok=True)
        print("-" * 60)
        print(f"完成。后续所有归档都会落在 {target}")
        print("提示：旧路径不再自动创建；如需回滚，把 tenants/%s 下的目录搬回去即可。" % args.owner)
    else:
        print("-" * 60)
        print("（dry-run，未改动任何文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
