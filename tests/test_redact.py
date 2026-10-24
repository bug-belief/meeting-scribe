"""字段级脱敏自检：该抓的抓得到，不该动的别动。

这个测试有一半是**反向对照**——故意把 appId / 文件名 / 行内代码 / URL
喂进去，要求一处都不许改。没有这一半，「能抓手机号」可能只是靠一条
把所有长数字都吃掉的正则，那种实现会把技术文档削成筛子。

跑法（仓库根目录）：
    python tests/test_redact.py
不依赖 pytest，不碰真实 KB。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import redact as R  # noqa: E402

PASS = FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}：得到 {got!r}，期望 {want!r}")


def kinds(text: str) -> list[str]:
    return sorted({f["kind"] for f in R.scan(text)})


def sev(text: str, kind: str) -> str:
    for f in R.scan(text):
        if f["kind"] == kind:
            return f["severity"]
    return ""


# 校验位合法的身份证（用真实权重算出来的，不是抄的）
_ID_BODY = "11010119900307123"   # 17 位本体，校验位由下面算出来
_ID_TOTAL = sum(int(_ID_BODY[i]) * R._ID_WEIGHTS[i] for i in range(17))
ID_OK = _ID_BODY + R._ID_CHECK[_ID_TOTAL % 11]
ID_BAD_CHECK = _ID_BODY + ("0" if R._ID_CHECK[_ID_TOTAL % 11] != "0" else "1")


def main() -> int:
    print("\n【该抓的】")
    check("手机号", kinds("联系我 13812345678"), ["phone_cn"])
    check("手机号带分隔符", kinds("备用 139-0000-1111"), ["phone_cn"])
    check("手机号带 +86", kinds("境外拨 +86 138 1234 5678"), ["phone_cn"])
    check("身份证（校验位合法）", kinds(f"证件 {ID_OK}"), ["id_card"])
    check("身份证合法→high", sev(f"证件 {ID_OK}", "id_card"), "high")
    check("身份证校验位错→medium 待核", sev(f"证件 {ID_BAD_CHECK}", "id_card"), "medium")
    check("银行卡（有上下文词）", kinds("卡号 6222020200112345678 转账"), ["bank_card"])
    check("银行卡（62 开头过 Luhn）", kinds("尾号 6222020200112345678"), ["bank_card"])
    check("邮箱", kinds("发到 user@example.com"), ["email"])
    check("邮箱（多级域名）", kinds("a.b_c@mail.example.com"), ["email"])
    check("API 令牌", kinds("key: sk-abcdefghijklmnopqrstuvwx1234"), ["api_token"])
    check("GitHub 令牌", kinds("ghp_" + "a" * 36), ["api_token"])
    # 私钥块的样本要拼接：整行字面量会被 sanitize-check 的「SSH 密钥」规则拦。
    # 那条规则本身是对的（真私钥不该进仓库），这里只需要「长得像」，所以拼出来。
    check("私钥块", kinds("-----BEGIN RSA " + "PRIVATE KEY-----"), ["private_key"])
    check("车牌", kinds("车是鲁A12345"), ["plate_cn"])
    check("住址（只提醒）", kinds("住址：甲乙省丙丁市戊己县庚辛镇壬癸村123号"), ["address"])
    check("住址→low", sev("住址：甲乙省丙丁市戊己县庚辛镇壬癸村123号", "address"), "low")
    check("明文口令", kinds("password=Abc12345678"), ["password"])

    print("\n【不该动的：反向对照】")
    negatives = {
        "小程序 appId（16 位）": "appId 1000000000000001 已上线",
        "appId（17/16 位混排）": "10000000000000001 和 1000000000000002",
        "文件名里的 @": "改 README@2026.md 和 content@v2.yaml",
        "行内代码里的号码": "执行 `13812345678` 即可",
        "围栏代码块": "```\nphone = 13812345678\n```",
        "URL 里的号码": "见 https://x.com/u/13812345678",
        "Windows 路径里的号码": "E:\\projects\\13812345678\\a.md",
        "Unix 路径里的号码": "/home/alice/13812345678/a.wav",
        "18 位订单号（首位 1）": "订单号 138123456789012345",
        "10 位商户号": "商户号 1600000000",
        "纯日期": "20261019 与 19900307",
        "占位符": "WEB_TOKEN=xxxxxxxx",
        "普通金额": "报价 82512 元，管理费 8%",
    }
    for name, text in negatives.items():
        check(name, kinds(text), [])

    print("\n【掩码形状】")
    masked = {f["kind"]: f["masked"] for f in R.scan("联系 13812345678，证件 " + ID_OK)}
    check("手机号保留前3后4", masked.get("phone_cn"), "138****5678")
    check("身份证保留前6后4", masked.get("id_card"), ID_OK[:6] + "*" * 8 + ID_OK[-4:])
    check("掩码后不再命中手机号", kinds("联系 138****5678"), [])

    print("\n【apply_text】")
    text = "我电话 13812345678，身份证 %s，appId 1000000000000001" % ID_OK
    out, found = R.apply_text(text, "medium")
    check("medium 档替换手机号", "13812345678" not in out, True)
    check("medium 档替换身份证", ID_OK not in out, True)
    check("appId 原样保留", "1000000000000001" in out, True)
    check("命中条数", len(found), 2)
    out2, _ = R.apply_text(out, "medium")
    check("二次脱敏无变化（幂等）", out2, out)

    print("\n【warn 模式不改内容】")
    raw = "电话 13812345678"
    out_w, found_w = R.redact_text(raw, mode="warn")
    check("warn 原样返回", out_w, raw)
    check("warn 仍然报出来", len(found_w), 1)
    out_m, _ = R.redact_text(raw, mode="mask")
    check("mask 才改", out_m != raw, True)

    print("\n【low 档不高配套用】")
    out_low, _ = R.apply_text("住址：甲乙省丙丁市戊己县庚辛镇壬癸村123号", "medium")
    check("住址默认不改", "壬癸村" in out_low, True)

    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
