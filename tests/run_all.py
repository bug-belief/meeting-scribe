#!/usr/bin/env python3
"""跑全量回归，输出一张统一汇总表。

    python tests/run_all.py              # 全量
    python tests/run_all.py --only kb    # 只跑名字含 kb 的
    python tests/run_all.py -q           # 安静模式，只在失败时打细节
    python tests/run_all.py --json       # 机器可读

为什么不直接 `for f in tests/test_*.py`：
  ① 各测试原来的汇总行有三种写法（`28 通过 / 0 失败`、`通过 24，失败 0`、
     `通过 42 项，失败 0 项`），用一条正则聚合必漏 → 2026-10-19 已统一成
     **`通过 N 项，失败 M 项`**，本脚本只认这一种；
  ② 光看退出码不够：测试里 if 分支被跳过时会「0 失败但只跑了 3 项」，
     本脚本拿 EXPECTED 基线比对，**通过数比基线少就判失败**（新增测试不算错）。

匹配不到汇总行 / 退出码非 0 / 通过数少于基线 / 失败数非 0 —— 任一命中即退出码 1。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# 基线：全绿时的通过数。合计 = 下面 EXPECTED 各项之和，**脚本实算，别手抄**
#   2026-10-23：test_routes 的汇总数原本写死 30（实际 45），改成实算后暴露出来，
#   基线 30 → 51（含新增的 6 项 /api/logout 断言）。
EXPECTED = {
    "test_assistant": 28,
    "test_live_echo": 10,
    "test_noise": 24,
    "test_dual": 8,
    "test_speakers": 18,
    "test_costs": 35,
    "test_prompter": 60,
    "test_websearch": 53,
    "test_llm_errors": 90,
    "test_voice_isolation": 21,
    "test_kb_search": 42,
    "test_redact": 42,
    "test_scenario": 26,
    "test_legal_pack": 61,
    "test_schema": 60,
    "test_version": 22,
    "test_deploy_units": 58,
    "test_cloud_asr": 44,
    "test_requirements": 20,
    "test_privacy_defaults": 20,
    "test_bootstrap": 49,
    "test_samples": 19,
    "test_install_units": 26,
    "test_asr_missing_hint": 31,
    "test_path_gate": 31,
    "test_sanitize_check": 38,
    "test_hardcode": 14,
    "test_repo_map": 22,
    "test_routes": 51,
    "test_session_context": 18,
    "test_live_retention": 39,
    "test_dual_naming": 24,
    "test_peer_channel": 16,
    "wake_decision": 16,
    "frontend_structure": 47,
    "dom_shadow": 8,
    "lib_format": 75,
    "lib_wake": 61,
    "frontend_boot": 78,
}

# 执行顺序：先快后慢，挂了能早停（--fail-fast 时有用）
ORDER = list(EXPECTED)

SUMMARY_RE = re.compile(r"通过 (\d+) 项，失败 (\d+) 项")


def suite_path(name: str) -> tuple[Path, list[str]]:
    """返回 (脚本路径, 命令行)。

    按「文件在不在」判断，而不是按名字前缀（原来只认 wake_ 开头，
    于是新增的 frontend_structure_test.js 被当成 .py 去找，报 missing）。
    """
    js = TESTS / f"{name}_test.js"
    if js.is_file():
        return js, ["node", str(js)]
    p = TESTS / f"{name}.py"
    return p, [sys.executable, str(p)]


def discover() -> list[str]:
    """自动发现套件：test_*.py + *_test.js（不含 emb_diag / selftest 这类诊断脚本）。

    已知的（EXPECTED 里有的）按 ORDER 排前面，新加的一律排后面 —— 新写测试不用改脚本。
    """
    found = [p.stem for p in sorted(TESTS.glob("test_*.py"))]
    found += [p.stem[:-5] for p in sorted(TESTS.glob("*_test.js")) if p.stem.endswith("_test")]
    known = [n for n in ORDER if n in found]
    extra = [n for n in found if n not in known]
    return known + extra


def run_one(name: str) -> dict:
    path, cmd = suite_path(name)
    if not path.exists():
        return {"name": name, "state": "missing", "passed": 0, "failed": 0,
                "expected": EXPECTED.get(name, 0), "secs": 0.0, "out": "", "notes": ["脚本不存在"]}

    t0 = time.time()
    try:
        # ⚠️ 全量回归**不该依赖外网**（离线机器 / CI 都要能跑）。
        # test_websearch 的第 11 节会真发请求验证端点，这里统一关掉。
        # 想连网测就单独跑：python tests/test_websearch.py
        env = {**os.environ, "SKIP_NET_TESTS": "1"}
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, env=env,
                              text=True, encoding="utf-8", errors="replace", timeout=900)
        out = (proc.stdout or "") + (proc.stderr or "")
        code = proc.returncode
    except subprocess.TimeoutExpired:
        return {"name": name, "state": "timeout", "passed": 0, "failed": 0,
                "expected": EXPECTED.get(name, 0), "secs": time.time() - t0,
                "out": "", "notes": ["超时 900s"]}
    secs = time.time() - t0

    m = None
    for m in SUMMARY_RE.finditer(out):
        pass  # 取最后一条：有些测试中间会打反向对照的小结
    notes: list[str] = []
    if m is None:
        notes.append("未匹配到汇总行（格式又变了？）")
        passed = failed = 0
        state = "no-summary"
    else:
        passed, failed = int(m.group(1)), int(m.group(2))
        state = "ok"
        if failed:
            notes.append(f"失败 {failed} 项")
            state = "fail"
        if code != 0 and not failed:
            notes.append(f"退出码 {code}（无失败项却非 0）")
            state = "fail"
        exp = EXPECTED.get(name)
        if exp is not None and passed < exp:
            notes.append(f"通过数 {passed} < 基线 {exp}，疑似有断言被跳过")
            state = "fail"

    return {"name": name, "state": state, "passed": passed, "failed": failed,
            "expected": EXPECTED.get(name, 0), "secs": secs, "out": out, "notes": notes}


def main() -> int:
    ap = argparse.ArgumentParser(description="跑全量回归并汇总")
    ap.add_argument("--only", default="", help="只跑名字含该子串的套件")
    ap.add_argument("-q", "--quiet", action="store_true", help="只在失败时打测试输出")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--fail-fast", action="store_true", help="第一个失败就停")
    args = ap.parse_args()

    names = [n for n in discover() if args.only in n]
    if not names:
        print(f"没有匹配 --only {args.only!r} 的套件", file=sys.stderr)
        return 2

    results = []
    for name in names:
        r = run_one(name)
        results.append(r)
        if not args.quiet or r["state"] != "ok":
            line = (f"{r['name']:<22}通过 {r['passed']:>4}  失败 {r['failed']:>3}"
                    f"  基线 {r['expected']:>3}  {r['secs']:>6.1f}s")
            print(line)
            if r["state"] != "ok":
                for n in r["notes"]:
                    print(f"    ! {n}")
                for ln in r["out"].splitlines():
                    if "✗" in ln:
                        print(f"    {ln.strip()}")
        if args.fail_fast and r["state"] != "ok":
            break

    tp = sum(r["passed"] for r in results)
    tf = sum(r["failed"] for r in results)
    te = sum(r["expected"] for r in results)
    bad = [r for r in results if r["state"] != "ok"]

    print("-" * 62)
    print(f"{'合计':<22}通过 {tp:>4}  失败 {tf:>3}  基线 {te:>3}"
          f"  {sum(r['secs'] for r in results):>6.1f}s")
    if bad:
        print("异常套件：" + "、".join(f"{r['name']}({r['state']})" for r in bad))
    print("结论：" + ("全部通过" if not bad and tf == 0 else "有问题，别放过"))

    if args.json:
        print(json.dumps({"passed": tp, "failed": tf, "expected": te,
                          "suites": [{k: v for k, v in r.items() if k != "out"} for r in results]},
                         ensure_ascii=False, indent=2))

    return 0 if (not bad and tf == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
