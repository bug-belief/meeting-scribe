#!/usr/bin/env python3
"""给已有音色的 meta.json 补 `owner`，并把 `source` 的绝对路径裁成文件名。

为什么单独一个脚本：

- `owner` 是隔离的判定依据，`server.py` 靠它决定「这个音色归谁」。老音色没有
  这个字段，升级后会因为 `owner` 为空被当成「谁都能用」——先补成明确归属。
- `source` 存的是 `<家目录>/data/tenants/<owner>/processed/...` 这种绝对路径，
  `/api/tts/voices` 会原样返回（现已改成只回 basename）。数据源头也一并清掉，
  免得将来别的接口又把它读出去。

两套目录布局都认：

    voices/<name>/meta.json
    voices/<speaker>/<profile>/meta.json

用法：

    python3 deploy/tag_voice_owner.py                # 预演，只打印
    python3 deploy/tag_voice_owner.py --apply        # 真写，写前各留 .bak
    python3 deploy/tag_voice_owner.py --apply --owner mom
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path


def default_voices_dir() -> Path:
    raw = (os.getenv("TTS_VOICES_DIR") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "tts" / "voices"


def iter_meta(root: Path):
    """找出所有音色目录里的 meta.json（两层 + 一层布局）。"""
    if not root.is_dir():
        return []
    out: list[Path] = []
    for top in sorted(root.iterdir()):
        if not top.is_dir():
            continue
        for sub in sorted(top.iterdir()):
            if sub.is_dir() and (sub / "prompt.wav").is_file():
                meta = sub / "meta.json"
                if meta.is_file():
                    out.append(meta)
        if (top / "prompt.wav").is_file():
            meta = top / "meta.json"
            if meta.is_file():
                out.append(meta)
    return out


def basename_of(raw: str) -> str:
    """`/a/b/c.wav` → `c.wav`。已经是文件名就原样。"""
    return os.path.basename(str(raw).replace("\\", "/")) if raw else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="给音色补 owner、裁 source 路径")
    ap.add_argument("--dir", default="", help="音色根目录，默认 $TTS_VOICES_DIR 或 ~/tts/voices")
    ap.add_argument("--owner", default="me", help="要补的归属人，默认 me")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只看不写）")
    args = ap.parse_args()

    root = Path(args.dir).expanduser() if args.dir else default_voices_dir()
    metas = iter_meta(root)
    if not metas:
        print(f"❌ 在 {root} 下没找到任何带 prompt.wav 的音色")
        return 1

    changed = 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for meta in metas:
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ! {meta}: 读不了（{exc}），跳过")
            continue

        before_owner = str(info.get("owner") or "")
        before_src = str(info.get("source") or "")
        after_owner = before_owner or args.owner
        after_src = basename_of(before_src)

        if before_owner == after_owner and before_src == after_src:
            print(f"  · {meta.parent.name}: 已是 owner={after_owner}，无需改动")
            continue

        rel = meta.relative_to(root)
        print(f"  → {rel}")
        if before_owner != after_owner:
            print(f"      owner : {before_owner or '(空)'} → {after_owner}")
        if before_src != after_src:
            print(f"      source: ...{before_src[-40:] if len(before_src) > 40 else before_src}")
            print(f"            → {after_src}")

        if args.apply:
            backup = meta.with_suffix(f".json.bak-{stamp}")
            shutil.copy2(meta, backup)
            info["owner"] = after_owner
            if after_src:
                info["source"] = after_src
            meta.write_text(
                json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        changed += 1

    verb = "已改" if args.apply else "待改"
    print(f"\n{verb} {changed} 个 / 共 {len(metas)} 个音色")
    if changed and not args.apply:
        print("这是预演。确认无误后加 --apply 真写（每个文件会先留 .bak）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
