#!/usr/bin/env python3
"""符号地图生成器（O-13 / A-5）。

**要解决的问题**：63 个 .py、12,752 行，新会话或接手的人想知道「XX 在哪」只能 grep。
grep 能查到定义，但查不出「谁最常被引用」——也就是改动风险在哪。

**做法**（只用标准库 `ast`，不引 tree-sitter：那要编译 C 扩展，
和本项目「托管 venv 零额外依赖」的约定冲突，收益不值这个代价）：

  ① 遍历全部 .py（跳过 tests/ 里的一次性诊断脚本除外——测试也算进引用计数）
  ② 抽出模块级符号：函数 / 类 / 顶层常量，记录签名、行号、docstring 首句
  ③ 统计每个符号在**全仓源码里被引用的次数**（`ast.Name` / `ast.Attribute` 命中）
  ④ 按引用次数降序，输出 markdown，按 token 预算裁剪

**为什么按引用次数排序**：入度高的符号 = 改它会牵动最多地方 = 最该先知道它在哪。
这比按字母序或按文件序有用得多。

用法：
    python tools/repo_map.py                 # 打印到 stdout
    python tools/repo_map.py --write         # 写入 docs/REPO_MAP.md
    python tools/repo_map.py --budget 2000   # token 预算（默认 2000）
    python tools/repo_map.py --check         # 校验 docs/REPO_MAP.md 是否过期（CI/门禁用）

token 估算：中文按 1 字 1 token、ASCII 按 4 字符 1 token 的粗估，
**故意偏保守**（宁可少写几行，也别超预算）。
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "REPO_MAP.md"

# 哪些目录里的符号值得上图。tests/ 的符号只参与引用计数，不作为条目列出。
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "build", "dist", "knowledge"}
SKIP_PREFIX = ("tests/", "deploy/")          # deploy 脚本单独做简表，不混进符号
MAX_FILES_PER_MODULE = 60                     # 单模块最多列几个符号

# 一批不值得占版面的名字：到处都在用，写进地图等于噪音
NOISE = {
    "main", "run", "get", "post", "put", "delete", "setup", "__init__", "__enter__",
    "__exit__", "__repr__", "__str__", "self", "cls", "args", "kwargs", "logger",
    "app", "router", "test", "check", "print", "log", "ok", "err", "path", "root",
}

# 中文 token 估算权重（保守）
CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def est_tokens(s: str) -> int:
    """粗估 token：中文字符按 1、其余按 4 字符 1 token。"""
    cjk = len(CJK.findall(s))
    other = len(s) - cjk
    return int(cjk + other / 4) + 1


def py_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "-z", "*.py"],
        cwd=ROOT, capture_output=True,
    )
    names = [n for n in out.stdout.decode("utf-8").split("\0") if n]
    if not names:                              # 非 git 环境退化为 walk
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")]
    keep = []
    for n in names:
        parts = n.split("/")
        if any(d in SKIP_DIRS for d in parts[:-1]):
            continue
        keep.append(n)
    return [ROOT / n for n in sorted(keep)]


def first_sentence(doc: str | None, limit: int = 46) -> str:
    if not doc:
        return ""
    txt = " ".join(doc.strip().split())
    # 取第一句：优先中文句号，其次英文句点
    for sep in ("。", ". ", "；", "; "):
        i = txt.find(sep)
        if 0 < i < limit:
            txt = txt[:i]
            break
    if len(txt) > limit:
        txt = txt[:limit] + "…"
    return txt


def sig_of(node: ast.AST) -> str:
    """把函数签名压成一行：`name(a, b, *args, k=None)` 里的括号内部。

    ⚠️ 不要给参数加 `*` 前缀来标记「有默认值」—— `*x` 在 Python 里是
    「拆包」的视觉暗示，读起来像 `*args`，实测第一版就这么误导了。
    参数太多时直接截断，反正地图只需要「大致长什么样」。
    """
    if isinstance(node, ast.ClassDef):
        return ""
    a = node.args  # type: ignore[attr-defined]
    names = [x.arg for x in getattr(a, "posonlyargs", [])] + [x.arg for x in a.args]
    names = [n for n in names if n not in {"self", "cls"}]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names += [k.arg or "?" for k in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    if len(names) > 5:
        return ", ".join(names[:4]) + ", …"
    return ", ".join(names)


def collect():
    """返回 (符号表, 引用计数)。符号表：{module: [(name, kind, lineno, sig, doc)]}"""
    refs: Counter[str] = Counter()
    symbols: dict[str, list[tuple[str, str, int, str, str]]] = defaultdict(list)
    parsed = []

    for path in py_files():
        rel = path.relative_to(ROOT).as_posix()
        try:
            src = io.open(path, encoding="utf-8", errors="replace").read()
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue
        parsed.append((rel, tree))

    # 先统计引用：全仓出现的名字
    for rel, tree in parsed:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                refs[node.id] += 1
            elif isinstance(node, ast.Attribute):
                refs[node.attr] += 1
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    refs[alias.name] += 1
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    refs[alias.name.split(".")[0]] += 1

    # 再抽符号（只要模块级）
    for rel, tree in parsed:
        if rel.startswith(SKIP_PREFIX):
            continue
        items = symbols[rel]
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                sig = sig_of(node)
                items.append((node.name, "def", node.lineno, sig,
                              first_sentence(ast.get_docstring(node))))
            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue
                pub = [n.name for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and not n.name.startswith("_")]
                items.append((node.name, "class", node.lineno, f"{len(pub)} 个公开方法",
                              first_sentence(ast.get_docstring(node))))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if (isinstance(t, ast.Name) and t.id.isupper()
                            and len(t.id) > 3):
                        items.append((t.id, "const", node.lineno, "", ""))
    return symbols, refs


def render(budget: int) -> str:
    symbols, refs = collect()

    # 先算每个模块的总引用热度，用来决定模块间顺序
    mod_score = {}
    for mod, items in symbols.items():
        mod_score[mod] = sum(refs.get(n, 0) for n, *_ in items)

    lines: list[str] = []
    lines.append("# REPO_MAP · 符号地图（自动生成，勿手改）")
    lines.append("")
    lines.append(f"> 由 `tools/repo_map.py` 生成于 {date.today().isoformat()}，"
                 f"按**被引用次数**降序。改了代码就重跑：")
    lines.append("> `python tools/repo_map.py --write`。"
                 "检查是否过期：`python tools/repo_map.py --check`。")
    lines.append(">")
    lines.append("> 用途：新接手时先看这里，`grep` 查「叫什么」、这张表查「改哪个最危险」。")
    lines.append("")

    used = est_tokens("\n".join(lines))
    listed = 0
    # 预算口径是**整份文件**（含表头、模块标题、收尾说明），不是只算符号行。
    # 第一版只算符号行，报「符号 1,955 达标」而文件实测 2,393 —— 差 400+。
    # 现在收尾阶段用实际拼出来的全文重新估算，见下面 tail_cost 那段。

    # ---- 第一轮：每个模块都至少给 2 行（保证「地图上有这个名字」）----
    # 两轮走，是因为一轮按热度贪心会把预算全吃光，排序第 9 名之后的模块
    # （git.py / speakers.py 这类核心库）直接消失，地图就只覆盖了前 3 成代码。
    plan: list[tuple[str, list[str], list[str]]] = []
    for mod in sorted(symbols, key=lambda m: -mod_score[m]):
        items = symbols[mod]
        if not items:
            continue
        items = sorted(items, key=lambda x: (-refs.get(x[0], 0), x[2]))[:MAX_FILES_PER_MODULE]
        head = f"## `{mod}`"
        body = []
        for name, kind, lineno, sig, doc in items:
            n = refs.get(name, 0)
            if name.lower() in NOISE:
                continue
            if kind == "const":
                row = f"- `{name}` L{lineno}  ·引用 {n}"
            elif kind == "class":
                row = f"- **`{name}`** L{lineno} ·类·{sig} ·引用 {n}"
            else:
                row = f"- `{name}({sig})` L{lineno} ·引用 {n}"
            if doc:
                row += f" — {doc}"
            body.append(row)
        if body:
            plan.append((head, body, [head]))

    def emit(head: str, rows: list[str], more: int) -> int:
        nonlocal used, listed
        block = "\n".join([head, ""] + rows + [""])
        cost = est_tokens(block)
        lines.append(head)
        lines.append("")
        lines.extend(rows)
        if more:
            lines.append(f"（另有 {more} 个符号引用 ≤1，`--budget` 加大可见）")
        lines.append("")
        used += cost
        listed += len(rows)
        return cost

    budget_left = budget - used
    # 第一轮配额：每模块 2 行，按热度顺序发，发完为止
    first: list[tuple[str, list[str], list[str]]] = []
    for head, body, _ in plan:
        take = min(2, len(body))
        cost = est_tokens("\n".join([head, ""] + body[:take] + [""]))
        if cost > budget_left:
            continue
        budget_left -= cost
        first.append((head, body[:take], body))

    # 第二轮：把剩余预算按热度回填到**已上表**的模块（不回填新模块，避免又失衡）
    for idx, (head, taken, body) in enumerate(first):
        if taken is body:
            continue
        add = []
        for row in body[len(taken):]:
            cand = taken + add + [row]
            cost = est_tokens("\n".join([head, ""] + cand + [""]))
            if cost > budget_left:
                break
            add.append(row)
        if add:
            budget_left -= est_tokens("\n".join([head, ""] + add + [""]))
            first[idx] = (head, taken + add, body)

    for head, rows, body in first:
        emit(head, rows, len(body) - len(rows))

    # 收尾预留：这两行必须放得下，否则「预算达标」是假的。
    # ⚠️ 口径必须一致：`used` 是逐块累加的**近似**（每块末尾换行各算 1 个字符，
    # 拼起来只算 1 个），比整文件实测少算了一部分。所以收尾时**用实际拼出来的
    # 全文重新估算**，而不是继续用累加值 —— 第一版就是拿累加值报「1,976 达标」，
    # 整文件实测 2,393，差了 400+。
    tail_cost = est_tokens(f"> 共列出 {listed} 个符号，估算 9999 tokens（预算 {budget}）。\n")
    while first:
        actual = est_tokens("\n".join(lines)) + tail_cost
        if actual <= budget:
            break
        head, rows, body = first.pop()
        listed -= len(rows)
        dropped = len(rows) + 3 if len(rows) < len(body) else len(rows) + 2
        del lines[-dropped:]

    actual = est_tokens("\n".join(lines)) + tail_cost
    lines.append(f"> 共列出 {listed} 个符号，估算 {actual} tokens（预算 {budget}）。")
    if actual > budget:
        # 每个模块 2 行的下限本身就可能超预算（模块数多的时候）。
        # 如实报告，不假装达标——「≤2k」这个验收口径要能被机器检查。
        lines.append(f"> ⚠️ 超出预算 {actual - budget} tokens："
                     f"模块数 × 每模块 2 行的下限已经装不下，收窄 `SKIP_PREFIX` 或调大 `--budget`。")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成符号地图（按引用次数排序）")
    ap.add_argument("--budget", type=int, default=2000, help="token 预算，默认 2000")
    ap.add_argument("--write", action="store_true", help=f"写入 {OUT.relative_to(ROOT)}")
    ap.add_argument("--check", action="store_true", help="校验现有文件是否过期（过期返 1）")
    args = ap.parse_args(argv)

    text = render(args.budget)

    if args.check:
        if not OUT.is_file():
            print(f"FAIL {OUT.relative_to(ROOT)} 不存在，先跑 --write")
            return 1
        old = io.open(OUT, encoding="utf-8").read()
        # 忽略生成日期那一行（每天都会变）
        strip = lambda s: re.sub(r"生成于 \d{4}-\d{2}-\d{2}", "", s)  # noqa: E731
        if strip(old) != strip(text):
            print(f"FAIL {OUT.relative_to(ROOT)} 已过期，重跑：python tools/repo_map.py --write")
            return 1
        print(f"ok   {OUT.relative_to(ROOT)} 是最新的")
        return 0

    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        io.open(OUT, "w", encoding="utf-8", newline="\n").write(text)
        print(f"已写入 {OUT.relative_to(ROOT)}（{text.count(chr(10))} 行，"
              f"估算 {est_tokens(text)} tokens）")
        return 0

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
