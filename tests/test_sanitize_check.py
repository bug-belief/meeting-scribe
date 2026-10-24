#!/usr/bin/env python3
"""脱敏门禁回归锁。

这道门禁挡的是**不会让任何测试失败**的那类问题：真实姓名、内网 IP、住址、案情
留在仓库里。它们不影响功能，只会让仓库一公开就把这些带出去，而且一旦进了
git 历史，`git rm` 也删不干净。

所以它自己必须有测试 —— 一个「永远返回通过」的门禁比没有门禁更糟，
因为它会让人以为查过了。

四组断言同等重要：
  ① 该报的必须报（规则没写错）
  ② 不该报的必须放（白名单没写歪，否则大家会用 --no-verify 绕掉）
  ③ 把规则改坏必须漏（证明 ① 不是恒真）
  ④ 没有文件豁免（上一版的核心缺陷：豁免让门禁自己成了泄漏点）

🔴 本文件里**不允许出现真实姓名 / 真实 IP 字面量**。
   测试样本一律虚构 + 运行时拼接 —— 否则「为了测脱敏而写出真名」，
   本文件就成了新的泄漏点。这不是洁癖：上一版 19/25 处泄漏正出自这里。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TOTAL = 0
FAILS = 0


def check(name: str, got, want) -> None:
    global TOTAL, FAILS
    TOTAL += 1
    ok = got == want
    if not ok:
        FAILS += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}: 得={got!r} 期={want!r}")


_spec = importlib.util.spec_from_file_location("sanitize_check", ROOT / "tools" / "sanitize-check.py")
SC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SC)          # type: ignore[union-attr]


def kinds(text: str, rules=None) -> list[str]:
    """扫一段文本，返回命中的规则名列表。"""
    return [n for n, _w, _a in SC.scan_text(text, rules)]


# --------------------------------------------------------------------------- #
# 测试用的假规则：验证「引擎拿到规则后能不能正确命中」，不涉及任何真人信息。
# --------------------------------------------------------------------------- #
FAKE_RULES = [
    ("假真名", r"王五|赵六", "换成 张三/李四", []),
    # 白名单必须两种形态都写：点号（package="a.b.c"）与路径（java/a/b/c/）。
    # 只写一种的话，另一种形态会被误报 —— 测试第 2 组当场抓到过。
    ("假账号", r"acct-9x", "换成 <你的用户名>", [r"pkg\.acct-9x", r"pkg/acct-9x"]),
    ("假住址", r"某甲县|某乙镇", "删掉", []),
    ("假案情", r"9,?999\s*公里", "整篇移出", []),
    # 通用规则照常参与，验证它们与个人规则共存时互不干扰
    *SC.GENERIC_RULES,
]


def main() -> int:
    print("1) 该报的必须报（引擎 + 注入的假规则）")
    cases = [
        ("真名", "这个月的纪要交给王五看", "假真名"),
        ("真名（第二个）", "赵六辩称没有依据", "假真名"),
        ("账号名", "git@gitee.com:acct-9x/demo.git", "假账号"),
        ("住址", "老家在某甲县某乙镇那边", "假住址"),
        ("案情", "车辆 7 个月、9,999 公里", "假案情"),
        # 通用规则（真实生效的那几条）。
        # ⚠️ 样本一律**运行时拼接**：这些 IP/域名形态本身就被规则拦，
        #    直接写成字面量会让本文件自己被判红（第一版就是这么翻车的）。
        ("内网 IP（CGNAT）", "访问 http://" + "100." + "86.75.4" + ":8848 即可", "内网 IP"),
        ("内网 IP（家用）", "路由是 " + "192." + "168.1.1", "内网 IP"),
        ("隧道域名", "https://demo.tail" + "abc123.ts.net/?k=x", "隧道域名"),
        # 个人域名拼出来：既测到规则，又不让文件里出现一个可直接使用的邮箱
        ("个人邮箱", "发到 someone@" + "qq.com", "个人邮箱"),
    ]
    for label, text, want_kind in cases:
        got = kinds(text, FAKE_RULES)
        check(f"{label} → 报 {want_kind}", want_kind in got, True)

    print("\n2) 不该报的必须放（白名单粒度 = 字符串，不是文件）")
    # 用假包名验证白名单机制：pkg.acct-9x 在 FAKE_RULES 的 allow 里
    allowed = [
        ("包名（点号形式）", '    package="pkg.acct-9x"'),
        ("包名（路径形式）", "android/java/pkg/acct-9x/MainActivity.java"),
        ("占位符用户名", "git@gitee.com:<你的用户名>/repo.git"),
        ("占位符 IP", "访问 http://<你的内网IP>:8848"),
        ("占位符域名", "https://<你的域名>/"),
        ("示例邮箱", "you@example.com"),
        ("虚构中文名", "记录：张三和李四讨论接口联调"),
        ("省+市（粗粒度不算住址）", "所在城市：某省某市"),
        ("公网文档保留段 IP", "203.0.113.9"),
        ("普通端口号", "监听 0.0.0.0:8848 与 127.0.0.1:9880"),
    ]
    for label, text in allowed:
        got = kinds(text, FAKE_RULES)
        check(f"{label} → 不报", got, [])

    print("\n3) 白名单只放行约定形态，同文件别处写脏仍要报")
    mixed = 'package="pkg.acct-9x"  联系 someone@' + "foxmail.com"
    got = kinds(mixed, FAKE_RULES)
    check("包名放行但邮箱仍被抓", "个人邮箱" in got, True)
    mixed2 = 'package="pkg.acct-9x" # 作者 acct-9x'
    check("裸账号名仍被抓", "假账号" in kinds(mixed2, FAKE_RULES), True)

    print("\n4) 真实仓库必须零命中")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "sanitize-check.py"), "--json"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    check("退出码 0", r.returncode, 0)
    data = json.loads(r.stdout or "{}")
    probs = data.get("problems", [])
    check("零命中", len(probs), 0)
    check("确实扫了文件（不是空跑）", data.get("scanned", 0) > 100, True)
    for p in probs[:5]:
        print(f"       {p['file']}:{p['at']}")

    print("\n5) 跳过规则：语料与二进制不参与")
    # ⚠️ 用 any(...in...) 而不是 `"x" in tuple`：后者判的是「元素等于」，不是「包含」。
    check("knowledge/legal 跳过",
          any("knowledge/legal" in pre for pre in SC.SKIP_PREFIX), True)
    check("png 在跳过扩展名里", ".png" in SC.SKIP_EXT, True)
    check("wav 在跳过扩展名里", ".wav" in SC.SKIP_EXT, True)
    # 用**真实存在**的文件测（scannable 会过滤掉磁盘上不存在的路径）
    keep = SC.scannable(["README.md", "a.png", "b.wav", "knowledge/legal/x.md"])
    check("scannable 只留真实存在的文本文件", keep, ["README.md"])

    print("\n6) 反向对照：把规则改坏，第 1 组必须漏")
    broken = [("假真名", r"NEVER_MATCH_ANYTHING", "x", [])]
    sample = "这个月的纪要交给王五看"
    check("规则失效后确实漏过", len(SC.scan_text(sample, broken)), 0)
    check("规则正常时能抓到同一段", len(SC.scan_text(sample, FAKE_RULES)) > 0, True)

    print("\n7) 反向对照：白名单写歪 → 第 2 组会误报（说明白名单是必要的）")
    strict = [("假账号", r"acct-9x", "x", [])]     # 去掉包名白名单
    check("无白名单时包名被误报", len(SC.scan_text('package="pkg.acct-9x"', strict)), 1)

    print("\n8) 没有文件豁免 —— 上一版的核心缺陷")
    # 上一版豁免了引擎与测试文件，结果那 19 处真名成了开源仓最集中的泄漏点。
    src = (ROOT / "tools" / "sanitize-check.py").read_text(encoding="utf-8")
    check("源码里没有 SELF 豁免变量", "SELF = " in src, False)
    check("源码里没有 SAMPLE_LIB 豁免变量", "SAMPLE_LIB = " in src, False)
    scan = SC.scannable(["tools/sanitize-check.py", "tests/test_sanitize_check.py", "a.png"])
    check("引擎与测试文件都在扫描范围内", scan,
          ["tools/sanitize-check.py", "tests/test_sanitize_check.py"])

    print("\n9) 规则分层：个人规则来自本地文件，缺失时明确说「未校验」")
    check("通用规则不含个人姓名", any("真名" in r[0] for r in SC.GENERIC_RULES), False)
    check("本地规则缺失时返回空", SC.load_personal_rules(ROOT / "tools" / "__nope__.json"), [])
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "sanitize-check.py")],
                         cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    has_local = SC.PERSONAL_RULES_FILE.is_file()
    combined = out.stdout + out.stderr
    # 🔴 断言条数必须恒定，不能写成 if/else 各跑不同条数。
    #    私有仓有本地规则（38 项）、开源仓没有（37 项）→ run_all 的
    #    「少于基线即失败」会把开源仓判红。曾经就这么栽过一次。
    #    做法：条件放进**期望值**，条数不变。
    check("未校验提示的有无与本地规则一致",
          "未校验" in combined, not has_local)
    check("加载到的规则数与本地文件一致",
          data.get("personal_rules", 0) > 0, has_local)

    print("\n" + "-" * 46)
    print(f"通过 {TOTAL - FAILS} 项，失败 {FAILS} 项")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
