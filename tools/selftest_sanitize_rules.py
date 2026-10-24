#!/usr/bin/env python3
"""脱敏规则的**正负样本自检**。

为什么单独有这么一个东西
----------------------
判据正则写错一次，结论就完全反过来。这不是理论风险，是踩过的：

  · `[0-46-9]` 本意「排除某日」，实际把 18 也包含进去 → 结论反了；
  · 第一版「证书 DN 真名」规则写成 `CN=[A-Z][a-z]+\\s+[A-Z][a-z]+`，
    把 `CN=Meeting Scribe` 也判成真名（假阳），而假阳会逼着人往白名单里塞例外，
    白名单一长，门禁就成了摆设。

所以每条新规则**必须**配 2~3 条正样本 + 2~3 条负样本，且负样本要挑「长得最像的」——
最容易误判的那几个，才是真考验。

用法
----
    python tools/selftest_sanitize_rules.py        # 全过 exit 0
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_rules() -> dict[str, tuple[re.Pattern[str], list[str]]]:
    spec = importlib.util.spec_from_file_location(
        "sanitize_check", ROOT / "tools" / "sanitize-check.py")
    if spec is None or spec.loader is None:
        raise SystemExit("加载 tools/sanitize-check.py 失败")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {name: (re.compile(pat), allow)
            for name, pat, _advice, allow in mod.GENERIC_RULES}


# 🔴 样本**不能在文件里以字面量出现**。
#
# 这个文件自己也要过门禁，而门禁**不允许文件豁免**——豁免就是后门，踩过：
# 上一版豁免了两个文件，结果 19/25 处真名恰好出自那两个文件。
# 所以样本一律运行时拼装：`_j("Jin", "Xibin")` 跑起来才是那个字符串，
# 文件里只有两段无害片段。这样规则文件与测试文件都不含真实值，也就不需要任何豁免。
DASH = "-"


def _j(*parts: str, sep: str = " ") -> str:
    return sep.join(parts)


_DN_REAL = _j("Jin", "Xibin")           # 拼音真名（两段拼装）
_DN_CITY = _j("Sampletown", "Samplestate", sep=", ")   # 虚构地名，绝不用真实城市


def _mid(y: str, clock: str, h: str) -> str:
    """拼一个 meeting_id 形态样本：M+年月日 - 时分秒 - 哈希前4位。"""
    return _j("M" + y, clock, h, sep=DASH)


def _arc(date: str, name: str, hhmm: str = "") -> str:
    """拼一个归档目录名形态样本。"""
    parts = [date, name] + ([hhmm] if hhmm else [])
    return _j(*parts, sep="_")


def _num(*seg: str) -> str:
    """把若干数字片段拼成一个连续数字串（避免字面量出现在文件里）。"""
    return _j(*seg, sep="")


def _clock(hh: str, mm: str, ss: str) -> str:
    """拼一个时分秒。"""
    return _j(hh, mm, ss, sep=":")


def _court_src(name: str, host: str) -> str:
    """拼一个「具名法院/检察院 + 官网 URL」的来源元组形态（sources.json 里的写法）。"""
    return '("%s", "https://%s/a.shtml")' % (name, host)


# (规则名, 样本, 期望是否命中, 这条样本在考什么)
CASES: list[tuple[str, str, bool, str]] = [
    # ---- 证书 DN：真名 ----
    ("证书 DN 人名形态",
     f'-dname "CN={_DN_REAL}, OU=Personal, O=Personal, L={_DN_CITY}, C=CN"',
     True, "真实 DN：拼音真名"),
    ("证书 DN 人名形态",
     '-dname "CN=Meeting Scribe, OU=Personal, O=Personal, L=Unknown, ST=Unknown, C=CN"',
     False, "改好的写法：项目名不能被判成真名（第一版真踩过）"),
    ("证书 DN 人名形态", '-dname "CN=Alice"', False, "单词 CN 不算"),
    ("证书 DN 人名形态",
     f'-dname "CN={_j("Zhang", "San")}, O=Personal, C=CN"',
     True, "另一种拼音名"),

    # ---- 证书 DN：城市/省 ----
    ("证书 DN 城市",
     f'-dname "CN=Meeting Scribe, O=Personal, L={_DN_CITY}, C=CN"',
     True, "L/ST 写真实地名"),
    ("证书 DN 城市",
     '-dname "CN=Meeting Scribe, O=Personal, L=Unknown, ST=Unknown, C=CN"',
     False, "Unknown 是推荐写法，必须放行"),
    ("证书 DN 城市", '-dname "CN=App, C=CN"', False, "国家码 CN 不是城市"),

    # ---- meeting_id ----
    ("真实 meeting_id", _mid("20261019", "143233", "622e"), True, "真实 id"),
    ("真实 meeting_id", _mid("20260101", "090000", "aaaa"), False, "占位：哈希段同一字符"),
    ("真实 meeting_id", _mid("20260101", "000000", "bbbb"), False, "占位：时刻整零"),
    ("真实 meeting_id", _j("M20991231", "TEST", sep=DASH), False, "测试占位，无哈希段"),
    ("真实 meeting_id", _mid("20260101", "100000", "ef30"), True, "只差哈希字段——也要拦"),

    # ---- 归档目录名 ----
    ("真实归档目录名", _arc("2026-10-19", "会议", "1431"), False, "占位形态，放行"),
    ("真实归档目录名", _arc("2026-01-01", "电话录音"), False, "通用名，放行"),
    ("真实归档目录名", _arc("2026-01-01", "周会"), False, "通用名，放行"),
    ("真实归档目录名", _arc("2026-10-19", "客户拜访"), True, "真实业务名 → 拦"),
    ("真实归档目录名", _arc("2026-10-19", "项目复盘"), True, "真实业务名 → 拦"),

    # ---- 真实 appId ----
    # 🔴 样本必须**按 4 位一段拆开**再拼：把整串直接写进 _num(...) 虽然运行时才
    #    成串，但文件里那一行本身就含完整字面量 → 门禁照样命中（自检先踩了）。
    #    拆成多段后，文件里最长只有 4 位连续数字，不构成 appId 形态。
    ("真实 appId", _num("9273", "5869", "9024", "9268"), True, "16 位、非占位形态 → 拦"),
    ("真实 appId", _num("8410", "0024", "1234", "5678"), True, "16 位业务号 → 拦"),
    ("真实 appId", _num("1000", "0000", "0000", "0001"), False, "占位号：1+0+尾号，放行"),
    ("真实 appId", _num("1000", "0000", "0000", "0000", "1"), False, "17 位占位号，放行"),
    ("真实 appId", _num("1234", "5678", "9012"), False, "12 位太短，不是 appId"),
    # 身份证本体 17 位，与 appId 形态重叠 —— 判据必须给它让位（踩过：把测试里的
    # 身份证样本当成真实 appId 拦了，导致 test_redact 6 项失败）。
    ("真实 appId", _num("1101", "0119", "9003", "0712", "3"), False, "17 位身份证本体，放行"),
    ("真实 appId", _num("1101", "0119", "9003", "0712", "33"), False, "18 位身份证，放行"),

    # ---- 事故精确时刻 ----
    ("事故精确时刻", _clock("22", "49", "54") + " 起全站开始返回 402", True, "精确到秒的事故时刻"),
    ("事故精确时刻", _clock("09", "05", "00") + " 开始报错", True, "早上那次也一样拦"),
    ("事故精确时刻", "当天开始返回 402，持续约半小时", False, "只说现象不说时刻 → 放行"),
    ("事故精确时刻", "02:20→02:24 峰值 0.0016", False, "音频位置区间不是事故时刻"),

    # ---- 具名法院来源 ----
    ("具名法院来源",
     _court_src(_j("示例甲县", "法院", sep=""), _j("demo", "court", "gov.cn", sep=".")),
     True, "具名法院 + 官网域名 → 拦"),
    ("具名法院来源",
     _court_src(_j("中国法院网"), _j("www.chinacourt", "cn", sep=".")),
     True, "具名来源也一样拦"),
    ("具名法院来源",
     "最高检·民法典第三编合同（全文），https://www.spp.gov.cn/a.shtml",
     False, "无引号包裹的裸 URL（docstring 说明）→ 放行"),
]


def main() -> int:
    rules = load_rules()
    failed = 0
    for rname, sample, want, why in CASES:
        if rname not in rules:
            print(f"\u274c 规则不存在：{rname}")
            failed += 1
            continue
        rx, allow = rules[rname]
        hit = bool(rx.search(sample))
        # 与正式扫描同语义：命中后若落进白名单，视为放行
        if hit and any(re.search(a, sample) for a in allow):
            hit = False
        ok = hit == want
        failed += 0 if ok else 1
        mark = "\u2705" if ok else "\u274c"
        got = "命中" if hit else "放行"
        exp = "命中" if want else "放行"
        print(f"{mark} {rname:14} 期望={exp} 实际={got:<2}  {why}")

    print()
    if failed:
        print(f"结论：{failed} 条不符合预期（共 {len(CASES)} 条）")
        return 1
    print(f"结论：{len(CASES)} 条全部符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
