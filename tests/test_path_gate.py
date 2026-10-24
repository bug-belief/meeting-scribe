#!/usr/bin/env python3
"""P2-8 路径门禁回归锁。

挡的是**不会让任何测试失败**的那类问题：仓库里带着某个人的目录结构发出去。
`/home/meeting`、`C:\\Users\\someone` 这些字符串在作者机器上永远是对的，
在别人机器上永远是错的，但没有任何测试会因此变红 —— 只能靠门禁。

两条断言同样重要：
  ① 违规必须被拦住（否则门禁是摆设）
  ② 占位符/假名必须放行（否则门禁变成噪音，大家会用 --no-verify 绕掉）
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "tools" / "check-style.sh"
TOTAL = 0
FAILS = 0


def check(name: str, got, want) -> None:
    global TOTAL, FAILS
    TOTAL += 1
    ok = got == want
    if not ok:
        FAILS += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}: 得={got!r} 期={want!r}")


def run_gate(cwd: Path) -> tuple[int, str]:
    r = subprocess.run(["bash", str(cwd / "tools" / "check-style.sh")],
                       cwd=str(cwd), capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def make_repo(files: dict[str, str]) -> Path:
    """造一个最小 git 仓（门禁靠 git ls-files 取清单）。"""
    d = Path(tempfile.mkdtemp(prefix="p8-"))
    (d / "tools").mkdir()
    (d / "tools" / "check-style.sh").write_text(
        GATE.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    for name, body in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8", newline="\n")
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"],
                ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(["git", *cmd], cwd=str(d), capture_output=True)
    return d


def main() -> int:
    print("1) 门禁里有这一层，且不是永远返回真")
    src = GATE.read_text(encoding="utf-8")
    check("有 ①-5 层", "①-5 个人绝对路径" in src, True)
    check("定义了匹配正则", "ABS_RE=" in src, True)
    check("定义了白名单", "ABS_OK_RE=" in src, True)
    # 🔴 断「豁免清单里必须有谁」，不断「用哪条命令实现」。
    #    原来这里断言的是 `grep -vE '^(tools/check-style...` 这个字面串，
    #    而注释本身就写着「断言机制存在，而不是某一种写法」—— 实现和注释自相矛盾。
    #    2026-10-24 把豁免收进 PATH_GATE_SKIP_RE 变量后，这条当场变红：
    #    功能没坏，是断言写死了实现。
    m = re.search(r"PATH_GATE_SKIP_RE='([^']*)'", src)
    check("定义了豁免清单", bool(m), True)
    skip_list = m.group(1) if m else ""
    for must in ("tools/check-style", "tools/sanitize-check", "tests/test_path_gate"):
        check(f"豁免清单含 {must}", must in skip_list, True)
    check("豁免清单被实际使用", 'grep -vE "$PATH_GATE_SKIP_RE"' in src, True)

    print("\n2) 违规样本：必须全部拦住")
    bad = {
        "a.txt": "/home/meeting/data/processed\n",
        "b.txt": "/home/ubuntu/meeting-asr\n",
        "c.txt": "/Users/john/Documents\n",
        "d.txt": "/root/app\n",
    }
    d = make_repo(bad)
    rc, out = run_gate(d)
    check("退出码 1", rc, 1)
    for token, label in (("/home/meeting", "linux 真实用户名"),
                         ("/home/ubuntu", "发行版默认用户名"),
                         ("/Users/john", "macOS 用户名"),
                         ("/root/", "root 家目录")):
        check(f"报出 {label}", token in out, True)
    # 行号也要对，否则不知道改哪
    check("带行号", "a.txt:1:" in out, True)

    print("\n3) 占位符与假名：必须放行（否则门禁会被绕掉）")
    ok = {
        "ok1.txt": "/home/xxx/app\n",
        "ok2.txt": "/home/user/data\n",
        "ok3.txt": "/home/<user>/app\n",
        "ok4.txt": "/home/alice/data\n",
        "ok5.txt": "/Users/you/x\n",
        "ok6.txt": "/Users/bob/y\n",
    }
    d2 = make_repo(ok)
    rc2, out2 = run_gate(d2)
    check("退出码 0", rc2, 0)
    check("没有任何路径告警", "个人绝对路径" in out2, False)

    print("\n4) 边界：混在一行里也要能判")
    mixed = {
        "m.txt": "占位 /home/xxx 与真实 /home/meeting 同时出现\n",
    }
    d3 = make_repo(mixed)
    rc3, out3 = run_gate(d3)
    check("含违规 → 退出码 1", rc3, 1)
    check("仍报出来", "/home/meeting" in out3, True)

    print("\n5) 真实仓库：当前必须零违规（零豁免，没有基线文件）")
    rc4, out4 = run_gate(ROOT)
    check("全仓门禁通过", rc4, 0)
    hits = [ln for ln in out4.splitlines() if "个人绝对路径" in ln]
    check("零违规", len(hits), 0)
    for h in hits[:5]:
        print(f"       {h[:110]}")

    print("\n6) 反向对照：把正则改坏后第 2 组必须变红")
    # 把 ABS_RE 换成一个不可能命中的模式，验证第 2 组不是恒真
    d4 = make_repo({"a.txt": "/home/meeting/x\n"})
    broken = (d4 / "tools" / "check-style.sh").read_text(encoding="utf-8")
    broken = broken.replace("ABS_RE='(/home/[a-z_][a-z0-9_-]*",
                            "ABS_RE='(NEVER_MATCH_[a-z_][a-z0-9_-]*", 1)
    (d4 / "tools" / "check-style.sh").write_text(broken, encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "-A"], cwd=str(d4), capture_output=True)
    rc5, out5 = run_gate(d4)
    check("正则失效时确实漏过（证明第 2 组不是恒真）", "个人绝对路径" in out5, False)

    print("\n7) 盘符路径：不只 C:\\Users，任意盘符目录都要抓")
    # 2026-10-24 补：原来只认 C:\Users\<name>，导致非用户目录的盘符路径全部漏检。
    # 样本用常见的开发盘路径，不涉及任何真实目录。
    win = {
        "priv.txt": "E:\\work\\proj\\x\n",            # 开发盘上的项目目录 → 报
        "cn.txt": "D:\\工具\\某目录\\x\n",             # 中文目录名 → 报
        "users.txt": "C:\\Users\\someone\\a\n",       # 用户目录 → 报（原有能力）
        "ok_placeholder.txt": "E:\\...\\watcher.py\n",  # 占位 → 放行
        "ok_generic.txt": "C:\\Windows\\System32\n",    # 通用系统目录 → 放行
    }
    d5 = make_repo(win)
    rc6, out6 = run_gate(d5)
    check("有违规时退出码非 0", rc6 != 0, True)
    for name in ("priv.txt", "cn.txt", "users.txt"):
        check(f"{name} 被抓", name in out6, True)
    for name in ("ok_placeholder.txt", "ok_generic.txt"):
        check(f"{name} 被放行", name in out6, False)

    print("\n8) 豁免不能越界：只对「规则定义文件 + 私有运维存档」生效")
    # 豁免的理由写在 check-style.sh 里。这里钉死两件事：
    #   ① 豁免清单里的私有存档，必须同时出现在 export-public.sh 的 EXCLUDE 里
    #      （否则「不导出」这个前提不成立，豁免就失去依据）；
    #   ② 随便造一个普通文件，含路径就必须被抓。
    gate_src = (ROOT / "tools" / "check-style.sh").read_text(encoding="utf-8")
    check("豁免清单变量存在", "PATH_GATE_SKIP_RE=" in gate_src, True)
    # 私仓里这两篇是「不导出且主题就是本机」；开源仓里它们已被剔除，
    # export-public.sh 也不存在。两种情况下都要跑 **同样条数** 的断言 ——
    # 条件放进期望值，不要写成 if/else 各跑不同条数（否则两仓基线不同必红）。
    exp_path = ROOT / "deploy" / "export-public.sh"
    exp_exists = exp_path.is_file()
    exp_src = exp_path.read_text(encoding="utf-8") if exp_exists else ""
    for doc in ("docs/仓库工作流-Gitee代码仓.md", "docs/部署现状与迁移清单.md"):
        # 没有 export-public.sh 时（开源仓）这条无从校验，视为满足
        check(f"{doc} 在导出 EXCLUDE 里", doc in exp_src if exp_exists else True, True)
    d6 = make_repo({"ordinary.txt": "E:\\work\\x\n"})
    rc7, out7 = run_gate(d6)
    check("普通文件不在豁免内", "个人绝对路径" in out7, True)

    print("\n" + "-" * 46)
    print(f"通过 {TOTAL - FAILS} 项，失败 {FAILS} 项")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
