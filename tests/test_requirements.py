#!/usr/bin/env python3
"""依赖声明与真实 import 的一致性（防止「装了 requirements 还是起不来」）。

## 为什么要有这个测试

2026-10-23 实测发现：`requirements.txt` 漏了 fastapi / uvicorn / httpx /
pyyaml / soundfile / scipy，而这些**没有任何上游包会带进来**
（`pip show` 的 Required-by 全为空）。后果是全新环境
`pip install -r requirements.txt` 之后仍然 `ModuleNotFoundError: fastapi`，
**服务根本起不来** —— 比「面板空白」严重得多，但本地开发机上永远发现不了，
因为那台机器早就装好了。

所以这个测试不看人记得住记不住，而是**扫源码里的真实 import**，
逐个核对它是否被两条依赖线之一覆盖：

    requirements.txt             基础（必装，不含 torch/funasr）
    requirements-asr-local.txt  本地模型（可选，含 torch/funasr/scipy）

## 判据

每个第三方模块都必须能落到下面之一：
  1. 基础依赖清单里（含别名：yaml→pyyaml、dotenv→python-dotenv …）
  2. 本地模型清单里
  3. `OPTIONAL` 白名单里，且**必须写明理由**（如 cosyvoice 只跑在独立 venv）

反向对照：故意往声明里删掉一个已知必需的包，断言本测试会红。

跑法：
    python tests/test_requirements.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TOTAL = 0
FAILED = 0

# Python 标准库（不完整，够用；漏了的会被当第三方报出来，宁可误报不漏报）
STDLIB = set("""
__future__ abc argparse array ast asyncio base64 binascii bisect builtins bz2 calendar cmath
codecs collections colorsys concurrent contextlib copy csv ctypes dataclasses datetime decimal
difflib dis email encodings enum errno faulthandler fcntl filecmp fileinput fnmatch fractions
functools gc getpass gettext glob grp gzip hashlib heapq hmac html http imaplib importlib
inspect io ipaddress itertools json keyword linecache locale logging lzma mailbox math mimetypes
mmap multiprocessing numbers operator os pathlib pickle pipes platform plistlib pprint profile
pstats pty pwd py_compile queue quopri random re readline reprlib resource secrets select
selectors shelve shlex shutil signal site smtplib socket sqlite3 ssl stat statistics string
struct subprocess sys sysconfig tarfile tempfile textwrap threading time timeit token
tokenize traceback tracemalloc types typing unicodedata unittest urllib uu uuid venv warnings
wave weakref webbrowser xml zipfile zlib zoneinfo
""".split())

# 包名 → 实际 import 名 的差异
IMPORT_TO_PKG = {
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "pil": "pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "edge_tts": "edge-tts",
    "pptx": "python-pptx",
    "docx": "python-docx",
}

# 不进任何 requirements 的第三方模块：必须写明理由
OPTIONAL = {
    # 只跑在 ~/tts/venv 独立环境（CosyVoice 与主 .venv 不能合并）
    "cosyvoice": "只在 tts_server.py 里用；TTS 跑在独立 venv ~/tts/venv",
    "torch": "属本地模型线，在 requirements-asr-local.txt",
    "torchaudio": "属本地模型线，在 requirements-asr-local.txt",
    "funasr": "属本地模型线，在 requirements-asr-local.txt",
    "modelscope": "属本地模型线，在 requirements-asr-local.txt",
    "whisperx": "可选后端，用 requirements-whisperx.txt",
    "scipy": "属本地模型线（聚类），在 requirements-asr-local.txt",
    "edge_tts": "TTS 备用合成，只在 tts_server.py 里按需 import（独立 venv）",
}

# 扫描时跳过的目录
SKIP_DIRS = {".git", ".venv", "__pycache__", ".ruff_cache", "knowledge", "node_modules",
             "tests", "tools", "deploy", "docs", "web", "android", ".mypy_cache"}


def check(desc: str, got, want) -> None:
    global TOTAL, FAILED
    TOTAL += 1
    if got == want:
        print(f"  ok   {desc}")
    else:
        FAILED += 1
        print(f"  FAIL {desc}\n         得={got!r} 期望={want!r}")


def parse_requirements(path: Path) -> set[str]:
    """抽出包名（小写、去版本号、去 extras）。"""
    if not path.is_file():
        return set()
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        names.add(re.split(r"[<>=!\[;]", line)[0].strip().lower())
    return names


def collect_third_party_imports() -> dict[str, set[str]]:
    """扫仓库里所有运行代码的 import，返回 {模块名: {出现在哪些文件}}。"""
    local = {p.stem for p in ROOT.glob("*.py")} | {"api"}
    found: dict[str, set[str]] = {}
    for p in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", src, re.M):
            name = m.group(1)
            if name in STDLIB or name in local:
                continue
            found.setdefault(name, set()).add(p.name)
    return found


def main() -> int:
    base_path = ROOT / "requirements.txt"
    local_path = ROOT / "requirements-asr-local.txt"

    print("1) 两个依赖文件都在")
    check("requirements.txt 存在", base_path.is_file(), True)
    check("requirements-asr-local.txt 存在", local_path.is_file(), True)

    base = parse_requirements(base_path)
    local_req = parse_requirements(local_path)

    print("\n2) 基础清单不含 torch / funasr（否则「最轻」路线名不副实）")
    for heavy in ("torch", "torchaudio", "funasr", "modelscope"):
        check(f"{heavy} 不在 requirements.txt", heavy in base, False)
    check("torch 在 requirements-asr-local.txt", "torch" in local_req, True)
    check("funasr 在 requirements-asr-local.txt", "funasr" in local_req, True)

    print("\n3) 曾经漏掉的六个包必须在基础清单里（本次修复的回归锁）")
    # 这六个此前漏了，且无上游带进来 → 全新环境起不来
    for pkg in ("fastapi", "uvicorn", "httpx", "pyyaml", "soundfile"):
        check(f"{pkg} 在 requirements.txt", pkg in base, True)
    check("scipy 在 requirements-asr-local.txt", "scipy" in local_req, True)

    print("\n4) 真实 import 全覆盖：每个第三方模块都要有归属")
    found = collect_third_party_imports()
    check("扫到的第三方模块数 > 10（防路径写错导致空扫）", len(found) > 10, True)

    uncovered: list[str] = []
    covered_base: list[str] = []
    covered_local: list[str] = []
    covered_opt: list[str] = []
    for mod in sorted(found):
        pkg = IMPORT_TO_PKG.get(mod, mod).lower()
        if pkg in base:
            covered_base.append(mod)
        elif pkg in local_req:
            covered_local.append(mod)
        elif mod in OPTIONAL or pkg in OPTIONAL:
            covered_opt.append(mod)
        else:
            uncovered.append(f"{mod}（出现在 {', '.join(sorted(found[mod]))}）")

    check(f"无未覆盖模块（漏的：{uncovered}）", uncovered, [])
    print(f"       基础 {len(covered_base)} · 本地 {len(covered_local)} · 可选 {len(covered_opt)}")

    print("\n5) OPTIONAL 白名单里每条都要写理由（防止拿它当垃圾桶）")
    no_reason = [k for k, v in OPTIONAL.items() if len(v.strip()) < 6]
    check(f"白名单都有理由（缺理由：{no_reason}）", no_reason, [])

    print("\n6) 云端 ASR 需要的 httpx 在基础线（否则 asr_backend=cloud 起不来）")
    check("httpx 在基础清单（不是可选）", "httpx" in base and "httpx" not in local_req, True)

    print("\n7) 反向对照：从声明里删掉 fastapi，第 3、4 组必须变红")
    # 不真改文件，只在内存里模拟一次判定，证明这套判据不是恒真
    broken_base = set(base) - {"fastapi"}
    flagged = []
    for mod in sorted(found):
        pkg = IMPORT_TO_PKG.get(mod, mod).lower()
        if (pkg not in broken_base and pkg not in local_req
                and mod not in OPTIONAL and pkg not in OPTIONAL):
            flagged.append(mod)
    check("删掉 fastapi 后能被抓出来", "fastapi" in flagged, True)
    check("且只抓到它（判据没有过度误报）", flagged, ["fastapi"])

    print("\n" + "-" * 46)
    print(f"通过 {TOTAL} 项，失败 {FAILED} 项")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
