#!/usr/bin/env python3
"""脱敏门禁：仓库里不许再出现真实身份、基础设施坐标、真实案情。

## 为什么需要它，而不是靠人记得

这类泄漏**不会让任何测试失败**。改代码的人不会看到红灯，review 的人不会注意到
一行注释里的真名，而仓库一旦公开，这些字符串就永久留在 git 历史里
（`git log -p -S` 一搜就出来，删文件没用）。

所以必须有一条**机器判据**：出现就红。

## 与 tools/check-style.sh 的分工

| 门禁 | 管什么 | 例子 |
|---|---|---|
| `check-style.sh` ①-5 | 个人**路径** | `/home/someone/app` |
| `sanitize-check.py`（本文件） | 个人**身份 / 坐标 / 案情** | 真名、内网 IP、隧道域名、账号名、住址、案件细节 |

两者不重叠：路径是「能不能在别人机器上跑通」，身份是「该不该被别人看见」。

## 规则分两层：通用留在源码，个人放到本地文件

第一版把「要拦谁的名字」硬编码在源码里，然后用「本文件自我豁免」解决
「门禁拦自己」的问题。**这是错的**：豁免等于在门禁上开了后门，而门禁的源码
反而成了最集中的泄漏点 —— 实测开源仓里 19/25 处真名就出自那两个被豁免的文件。

现在改成：

| 层 | 内容 | 在哪 |
|---|---|---|
| 通用规则 | 个人邮箱域名 / 内网 IP / 隧道域名 | 本文件源码（不含任何个人信息） |
| 个人规则 | 真名 / 账号 / 住址 / 案情 | `tools/sanitize-rules.local.json`（被 .gitignore 挡住） |

于是**没有任何文件需要豁免**，门禁覆盖 100% 的文件。
`tools/sanitize-rules.example.json` 是空模板 + 写法示例，可以安全地公开。

本地规则文件不存在时会明确打印「个人规则未校验」，不静默放行 ——
否则换个克隆的人会以为「真名也查过了」，而实际只跑了 IP / 域名 / 邮箱三条。

## 白名单的粒度：按**字符串**放行，不按文件放行

这一点同样容易做错。Android 包名必须保留（包名是契约：改了老 APK 无法升级），
但如果按「文件」放行 `android/AndroidManifest.xml`，那个文件里将来加一行真名
就查不出来了。

所以做法是：**先把允许的模式从行里剔掉，再看剩下还有没有命中**。
包名那一行 → 剔掉包名 → 空 → 通过；同一个文件里再出现一个真实邮箱 → 剔不掉 → 报错。

## 用法

    python3 tools/sanitize-check.py              # 扫全部 git 跟踪文件（跳过 knowledge/legal）
    python3 tools/sanitize-check.py a.py b.md    # 只扫这些（pre-commit 用）
    python3 tools/sanitize-check.py --json       # 机器可读

退出码：0 干净 / 1 有命中 / 2 环境错误
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# 通用规则：不含任何个人信息，可以公开。
# 每条 = (名字, 正则, 说明, 允许的模式列表)
#
# ⚠️ 「允许的模式」只写**必须保留的契约**，不要为了消红往里塞东西 ——
#    白名单越长，这道门禁越像摆设。
# --------------------------------------------------------------------------- #
GENERIC_RULES: list[tuple[str, str, str, list[str]]] = [
    # ---- 身份：真实邮箱 ----
    ("个人邮箱", r"[A-Za-z0-9._%+-]+@(?:qq|foxmail|163|126|gmail)\.com",
     "换成 you@example.com", [r"noreply@", r"example\.com"]),

    # ---- 坐标：内网 / 公网 IP ----
    # 100.64/10 整段是 Tailscale 用的 CGNAT 段，其它私有段（192.168/16、10/8）一并挡。
    ("内网 IP", r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"
                r"|\b192\.168\.\d{1,3}\.\d{1,3}\b"
                r"|\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
     "换成 <你的内网IP>，或整行删掉（内网访问不是通用需求）", []),

    # ---- 凭据：SSH 公钥 / 私钥 ----
    # 公钥严格说不算秘密，但两个理由必须拦：
    #   ① 硬编码的公钥会被装到**别人的服务器**上（脚本里「复用现有公钥」的意图
    #      应当运行时去读，而不是写死当时读到的值）；
    #   ② 私钥整块出现 = 直接失守。
    # 这条是踩过之后补的：`server-setup.sh` 曾把一把真实公钥写死在常量里，
    # 陌生人跑脚本会往自己机器里装一把不属于他的钥匙。
    ("SSH 密钥", r"ssh-(?:rsa|ed25519|dss)\s+AAAA[A-Za-z0-9+/=]{20,}"
                 r"|-----BEGIN\s+(?:OPENSSH|RSA|EC|DSA|PGP)\s+PRIVATE KEY-----",
     "去掉硬编码；运行时从 authorized_keys 读，或用 --pubkey / 环境变量传入", []),

    # ---- 坐标：Funnel / 隧道域名 ----
    ("隧道域名", r"[a-z0-9-]+\.tail[a-z0-9]+\.ts\.net|\.ngrok\.(?:io|app)|\.trycloudflare\.com",
     "换成 <你的域名>", []),

    # ---- 身份：证书 DN 里的真名 / 城市 ----
    # 踩过之后补的（2026-10-25）：APK 签名证书的 `-dname` 里写着
    # `CN=真名拼音, L=真实城市, ST=省`，三道门禁全都扫不到 ——
    # 因为它长得像「一个普通的字符串参数」。而这串东西会**烧进每一个 APK**，
    # 任何人 apksigner verify --print-certs 都能读出来。
    # 拦「CN=两个拼音词」与「L=/ST= 具体值」，放行项目名与 Unknown/CN。
    # ⚠️ 判据要点：拼音名的特征是**两个短词**（3~9 字母），而项目名通常更长或带空格。
    #    第一版写成 `[A-Z][a-z]+\s+[A-Z][a-z]+` 会把 "CN=Meeting Scribe" 也判成真名 → 假阳。
    #    自检样本见 tools/selftest_sanitize_rules.py。
    ("证书 DN 人名形态", r"-dname\s+[\"'][^\"']*CN=(?!Meeting Scribe)[A-Z][a-z]{2,8}\s+[A-Z][a-z]{2,9}\b",
     'DN 里别写自己名字：改 -dname "CN=<项目名>, O=Personal, C=CN"', []),
    ("证书 DN 城市", r"-dname\s+[\"'][^\"']*(?:L|ST)=(?!Unknown|China|CN\b)[A-Z][a-z]{2,}",
     'DN 里别写住址：L/ST 填 Unknown', []),

    # ---- 数据：真实 meeting_id ----
    # 形如 M<年月日>-<时分秒>-<音频哈希前4位>，例如 M20260101-090000-ab12。
    # 它是**一段真实录音的指纹**，而且把时间定位到秒。
    # 示例一律用 M20260101-000000-xxxx 这种明显占位的形态。
    # ⚠️ 两处判据细节（都经样本自检修正）：
    #    ① 年份放宽到 20xx 全覆盖 —— 真实 id 本来就是 2026，不能靠年份区分；
    #    ② **不拦占位形态**：时刻 000000、或哈希段是一个字符重复 4 次（aaaa / xxxx）。
    #       "真 id" 的特征是「时刻不是整零」且「哈希段有变化」。
    #    ③ 占位白名单**显式列出**（aaaa/bbbb/cccc/ab12 等），不靠「哈希有没有变化」猜——
    #       猜法把占位样例 ab12 也判成真的了（2026-10-25 自检抓到）。
    ("真实 meeting_id",
     r"\bM20\d{6}-(?!000000)[0-2]\d[0-5]\d[0-5]\d-"
     r"(?!([0-9a-f])\1{3}\b)(?!ab12\b)[0-9a-f]{4}\b",
     "换成 M20260101-000000-xxxx（示例别用真实日期与哈希）",
     [r"M20260101-\d{6}-xxxx", r"M20260101-\d{6}-ab12"]),

    # ---- 数据：真实归档目录名 ----
    # 形如 2026-01-01_会议_0900 / 2026-01-01_电话录音。
    # 目录名会出现在文档命令、测试常量、注释里，拼起来就是使用时间线。
    ("真实归档目录名", r"\b20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])_(?!会议_\d{4}\b|电话录音|周会|xxx)[\u4e00-\u9fa5]{2,}",
     "示例目录名用 2026-01-01_会议_0900 这种明显占位的形态", []),

    # ---- 数据：真实小程序 / 公众号 appId ----
    # 16~17 位纯数字。测试样本里拿真 appId 当反例是最容易漏的一类：
    # 它长得就像「一串数字」，但拿去微信里一查就是具体那个小程序。
    # 占位写法：1000000000000001 这种「1 + 一串 0 + 尾号」。
    #    ⚠️ 17 位与「身份证 / 银行卡」形态重叠，必须让位：身份证本体就是 17 位。
    #       判据：占位号放行（1+一串0+尾号）；**首位是 1 且第二位是 1~5** = 身份证地区码，放行。
    ("真实 appId", r"\b(?!1[0-9]0{10,}\d\b)(?!1[1-5]\d{15}\b)\d{16,17}\b",
     "样本用 1000000000000001 这类占位号，别拿真实 appId", []),

    # ---- 数据：精确到秒的线上事故时刻 ----
    # 「某天 hh:mm:ss 起全站开始返回 402」——把「几点几分断了钱」公开了。
    # 事故描述保留，去掉时分秒（写 hh:mm:ss 占位，别填一个像真值的时刻）。
    ("事故精确时刻", r"(?:当天|凌晨|上午|下午|晚上|晚间)?\s*(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\s*(?:起|开始|突然)",
     "事故记录里去掉时分秒，只留「某次」", []),

    # ---- 数据：真实法院 / 检察院域名的具名来源 ----
    # 抓哪几家法院 = 你的取数偏好（往往对应你正在处理的案子）。
    # 交叉来源改为 knowledge/legal/sources.json（不入库）。
    # ⚠️ 判据要点（经样本自检修正）：
    #    ① 机构名后面**可能还有一个字**（「中国法院**网**」「XX法院**官网**」），
    #       第一版写 \s* 直接跳引号，全放过 → 假阴；
    #    ② 逗号与引号之间常有空格，所以中间要允许 [\s，,\"']{1,6} 而不是单个字符。
    #    ③ 域名段不能用 [a-z0-9.-]+ 再要求 \.court —— 贪婪会把 .gov.cn 一起吞掉，
    #       导致尾部分支永远不成立（自检抓到）。改成在主机名里直接找 court/jcy/sfy/spp 标签。
    ("具名法院来源",
     r"[\u4e00-\u9fa5]{2,8}(?:法院|检察院)[\u4e00-\u9fa5]{0,3}[\s，,\"']{1,6}"
     r"https?://[a-z0-9.-]*(?:court|jcy|sfy|spp)[.][a-z]{2,}",
     "交叉来源放 knowledge/legal/sources.json（已 gitignore），代码里只留通用示例", []),
]

# 个人规则文件：{"rules": [{name, pattern, advice, allow}]}，见 sanitize-rules.example.json
PERSONAL_RULES_FILE = ROOT / "tools" / "sanitize-rules.local.json"


def load_personal_rules(path: Path | None = None) -> list[tuple[str, str, str, list[str]]]:
    """读个人规则。文件不存在 → 空列表（调用方负责提示「未校验」）。

    为什么不放在源码里：检测个人信息的工具若把名字写死在源码，源码本身就是泄漏点。
    第一版就吃了这个亏 —— 门禁自身占了全部命中的 76%。
    """
    target = Path(path) if path is not None else PERSONAL_RULES_FILE
    if not target.is_file():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[sanitize] ⚠️ 个人规则文件解析失败（{exc}），本次只跑通用规则", file=sys.stderr)
        return []
    out: list[tuple[str, str, str, list[str]]] = []
    for r in data.get("rules") or []:
        if not isinstance(r, dict) or not r.get("pattern"):
            continue
        try:
            re.compile(r["pattern"])
        except re.error as exc:
            print(f"[sanitize] ⚠️ 规则 {r.get('name')!r} 正则无效（{exc}），已跳过", file=sys.stderr)
            continue
        out.append((str(r.get("name") or "未命名"),
                    str(r["pattern"]),
                    str(r.get("advice") or ""),
                    [str(a) for a in (r.get("allow") or [])]))
    return out


def all_rules() -> list[tuple[str, str, str, list[str]]]:
    return GENERIC_RULES + load_personal_rules()


# 与 check-style.sh 一致：这些是外部抓取的语料，不参与本仓门禁
SKIP_PREFIX = ("knowledge/legal/",)

# 二进制 / 生成物不扫（按扩展名）
SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".pdf",
    ".mp3", ".wav", ".m4a", ".mp4", ".zip", ".gz", ".tgz", ".whl",
    ".apk", ".jar", ".jks", ".pyc", ".so", ".dll", ".exe",
}

# 🔴 这里**不允许**出现「文件豁免」。
#    第一版曾豁免本文件与测试文件（理由是「要写出规则就得写名字」），结果那 19 处
#    真名成了开源仓里最集中的泄漏点。正确的解法是让规则外置，不是让门禁开洞。


def tracked_files() -> list[str]:
    """git 跟踪的文件。必须 -z + quotePath=false：否则中文名会被转义成八进制串。"""
    out = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "-z"],
        cwd=str(ROOT), capture_output=True,
    )
    if out.returncode != 0:
        print("git ls-files 失败（不在仓库里？）", file=sys.stderr)
        raise SystemExit(2)
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def scannable(files: list[str]) -> list[str]:
    keep = []
    for rel in files:
        if rel.startswith(SKIP_PREFIX):
            continue
        if Path(rel).suffix.lower() in SKIP_EXT:
            continue
        if (ROOT / rel).is_file():
            keep.append(rel)
    return keep


def scan_text(text: str, rules=None) -> list[tuple[str, str, str]]:
    """返回 [(规则名, 命中串, 说明)]。按行扫，逐条规则剔除白名单后再判。

    rules 为 None 时用 all_rules()（通用 + 本地个人规则）。
    测试会显式传自己的一组规则 —— 这样测试文件里不需要出现任何真实姓名。
    """
    if rules is None:
        rules = all_rules()
    hits: list[tuple[str, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pattern, advice, allow in rules:
            # 先把「必须保留的契约」剔掉，再看剩下有没有命中。
            # 这样白名单粒度是字符串，同一个文件里别的地方写脏了照样能抓到。
            probe = line
            for a in allow:
                probe = re.sub(a, "", probe)
            m = re.search(pattern, probe, re.I)
            if m:
                hits.append((name, f"{lineno}:{m.group(0)}", advice))
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="脱敏门禁：真实身份 / 坐标 / 案情不许进仓库")
    ap.add_argument("files", nargs="*", help="要扫的文件（默认扫全部 git 跟踪文件）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    files = args.files or tracked_files()
    files = scannable(files)

    # 规则只解析一次（个人规则要读盘），别丢进逐行循环里反复读
    personal = load_personal_rules()
    rules = GENERIC_RULES + personal

    problems: list[dict] = []
    for rel in files:
        try:
            text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name, where, advice in scan_text(text, rules):
            problems.append({"file": rel, "kind": name, "at": where, "advice": advice})

    if args.json:
        print(json.dumps({"scanned": len(files), "personal_rules": len(personal),
                          "problems": problems}, ensure_ascii=False, indent=2))
    else:
        print(f"[sanitize] 扫描 {len(files)} 个文件")
        # 🔴 没有个人规则时必须说出来。否则换个克隆的人以为「真名也查过了」，
        #    而实际只跑了 IP / 域名 / 邮箱这三条 —— 这正是上一版栽的坑。
        if not personal:
            print("[sanitize] · 未校验：个人规则（真名 / 账号 / 住址 / 案情）"
                  "—— 没有 tools/sanitize-rules.local.json")
            print("            想启用：cp tools/sanitize-rules.example.json "
                  "tools/sanitize-rules.local.json 后填入你的")
        if problems:
            print(f"[sanitize] ❌ 命中 {len(problems)} 处：", file=sys.stderr)
            for p in problems[:40]:
                print(f"  ✖ {p['file']}:{p['at']}  [{p['kind']}] {p['advice']}", file=sys.stderr)
            if len(problems) > 40:
                print(f"  … 还有 {len(problems) - 40} 处", file=sys.stderr)
        else:
            print("[sanitize] ✅ 干净")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
