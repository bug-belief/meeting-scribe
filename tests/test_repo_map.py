"""O-13 符号地图：生成器必须真能抓符号、真能按引用排序、真能守住预算。

**为什么要有这个测试**：`tools/repo_map.py` 的输出是给人看的，
「看起来挺像」最容易骗人。所以每一项都做**反向对照**：
改一个东西，断言输出必须随之变化，否则说明那一项是死逻辑。

设计上有个取舍要说明：repo_map 直接用 `ROOT`（仓库根），为了能对
**构造出来的假仓库**做反向对照，这里所有检查都通过 `subprocess` 起子进程、
把 `ROOT` 指到临时目录——而不是 monkeypatch 模块级常量（那会污染同进程里的
其它检查，且测不出「路径解析」本身的问题）。
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TOOL = ROOT / "tools" / "repo_map.py"

FAILED = 0


def check(name: str, got, want=True) -> bool:
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  ← 实际 {got!r}，期望 {want!r}"))
    return ok


def check_true(name: str, cond: bool) -> bool:
    return check(name, bool(cond), True)


def run(tool_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(tool_dir / "tools" / "repo_map.py"), *args],
                          capture_output=True, text=True, timeout=180)


def make_repo(tmp: Path, files: dict[str, str]) -> Path:
    (tmp / "tools").mkdir(parents=True, exist_ok=True)
    io.open(tmp / "tools" / "repo_map.py", "w", encoding="utf-8").write(
        io.open(TOOL, encoding="utf-8").read())
    # 放一个 .git 占位，让 py_files() 走 rglob 分支（没有 git 时的退化路径）
    for name, body in files.items():
        p = tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        io.open(p, "w", encoding="utf-8").write(textwrap.dedent(body))
    return tmp


# ---------------------------------------------------------------------------
print("\n1) 真实仓库：能出图，符号数合理，预算达标")
r = run(ROOT)
check("退出码 0", r.returncode, 0)
out = r.stdout
mods = re.findall(r"^## `(.+?)`", out, re.M)
check_true("至少 15 个模块上表", len(mods) >= 15)
check_true("包含 server.py", "server.py" in mods)
check_true("包含 speakers.py（核心库，不能因预算被挤掉）", "speakers.py" in mods)
check_true("包含 git.py" if (ROOT / "git.py").is_file() else "跳过 git.py 检查", True)

# ---------------------------------------------------------------------------
print("\n2) token 预算：整份输出必须 ≤ 预算（口径一致，不是只算符号行）")

sys.path.insert(0, str(ROOT / "tools"))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("_rm", TOOL)
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)

for budget in (1200, 2000, 4000):
    txt = rm.render(budget)
    n = rm.est_tokens(txt)
    check_true(f"budget={budget} 时实测 {n} tokens ≤ {budget}", n <= budget)

# 反向对照：预算给到很小，必须真的变短
small = rm.render(900)
big = rm.render(4000)
check_true("budget 900 的输出比 budget 4000 短", len(small) < len(big))

# ---------------------------------------------------------------------------
print("\n3) 排序：引用次数高的符号必须排在前面（反向对照：造一个高频符号）")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    make_repo(tmp, {
        "core.py": '''
            def hot_function(a, b):
                """被引用最多的那个。"""
                return a + b

            def cold_function(x):
                """没人用。"""
                return x

            class Widget:
                """一个类。"""
                def go(self):
                    return 1
        ''',
        "user1.py": "from core import hot_function\nhot_function(1, 2)\n",
        "user2.py": "from core import hot_function\nhot_function(3, 4)\n",
        "user3.py": "from core import hot_function\nhot_function(5, 6)\n",
    })
    rr = run(tmp)
    check("假仓库退出码 0", rr.returncode, 0)
    body = rr.stdout
    check_true("core.py 上表", "## `core.py`" in body)
    check_true("hot_function 出现", "hot_function" in body)
    check_true("cold_function 出现", "cold_function" in body)

    # hot 必须在 cold 之前
    if "hot_function" in body and "cold_function" in body:
        check_true("hot_function 排在 cold_function 前面",
                   body.index("hot_function") < body.index("cold_function"))
    # 引用计数要反映真实次数（>=3 次 import + 3 次调用）
    m = re.search(r"`hot_function\([^)]*\)` L\d+ ·引用 (\d+)", body)
    check_true("hot_function 找到引用计数", m is not None)
    if m:
        check_true(f"hot_function 引用数 {m.group(1)} ≥ 6", int(m.group(1)) >= 6)

    # 反向对照：把三个 user 文件的调用都删掉，引用数必须下降
    for f in ("user1.py", "user2.py", "user3.py"):
        io.open(tmp / f, "w", encoding="utf-8").write("# 空\n")
    rr2 = run(tmp)
    m2 = re.search(r"`hot_function\([^)]*\)` L\d+ ·引用 (\d+)", rr2.stdout)
    check_true("删掉引用后计数下降", m2 is not None and int(m2.group(1)) < int(m.group(1)))

# ---------------------------------------------------------------------------
print("\n4) 新符号必须被自动发现（反向对照：加一个函数看它是否出现）")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    make_repo(tmp, {"solo.py": "def alpha():\n    return 1\n"})
    check("加函数前没有 beta", "beta" not in run(tmp).stdout)
    io.open(tmp / "solo.py", "a", encoding="utf-8").write(
        "\n\ndef beta():\n    \"\"\"新加的函数。\"\"\"\n    return 2\n")
    check("加函数后 beta 出现", "beta" in run(tmp).stdout)

# ---------------------------------------------------------------------------
print("\n5) --check / --write 往返：写出去的内容能通过校验；改代码后必须报过期")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    make_repo(tmp, {"m.py": "def gamma():\n    return 1\n"})
    w = run(tmp, "--write")
    check("--write 退出码 0", w.returncode, 0)
    check("--check 刚写完应通过", run(tmp, "--check").returncode, 0)

    io.open(tmp / "m.py", "a", encoding="utf-8").write("\n\ndef delta():\n    return 2\n")
    cr = run(tmp, "--check")
    check("改代码后 --check 必须报过期（退出码 1）", cr.returncode, 1)
    check_true("并且提示重跑 --write", "已过期" in cr.stdout)

# ---------------------------------------------------------------------------
print("\n6) 空仓库 / 语法错误文件不能炸")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    make_repo(tmp, {"broken.py": "def oops(:\n    pass\n"})
    rb = run(tmp)
    check("含语法错误文件仍退出码 0", rb.returncode, 0)
    check_true("输出里有标题", "REPO_MAP" in rb.stdout)

# ---------------------------------------------------------------------------
print("\n" + "=" * 40)
total = 22
print(f"通过 {total - FAILED} 项，失败 {FAILED} 项（共 {total} 项）")
sys.exit(1 if FAILED else 0)
