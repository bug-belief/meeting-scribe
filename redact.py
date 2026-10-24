#!/usr/bin/env python3
"""redact.py —— 字段级脱敏（构建期兜底）

为什么需要它：条目级 `visibility` 管的是「这一条能不能给外人看」，
管不了「正文里随手写的一句手机号 / 一个身份证号」。索引包一旦被检索注入
到对话里，那串数字就顺着代答念出去了。

所以过滤仍然发生在**检索之前**——在索引包构建期替换掉，线上根本不存在明文。

为什么不纯用正则：
    KB 68 条实测，`\\d{16,19}` 命中 22 处**全是小程序 appId**，`@` 3 处
    **全是文件名**。纯正则脱敏会把技术文档削成筛子，还会漏掉真正的身份证
    （它长得跟 appId 一样长）。

所以每条规则都带「反误伤」条件：
    · 手机号 / 身份证 / 银行卡一律要求**两侧不是数字字母**（`(?<![0-9A-Za-z])`），
      长数字串里的 11 位片段不会命中；
    · 身份证必须过**出生日期 + 校验位**双重验证，随机 18 位数字过不了；
    · 银行卡必须过 **Luhn** 且**前方 16 字内出现「卡号 / 账号 / 转账」等词**；
    · 邮箱要求 TLD 不在文件后缀表里（挡掉 `a@b.md` 这类文件名）；
    · 代码块、行内代码、URL、文件路径整段豁免，不改。

两种模式：
    warn（默认）：只报告不动内容，先看误伤率；
    mask        ：按严重度替换，high 恒替换，medium 视 `--min-severity`。

用法：
    python redact.py audit                     # 体检整个 KB（只报告）
    python redact.py audit --min-severity medium
    python redact.py scan entries/xxx/yyy.md
    python redact.py mask <文件> -o <输出>      # 真正写脱敏后的副本
    python redact.py rules                     # 看规则表
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------- 严重度

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def _sev_ge(a: str, b: str) -> bool:
    return SEVERITY_ORDER.get(a, 0) >= SEVERITY_ORDER.get(b, 0)


# ---------------------------------------------------------------- 豁免区

# 这些区域里的东西不是「数据」，是「代码/地址/链接」，改了会破坏原文意思
_EXEMPT_PATTERNS = [
    re.compile(r"```.*?```", re.S),              # 围栏代码块
    re.compile(r"~~~.*?~~~", re.S),
    re.compile(r"`[^`\n]+`"),                    # 行内代码
    re.compile(r"https?://[^\s)\]<>\"']+"),      # URL
    re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+"),      # Windows 路径
    re.compile(r"(?<![\w.])~?/(?:[\w.\-]+/)+[\w.\-]+"),  # Unix 绝对路径
    re.compile(r"[\w.\-]+/[\w.\-/]+\.\w{1,5}"),  # 相对路径 a/b/c.md
]

# 文件名后缀：用来挡「a@b.md」被当成邮箱
_FILE_TLDS = {
    "md", "markdown", "txt", "py", "js", "mjs", "ts", "tsx", "jsx", "json",
    "yaml", "yml", "toml", "ini", "cfg", "conf", "env", "sh", "bash", "ps1",
    "java", "kt", "c", "h", "cpp", "go", "rs", "rb", "php", "sql",
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "tsv",
    "zip", "tar", "gz", "tgz", "7z", "rar", "jks", "keystore", "apk", "aab",
    "log", "tmp", "bak", "lock", "map", "wasm", "whl", "so", "dll", "exe",
    "css", "scss", "less", "html", "htm", "xml", "webmanifest",
}

# 占位符不算秘密（文档里大量出现 `WEB_TOKEN=xxx`）
_PLACEHOLDERS = {
    "xxx", "xxxx", "xxxxx", "xxxxxx", "your_token", "your-token", "changeme",
    "placeholder", "redacted", "todo", "none", "null", "empty", "***",
    "...", "<token>", "<your-token>", "your_key", "yourkey", "sk-xxx",
}

# 出现在银行卡号前面才算数的词
_CARD_CONTEXT = ("卡号", "银行卡", "储蓄卡", "信用卡", "借记卡", "账号", "帐号",
                 "账户", "帐户", "转账", "收款", "汇款", "尾号", "卡面上")

# 卡号首位（BIN 第一码）：3=Amex/JCB，4=Visa，5=Master，6=银联/Discover
_BIN_PREFIX = ("3", "4", "5", "6")

_EMAIL_OK_TLD_MIN = 2


def exempt_spans(text: str) -> list[tuple[int, int]]:
    """返回豁免区间（代码块 / 行内代码 / URL / 路径）。纯函数，可测。"""
    spans: list[tuple[int, int]] = []
    for pat in _EXEMPT_PATTERNS:
        for m in pat.finditer(text):
            spans.append((m.start(), m.end()))
    return _merge(spans)


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    out = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    for s, e in spans:
        if s <= pos < e:
            return True
        if pos < s:
            break
    return False


# ---------------------------------------------------------------- 校验器

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def valid_cn_id(s: str) -> bool:
    """18 位身份证：出生日期合法 + 校验位对得上。随机数字过不了这一关。"""
    if len(s) != 18 or not s[:17].isdigit():
        return False
    try:
        y, m, d = int(s[6:10]), int(s[10:12]), int(s[12:14])
        if not (1900 <= y <= 2100):
            return False
        date(y, m, d)
    except ValueError:
        return False
    total = sum(int(s[i]) * _ID_WEIGHTS[i] for i in range(17))
    return _ID_CHECK[total % 11] == s[17].upper()


def luhn_ok(s: str) -> bool:
    """银行卡校验位。测试卡号 6222020000000000000 之类能过，appId 过不了。"""
    if not s.isdigit() or not (16 <= len(s) <= 19):
        return False
    total = 0
    for i, ch in enumerate(reversed(s)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def valid_phone(s: str) -> bool:
    """中国大陆手机号：11 位、1 开头、第二位 3-9，且不能是 8 个以上同数字。"""
    digits = re.sub(r"\D", "", s)
    if digits.startswith("86"):
        digits = digits[2:]
    if not re.fullmatch(r"1[3-9]\d{9}", digits):
        return False
    return len(set(digits[1:])) > 2


# ---------------------------------------------------------------- 掩码

def mask_phone(s: str) -> str:
    d = re.sub(r"\D", "", s)
    if d.startswith("86"):
        d = d[2:]
    return f"{d[:3]}****{d[-4:]}"


def mask_id(s: str) -> str:
    return f"{s[:6]}{'*' * 8}{s[-4:]}"


def mask_card(s: str) -> str:
    d = re.sub(r"\D", "", s)
    return f"{d[:4]} **** **** {d[-4:]}"


def mask_email(s: str) -> str:
    local, _, domain = s.partition("@")
    keep = local[:1] if local else "*"
    return f"{keep}***@{domain}"


def mask_secret(s: str) -> str:
    s = s.strip().strip('"\'')
    return (s[:4] + "****") if len(s) > 8 else "****"


def mask_plate(s: str) -> str:
    """车牌保留省份简称 + 发牌机关字母（这两段本身不定位到人），后面全隐。"""
    return s[:2] + "*" * (len(s) - 3) + s[-1] if len(s) > 3 else "****"


MASKERS = {
    "mask_phone": mask_phone,
    "mask_id": mask_id,
    "mask_card": mask_card,
    "mask_email": mask_email,
    "mask_secret": mask_secret,
    "mask_plate": mask_plate,
}


# ---------------------------------------------------------------- 规则表

@dataclass
class Rule:
    kind: str
    severity: str
    pattern: re.Pattern
    label: str
    masker: str  # 掩码函数名


RULES: list[Rule] = [
    Rule("id_card", "high",
         re.compile(r"(?<![0-9A-Za-z])([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
                    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9A-Za-z])"),
         "身份证号", "mask_id"),
    Rule("bank_card", "high",
         re.compile(r"(?<![0-9A-Za-z])(\d{16,19})(?![0-9A-Za-z])"),
         "银行卡号（需 Luhn + 上下文词）", "mask_card"),
    Rule("private_key", "high",
         re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
         "私钥块", "mask_secret"),
    Rule("api_token", "high",
         re.compile(r"(?<![A-Za-z0-9])((?:sk|rk|pk)-[A-Za-z0-9_\-]{16,}"
                    r"|gh[pousr]_[A-Za-z0-9]{20,}"
                    r"|github_pat_[A-Za-z0-9_]{20,}"
                    r"|AKIA[0-9A-Z]{16}"
                    r"|xox[baprs]-[A-Za-z0-9\-]{10,})"),
         "API 令牌", "mask_secret"),
    Rule("password", "high",
         re.compile(r"(?i)(?:password|passwd|pwd|口令|密码|token|secret|api[_-]?key)"
                    r"\s*[:=：]\s*[\"']?([A-Za-z0-9_\-\.\+\$\!\@\#\%\{\}]{8,})"),
         "明文口令/密钥", "mask_secret"),
    Rule("phone_cn", "medium",
         # 允许 138-1234-5678 / 138 1234 5678 / +86 139... 这几种写法：
         # 只放行「单个分隔符 + 一位数字」，`1[3-9]\d{7}` 这种区间编号不会误判。
         re.compile(r"(?<![0-9A-Za-z])((?:\+?86[-\s]?)?1[3-9](?:[-\s]?\d){9})(?![0-9A-Za-z])"),
         "手机号", "mask_phone"),
    Rule("email", "medium",
         re.compile(r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9._%+\-]{1,64}@"
                    r"[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)"),
         "邮箱", "mask_email"),
    Rule("plate_cn", "medium",
         # 蓝牌「省+字母+5位」、绿牌 6 位。前后只挡字母数字：中文里车牌就是长在
         # 「车是鲁A12345号」这种句子中间的，挡中文字面量会一个都抓不到。
         re.compile(r"(?<![A-Za-z0-9])([京津沪渝冀豫云辽黑湘皖鲁新苏浙赣"
                    r"鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-HJ-NP-Z][A-HJ-NP-Z0-9]{5,6})"
                    r"(?![0-9A-Za-z])"),
         "车牌号", "mask_plate"),
    Rule("address", "low",
         re.compile(r"(?:家庭住址|现住址|收货地址|邮寄地址|详细地址|住址|住在)\s*[:：]?\s*"
                    r"([\u4e00-\u9fffA-Za-z0-9\-#号栋单元室路街道巷弄]{6,40})"),
         "详细住址（只提醒，不自动改）", None),
]

def _generic_mask(s: str) -> str:
    if len(s) <= 2:
        return "*" * len(s)
    return s[0] + "*" * (len(s) - 2) + s[-1]


# ---------------------------------------------------------------- 检测

def is_placeholder(v: str) -> bool:
    """占位符不算秘密：`xxxx` / `00000000` / `****` / `changeme` / `<your-token>`。

    文档里大量出现 `WEB_TOKEN=xxxxxxxx`，把它们当明文口令报出来只会制造噪声，
    真出事的时候反而淹没在假警报里。
    """
    s = v.strip().strip('"\'').strip()
    if not s:
        return True
    if s.lower() in _PLACEHOLDERS:
        return True
    if len(set(s)) == 1:            # 全同一个字符：xxx / 000 / ****
        return True
    if set(s) <= {"*", "x", "X", ".", "·", "-", "_"}:
        return True
    return False


def _valid(rule: Rule, value: str, text: str, start: int) -> bool:
    if rule.kind == "phone_cn":
        return valid_phone(value)
    if rule.kind == "email":
        tld = value.rsplit(".", 1)[-1].lower()
        if tld in _FILE_TLDS or len(tld) < _EMAIL_OK_TLD_MIN:
            return False
        return True
    if rule.kind in {"password", "api_token"}:
        return not is_placeholder(value)
    return True


def _judge(rule: Rule, value: str, text: str, start: int) -> str | None:
    """返回该处命中的**实际严重度**；None 表示不是。

    身份证和银行卡在这里做「分级」而不是「一票否决」：
      · 结构像身份证（第 7~14 位是合法的年月日）但校验位对不上 —— 多半是**打错了一位**
        的真实身份证，降为 medium 仍然脱敏，但要标出来让人去看；
      · 银行卡有「卡号 / 转账」这类上下文词时不再要求 Luhn —— 手抄错的卡号同样得挡。
    这两个降级正是为了不被 appId 带偏：appId 既不会有合法出生日期，也不会长在「卡号」后面。
    """
    if rule.kind == "id_card":
        if not valid_id_shape(value):
            return None
        return "high" if valid_cn_id(value) else "medium"
    if rule.kind == "bank_card":
        ctx_hit = any(k in text[max(0, start - 16):start] for k in _CARD_CONTEXT)
        # 发卡行首位固定是 3/4/5/6（62 银联最多）。开头的「1381…」是订单号不是卡号——
        # 只靠 Luhn 时 1/10 的随机长数字会撞上校验位，加这一条把误伤压下去。
        if luhn_ok(value) and value[:1] in _BIN_PREFIX:
            return "high"
        return "high" if ctx_hit else None
    return rule.severity if _valid(rule, value, text, start) else None


def valid_id_shape(s: str) -> bool:
    """只验形状：6 位地区码 + 合法出生日期。不验校验位（校验位交给 _judge 分档）。"""
    if len(s) != 18 or not s[:17].isdigit():
        return False
    try:
        y, m, d = int(s[6:10]), int(s[10:12]), int(s[12:14])
        if not (1900 <= y <= 2100):
            return False
        date(y, m, d)
    except ValueError:
        return False
    return True


def scan(text: str, min_severity: str = "low") -> list[dict]:
    """扫描一段文本，返回命中列表（不改内容）。

    每条：kind / severity / start / end / value(仅内存) / preview(可安全打印) / masked
    """
    if not text:
        return []
    exempt = exempt_spans(text)
    found: list[dict] = []
    for rule in RULES:
        if not _sev_ge(rule.severity, min_severity):
            continue
        for m in rule.pattern.finditer(text):
            raw = m.group(1) if m.groups() else m.group(0)
            # 用捕获组时，start 要对齐到组
            if m.groups():
                start = m.start(1)
            else:
                start = m.start()
            end = start + len(raw)
            if _inside(start, exempt) or _inside(max(start, end - 1), exempt):
                continue
            sev = _judge(rule, raw, text, start)
            if sev is None:
                continue
            masker = MASKERS.get(rule.masker or "")
            masked = masker(raw) if masker else _generic_mask(raw)
            found.append({
                "kind": rule.kind,
                "severity": sev,
                "label": rule.label if sev == rule.severity else rule.label + "（待核）",
                "start": start,
                "end": end,
                "value": raw,
                "masked": masked,
                "preview": masked,
            })
    return _resolve(found)


def _resolve(found: list[dict]) -> list[dict]:
    """去重叠：位置靠前、严重度高、长度长的优先。避免身份证又被当银行卡。"""
    found.sort(key=lambda f: (f["start"], -SEVERITY_ORDER.get(f["severity"], 0), -(f["end"] - f["start"])))
    kept: list[dict] = []
    for f in found:
        if kept and f["start"] < kept[-1]["end"]:
            continue
        kept.append(f)
    return kept


def apply_text(text: str, min_severity: str = "medium") -> tuple[str, list[dict]]:
    """返回 (脱敏后文本, 命中列表)。`low` 档（住址）默认不替换。"""
    found = scan(text, min_severity="low")
    todo = [f for f in found if _sev_ge(f["severity"], min_severity)]
    if not todo:
        return text, found
    out = text
    for f in reversed(todo):  # 从后往前换，前面的偏移量才不会失效
        out = out[:f["start"]] + f["masked"] + out[f["end"]:]
    return out, found


def redact_text(text: str, mode: str = "warn", min_severity: str = "medium") -> tuple[str, list[dict]]:
    """mode=warn 原样返回；mode=mask 替换。两种模式都返回命中列表。"""
    if mode == "mask":
        return apply_text(text, min_severity)
    found = scan(text, min_severity="low")
    return text, found


def summarize(findings: list[dict]) -> dict:
    out: dict[str, int] = {}
    for f in findings:
        out[f["kind"]] = out.get(f["kind"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------- 文件级

def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def scan_file(path: Path, min_severity: str = "low") -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [{"kind": "error", "severity": "low", "label": str(exc),
                 "start": 0, "end": 0, "value": "", "masked": "", "preview": ""}]
    rows = scan(text, min_severity)
    for r in rows:
        r["line"] = _line_of(text, r["start"])
        r["file"] = str(path)
    return rows


def iter_targets(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            out.extend(sorted(p.rglob("*.md")))
        elif p.is_file():
            out.append(p)
    return out


# ---------------------------------------------------------------- CLI

def cmd_scan(args) -> int:
    targets = iter_targets(args.paths)
    if not targets:
        print("没有可扫描的文件", file=sys.stderr)
        return 2
    total = 0
    by_kind: dict[str, int] = {}
    for p in targets:
        rows = [r for r in scan_file(p, args.min_severity) if r["kind"] != "error"]
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        total += len(rows)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
            continue
        if rows:
            print(f"\n{p}")
            for r in rows:
                print(f"  L{r['line']:<5} [{r['severity']:<6}] {r['label']:<12} → {r['preview']}")
    if not args.json:
        print(f"\n合计 {total} 处")
        if by_kind:
            print("按类型：" + "、".join(f"{k}={v}" for k, v in
                                       sorted(by_kind.items(), key=lambda kv: -kv[1])))
    return 0


def cmd_audit(args) -> int:
    root = Path(args.kb).expanduser()
    targets = iter_targets([str(root / "entries")] if (root / "entries").is_dir() else [str(root)])
    if not targets:
        print(f"没有可扫描的文件：{root}", file=sys.stderr)
        return 2
    total = 0
    by_kind: dict[str, int] = {}
    hot: list[tuple[str, int, list[dict]]] = []
    for p in targets:
        rows = [r for r in scan_file(p, args.min_severity) if r["kind"] != "error"]
        if not rows:
            continue
        total += len(rows)
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            rel = str(p)
        hot.append((rel, len(rows), rows))
    hot.sort(key=lambda x: -x[1])

    scope = "全部条目" if args.min_severity == "low" else f"≥{args.min_severity}"
    print(f"体检范围：{root}   规则档位：{scope}   文件 {len(targets)} 个")
    print(f"命中 {total} 处，涉及 {len(hot)} 个文件")
    if by_kind:
        print("按类型：" + "、".join(f"{k}={v}" for k, v in
                                   sorted(by_kind.items(), key=lambda kv: -kv[1])))
    for rel, n, rows in hot[: args.top]:
        print(f"\n{rel}   （{n} 处）")
        for r in rows[:8]:
            print(f"  L{r['line']:<5} [{r['severity']:<6}] {r['label']:<12} → {r['preview']}")
        if len(rows) > 8:
            print(f"  …还有 {len(rows) - 8} 处")
    if args.json:
        print(json.dumps([{"file": rel, "count": n, "hits": rows} for rel, n, rows in hot],
                         ensure_ascii=False, indent=2))
    return 0


def cmd_mask(args) -> int:
    src = Path(args.path).expanduser()
    text = src.read_text(encoding="utf-8", errors="replace")
    out_text, found = apply_text(text, args.min_severity)
    changed = sum(1 for f in found if _sev_ge(f["severity"], args.min_severity))
    dst = Path(args.out).expanduser() if args.out else src
    if args.dry_run:
        print(f"[dry-run] {src}：将替换 {changed} 处，未写盘")
    else:
        dst.write_text(out_text, encoding="utf-8")
        print(f"✅ {dst}：替换 {changed} 处（共扫出 {len(found)} 处）")
    return 0


def cmd_rules(args) -> int:
    print(f"{'类型':<14}{'严重度':<8}规则")
    for r in RULES:
        print(f"{r.kind:<14}{r.severity:<8}{r.label}")
        print(f"{'':<22}{r.pattern.pattern[:78]}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="字段级脱敏")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("audit", help="体检整个 KB（只报告）")
    p.add_argument("--kb", default=str(Path(__file__).resolve().parent.parent))
    p.add_argument("--min-severity", choices=list(SEVERITY_ORDER), default="low")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("scan", help="扫描指定文件/目录")
    p.add_argument("paths", nargs="+")
    p.add_argument("--min-severity", choices=list(SEVERITY_ORDER), default="low")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("mask", help="生成脱敏副本")
    p.add_argument("path")
    p.add_argument("-o", "--out")
    p.add_argument("--min-severity", choices=list(SEVERITY_ORDER), default="medium")
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("rules", help="列出规则表")

    args = ap.parse_args(argv)
    return {"audit": cmd_audit, "scan": cmd_scan, "mask": cmd_mask,
            "rules": cmd_rules}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
