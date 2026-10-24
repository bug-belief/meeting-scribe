#!/usr/bin/env python3
"""找出 config.Settings 里"声明了但没人读"的字段。

为什么需要这个脚本
------------------
2026-10-19 重构审计第 5 项，我先用「统计 `.字段` 属性访问次数」的办法找死声明，
结论是 3 个字段零读取。实际复核发现其中 2 个是在用的——它们走的是
`getattr(settings, "字段")` 字符串形式，正则只认点号，看不见。

差点因此删掉在用的配置。这个脚本把两种访问形式都算上，
让"死声明"变成可复现的检查，而不是每次靠人肉统计、每次都错一遍。

判定口径（宁可漏报，不可误报）
------------------------------
算「被读取」的四种形式：
  1. `SETTINGS.字段` / `settings.字段` / 任意 `<x>.字段`
  2. `getattr(<任意>, "字段")` —— 单双引号都算
  3. `getattr(<任意>, "字段", <默认值>)`
  4. dict 形式 `settings["字段"]`（防御性写法，目前代码里没有）

不算读取的：
  - 字段在 config.py 里的声明行与赋值行（本来就是源头）
  - 注释与文档字符串中的出现（用 tokenize 剥离，避免"文档提了一句"被当成在用）
  - 字符串里恰好包含字段名（如日志文案），除非它出现在 getattr 调用位

用法
----
    python tools/check-dead-settings.py            # 报告，有问题返回 1
    python tools/check-dead-settings.py -v         # 连"仅 1 处读取"也列出来
    python tools/check-dead-settings.py --json     # 机器可读

注意：这是静态检查，覆盖不到 `**asdict(SETTINGS)` 这类整体遍历。
config.py 的 `--print-config` 就是整体遍历，不影响判定（那是输出，不是使用）。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.py"

# 这些前缀的路径不参与扫描：外部语料、虚拟环境、构建产物
SKIP_PREFIXES = ("knowledge/", ".venv/", "venv/", "dist/", "node_modules/")
# 只跳过脚本自己。**不能跳过 config.py**：`web_token` / `tenant_token_map`
# 的读取点全在 config.py 自己的 resolve_tenant() / known_tenants() 里，
# 整文件跳过会把两个在用字段误报成死声明（2026-10-20 实测踩到）。
SKIP_FILES = {"tools/check-dead-settings.py"}


def settings_fields(config_path: Path = CONFIG) -> list[str]:
    """从 config.py 的 class Settings 里取字段名（保持源码顺序）。"""
    src = config_path.read_text(encoding="utf-8")
    start = src.index("class Settings")
    # 类体结束于下一个顶层 def/class
    rest = src[start:]
    m = re.search(r"^\s*def |^class ", rest[len("class Settings"):], re.M)
    body = rest[: len("class Settings") + m.start()] if m else rest
    return re.findall(r"^    (\w+):\s", body, re.M)


def strip_comments_and_strings(text: str, name: str = "") -> str:
    """把注释与字符串字面量替换成等长空格，其余字符原样保留。

    关键：**不能改变字符位置**。早先写成 `" ".join(tokens)`，结果
    `settings.foo` 里的 `.` 和 `foo` 被插进空格，`\\.foo\\b` 就匹配不到了，
    整个检查会静默失效。空格填充保证位置与换行结构完全不变。
    """
    lines = text.splitlines(keepends=True)
    buf = [list(line) for line in lines]

    def blank(row: int, lo: int, hi: int) -> None:
        if not (1 <= row <= len(buf)):
            return
        line = buf[row - 1]
        for i in range(max(lo, 0), min(hi, len(line))):
            if line[i] not in "\r\n":
                line[i] = " "

    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            (srow, scol), (erow, ecol) = tok.start, tok.end
            if srow == erow:
                blank(srow, scol, ecol)
            else:
                blank(srow, scol, len(buf[srow - 1]))
                for r in range(srow + 1, erow):
                    blank(r, 0, len(buf[r - 1]))
                blank(erow, 0, ecol)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 语法不完整的文件退化为原文：宁可多报（当成在用），也不静默跳过
        return text

    return "".join("".join(line) for line in buf)


def py_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()
    if not out:  # 非 git 环境退化
        out = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")]
    return [
        ROOT / f
        for f in out
        if not f.startswith(SKIP_PREFIXES) and f not in SKIP_FILES and (ROOT / f).is_file()
    ]


def mask_declarations(text: str, field: str) -> str:
    """把该字段的「声明行」和「load_settings 赋值行」整行抹白。

    这两行是字段的源头，不是读取：
        funasr_spk: str                       ← class Settings 里的声明
        funasr_spk=_str("FUNASR_SPK", ...),   ← load_settings 里的赋值
    不抹掉的话，config.py 中每个字段都会被自己算成"读过 1 次"，
    死声明永远查不出来。保留换行，行号与位置不变。
    """
    decl = re.compile(rf"^\s*{re.escape(field)}\s*:\s")
    assign = re.compile(rf"^\s*{re.escape(field)}\s*=\s*_")
    out = []
    for line in text.splitlines(keepends=True):
        if decl.match(line) or assign.match(line):
            body = line.rstrip("\r\n")
            out.append(" " * len(body) + line[len(body):])
        else:
            out.append(line)
    return "".join(out)


def count_reads(field: str, files: list[Path]) -> tuple[int, list[str]]:
    """返回 (读取处数, 命中文件列表)。

    属性访问扫「剥掉注释与字符串后的骨架」，避免文档里提一句就被当成在用；
    getattr / 下标形式必须看原文，因为字段名本身就在字符串里。
    """
    attr_re = re.compile(rf"\.{re.escape(field)}\b")
    getattr_re = re.compile(rf"""getattr\(\s*[\w.]+\s*,\s*["']{re.escape(field)}["']""")
    sub_re = re.compile(rf"""\[\s*["']{re.escape(field)}["']\s*\]""")

    total = 0
    hits: list[str] = []

    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = mask_declarations(text, field)
        skeleton = strip_comments_and_strings(text, p.name)
        n = (
            len(attr_re.findall(skeleton))
            + len(getattr_re.findall(text))
            + len(sub_re.findall(text))
        )
        if n:
            total += n
            hits.append(f"{p.relative_to(ROOT).as_posix()} ×{n}")

    return total, hits


def main() -> int:
    ap = argparse.ArgumentParser(description="查出声明了但没人读的 Settings 字段")
    ap.add_argument("-v", "--verbose", action="store_true", help="同时列出仅 1 处读取的字段")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    fields = settings_fields()
    files = py_files()
    if not files:
        print("没找到 .py 文件，检查是否在仓库根目录运行", file=sys.stderr)
        return 2

    dead, low = [], []
    for f in fields:
        n, hits = count_reads(f, files)
        if n == 0:
            dead.append({"field": f, "reads": 0})
        elif n == 1:
            low.append({"field": f, "reads": n, "at": hits[:1]})

    if args.json:
        print(json.dumps(
            {"fields": len(fields), "files": len(files), "dead": dead, "once": low},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(f"Settings 字段 {len(fields)} 个（扫描 {len(files)} 个 .py）")
        if dead:
            print(f"\n死声明 {len(dead)} 个 —— 声明了但没有任何代码读：")
            for d in dead:
                print(f"   ✗ {d['field']}")
        else:
            print("\n死声明 0 个")
        if args.verbose and low:
            print(f"\n仅 1 处读取 {len(low)} 个（可能是漏删也可能是单点使用，需人工判断）：")
            for d in low:
                print(f"   · {d['field']}  ← {d['at'][0]}")

    return 1 if dead else 0


if __name__ == "__main__":
    raise SystemExit(main())
