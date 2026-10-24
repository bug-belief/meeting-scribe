#!/usr/bin/env python3
"""只读体检：扫历史落盘数据，报告哪些不符合 `schema/*.schema.json`。

为什么需要这个脚本
------------------
`schema_guard` 只在「新建」处做严格校验（新建归档的 meta、每条用量记录），
历史数据一律不校验——因为 refresh_archive / backfill 这类修补工具面对的就是
字段不全的老归档，在那里卡住等于把工具废掉。

于是出现一个盲区：结构契约升级后，没人知道线上历史数据到底有多少不合规。
这个脚本只读不写、不改任何文件，把盲区变成一份可复现的清单。

它不阻断任何流程（exit 0/1 只代表人看不看），验收口径是：
**先看清有多少不合规，再决定要不要迁移**——而不是先改数据再看会不会崩。

用法
----
    python tools/check-schemas.py                      # 用默认位置
    python tools/check-schemas.py --tenants ~/data/tenants
    python tools/check-schemas.py --limit 5            # 每类只列前 5 条
    python tools/check-schemas.py --json               # 机读输出

退出码：0 全部合规 / 1 有不合规 / 2 环境错误（目录都找不到）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 允许从仓库任意位置调用：tools/ 的上一级就是仓库根
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schema_guard  # noqa: E402


def _shorten(path: str) -> str:
    """state.json 的 key 是 64 位 sha256，原样打印会把一行撑爆。"""
    return re.sub(r"\b([0-9a-f]{8})[0-9a-f]{40,56}([0-9a-f]{4})\b", r"\1…\2", path)


# 明细硬上限：账本可能几十万行，全记在内存里没必要。显示条数另由 --limit 控制。
MAX_PROBLEMS = 5000


def check_metas(tenants: Path) -> tuple[int, list[str]]:
    total = 0
    problems: list[str] = []
    if not tenants.is_dir():
        return 0, []
    for meta_path in sorted(tenants.glob("*/processed/*/meta.json")):
        total += 1
        if len(problems) >= MAX_PROBLEMS:
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{meta_path.relative_to(tenants)}: 读不了/不是合法 JSON（{exc}）")
            continue
        for one in schema_guard.violations("meta", data):
            problems.append(f"{meta_path.relative_to(tenants)}: {_shorten(one)}")
    return total, problems


def check_states(tenants: Path) -> tuple[int, list[str]]:
    total = 0
    problems: list[str] = []
    if not tenants.is_dir():
        return 0, []
    for state_path in sorted(tenants.glob("*/work/state.json")) + sorted(
        tenants.glob("*/state.json")
    ):
        total += 1
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{state_path.relative_to(tenants)}: 读不了/不是合法 JSON（{exc}）")
            continue
        for one in schema_guard.violations("state", data):
            problems.append(f"{state_path.relative_to(tenants)}: {_shorten(one)}")
    return total, problems


def check_usage(usage: Path) -> tuple[int, list[str]]:
    total = 0
    problems: list[str] = []
    if not usage.is_file():
        return 0, []
    try:
        lines = usage.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return 0, [f"{usage}: 读不了（{exc}）"]
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        total += 1
        if len(problems) >= MAX_PROBLEMS:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"{usage.name}:{lineno}: 不是合法 JSON（{exc}）")
            continue
        for one in schema_guard.violations("usage", record):
            problems.append(f"{usage.name}:{lineno}: {one}")
    return total, problems


def main() -> int:
    ap = argparse.ArgumentParser(description="只读体检历史落盘数据的 schema 合规性")
    ap.add_argument("--tenants", default=str(Path.home() / "data" / "tenants"),
                    help="租户根目录（默认 ~/data/tenants）")
    ap.add_argument("--usage", default="",
                    help="usage.jsonl 路径（默认 <tenants 的上一级>/usage.jsonl）")
    ap.add_argument("--limit", type=int, default=10, help="每类最多列多少条明细（默认 10）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机读）")
    args = ap.parse_args()

    tenants = Path(args.tenants).expanduser()
    usage = Path(args.usage).expanduser() if args.usage else tenants.parent / "usage.jsonl"

    if not tenants.is_dir() and not usage.is_file():
        print(f"❌ 既找不到租户目录（{tenants}）也找不到用量账本（{usage}）")
        print("   如果数据不在默认位置，用 --tenants/--usage 指定")
        return 2

    n_meta, p_meta = check_metas(tenants)
    n_state, p_state = check_states(tenants)
    n_usage, p_usage = check_usage(usage)

    result = {
        "tenants": str(tenants),
        "usage": str(usage),
        "meta": {"checked": n_meta, "problems": p_meta},
        "state": {"checked": n_state, "problems": p_state},
        "usage_rows": {"checked": n_usage, "problems": p_usage},
    }
    total_problems = len(p_meta) + len(p_state) + len(p_usage)
    result["ok"] = total_problems == 0

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    print("[check-schemas] 只读体检（不修改任何文件）")
    print(f"  租户目录：{tenants}")
    print(f"  用量账本：{usage}")
    print()

    for label, checked, problems in (
        ("meta.json", n_meta, p_meta),
        ("state.json", n_state, p_state),
        ("usage.jsonl", n_usage, p_usage),
    ):
        mark = "✅" if not problems else "⚠️"
        print(f"  {mark} {label:12s} 检查 {checked:5d} 个，问题 {len(problems)} 条")
        for one in problems[: args.limit]:
            print(f"       · {one}")
        if len(problems) > args.limit:
            print(f"       …… 另有 {len(problems) - args.limit} 条，用 --limit 调大")

    print()
    if result["ok"]:
        print("结论：全部符合 schema 契约")
        return 0
    print(f"结论：共 {total_problems} 处不合规。**这不阻断任何流程**——")
    print("      历史数据本就可能缺字段，先看清清单再决定要不要迁移。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
