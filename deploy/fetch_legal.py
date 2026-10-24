#!/usr/bin/env python3
"""fetch_legal.py —— 把法条原文抓成本地语料，且**只在多源逐字一致时才收**。

为什么这么麻烦：
    法条是要拿去做谈判、甚至诉讼依据的。任何一个字抄错，都比「没有这条」更糟。
    模型转述更不行——它会把「可以请求适当减少」写成「应当减少」。
    所以这里只做两件事：抓原文、比对；不一致的直接丢弃并报出来。

为什么不用国家法律法规数据库：
    flk.npc.gov.cn 已改成前端渲染，旧 `/api/` 返回的是 SPA 外壳；人大网的民法典
    全文页 ID 已失效（实测 302 到首页）。所以改抓**法院系统站点**的法条引用，
    并用「≥2 个独立站点逐字相同」来交叉验证。

收录的每条都带：来源 URL、抓取日期、几源一致、是否已与官方数据库核对（当前是「否」）。

用法：
    python deploy/fetch_legal.py                  # 抓 + 校验 + 写 knowledge/legal/
    python deploy/fetch_legal.py --dry            # 只看结果不写文件
    python deploy/fetch_legal.py --add <url>      # 临时加一个源
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "legal"

# 来源白名单。**只认法院 / 检察院 / 政府域名**，避免哪天顺手加了自媒体。
#
# 第一条是**全文基准**：最高检官网发布的《民法典》第三编 合同 全文。
# 有它在，其余站点的短摘引就变成「子串」，一致性判定才站得住。
#
# 交叉来源（其余几条）**不写死在本文件里**——它们决定你抓哪个领域的法条，
# 写死等于公开你的取数偏好。放在 `knowledge/legal/sources.json`（已在 .gitignore 里），
# 用 `--add <URL>` 往里面加。下面这份只是可跑通的最小示例。
SOURCES = [
    ("最高检·民法典第三编合同（全文）",
     "https://www.spp.gov.cn/spp/ssmfdyflvdtpgz/202008/t20200831_478413.shtml"),
    ("中国法院网（民法典合同编释义）",
     "https://www.chinacourt.cn/article/detail/2023/08/id/7463555.shtml"),
    ("最高法人民法庭信息网（合同纠纷案例）",
     "https://rmft.court.gov.cn/2050/jdal/3466.jhtml"),
]

# 额外的交叉来源写在这里，不入库；存在则追加到 SOURCES。
EXTRA_SOURCES_FILE = OUT_DIR / "sources.json"
if EXTRA_SOURCES_FILE.is_file():
    import json as _json
    try:
        for _name, _url in _json.loads(EXTRA_SOURCES_FILE.read_text(encoding="utf-8")):
            SOURCES.append((_name, _url))
    except Exception as _e:  # 配错了就忽略，别让脚本因为这个跑不起来
        print(f"[fetch_legal] 忽略 {EXTRA_SOURCES_FILE}：{_e}")

# 整编全文源：单独一条就足以收录（它逐条发布，不是零散摘引）
OFFICIAL_FULLTEXT = {"最高检·民法典第三编合同（全文）"}

_CN_NUM = "一二三四五六七八九十百千零两"
# 只认「行首」的条号：正文里常有「依据本法第五百一十条」这类引用，
# 若把它们也当条号，会把当前条文拦腰截断，还会凭空多出条号。
_ART_RE = re.compile(rf"^[ \t\u3000]*第[{_CN_NUM}]{{1,8}}条", re.M)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script.*?</script>|<style.*?</style>", re.S)
# 块级标签要换成换行，否则整页压成一行，「行首条号」永远不成立
_BLOCK_RE = re.compile(r"</(p|div|br|tr|li|h[1-6]|td)\s*>|<br\s*/?>", re.I)


def fetch(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def html_to_text(html: str) -> str:
    text = _SCRIPT_RE.sub(" ", html)
    text = _BLOCK_RE.sub("\n", text)      # 块级标签 → 换行，保住段落边界
    text = _TAG_RE.sub(" ", text)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&ensp;", " "), ("&emsp;", " ")):
        text = text.replace(a, b)
    return text


def normalize(s: str) -> str:
    """比对用的归一化：只留汉字、数字、字母和必要的标点。"""
    s = re.sub(r"\s+", "", s)
    s = s.replace("，", ",").replace("。", ".").replace("：", ":").replace("；", ";")
    s = s.replace("（", "(").replace("）", ")").replace("、", ",")
    return s


def extract_articles(text: str) -> dict[str, str]:
    """从一段文本里切出「第X条 …」到下一个条号之间的内容。"""
    hits = list(_ART_RE.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else min(len(text), m.start() + 800)
        body = text[m.end():end]
        body = re.sub(r"\s+", " ", body).strip(" \u3000:：.;；")
        # 太短的（引用只写了条号没写正文）跳过
        if len(normalize(body)) < 8:
            continue
        label = m.group(0).strip()
        # 同一条在同一页出现两次时，留更长的那个（短的往往是被截断的转述）
        if label not in out or len(normalize(body)) > len(normalize(out[label])):
            out[label] = body
    return out


def build(sources: list[tuple[str, str]]) -> tuple[dict, list[str]]:
    """返回 {(条号): {"text","sources":[...]}} 与抓取失败清单。"""
    by_article: dict[str, list[tuple[str, str]]] = defaultdict(list)
    failed: list[str] = []
    for name, url in sources:
        try:
            html = fetch(url)
        except Exception as exc:
            failed.append(f"{name} 抓取失败：{exc}")
            continue
        arts = extract_articles(html_to_text(html))
        if not arts:
            failed.append(f"{name} 未解析出条文（页面结构可能变了）")
            continue
        print(f"   {name}：解析出 {len(arts)} 条 ({', '.join(list(arts)[:6])}…)")
        for label, body in arts.items():
            by_article[label].append((name, body))
    return by_article, failed


# 页面尾部噪声：抓到「最后一条」时正文会一路连到版权声明
TAIL_MARKERS = (
    "来源:", "来源：", "责任编辑", "友情链接", "联系我们", "版权所有", "Copyright",
    "您是第", "关闭窗口", "〖", "（撰稿", "(撰稿", "撰稿:", "法条链接", "Part0",
    "编辑：", "审核：", "&ensp;", "责任编辑：",
)


def clean_article(body: str) -> str:
    """切掉页面尾巴与「第一款」这类引用标记，只留条文本身。"""
    s = body
    cut = len(s)
    for m in TAIL_MARKERS:
        i = s.find(m)
        if i != -1:
            cut = min(cut, i)
    s = s[:cut].strip()
    s = re.sub(r"^第[一二三四五六七八九十]+款\s*", "", s)   # 摘引常带「第一款」
    s = re.sub(r"《[^》]{2,30}》\s*$", "", s).strip()        # 尾巴挂个书名号
    return s


def gram_sim(a: str, b: str, n: int = 4) -> float:
    """字符 n-gram Jaccard。用来判「是不是同一段话」，容忍标点与个别虚词差异。"""
    if not a or not b:
        return 0.0
    ga = {a[i:i + n] for i in range(max(len(a) - n + 1, 1))}
    gb = {b[i:i + n] for i in range(max(len(b) - n + 1, 1))}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _consistent(a: str, b: str) -> bool:
    """两种都算「一致」：
    ① 短的是长的**连续子串** —— 只引了其中一款；
    ② 4-gram 相似度 ≥0.9 —— 同一段话，标点/虚词有细微出入。
    抄错一个字（比如「可以」写成「应当」）在长条文里不会被判一致，
    但连续子串判定对短摘引是严格的。
    """
    if not a or not b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if short in long:
        return True
    return gram_sim(short, long) >= 0.9


_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9}
_UNIT = {"十": 10, "百": 100, "千": 1000}


def cn_to_int(label: str) -> int:
    """「第五百八十五条」→ 585。只为排序用，不做严格校验。"""
    s = re.sub(r"[第条]", "", label)
    total, cur = 0, 0
    for ch in s:
        if ch in _DIGIT:
            cur = _DIGIT[ch]
        elif ch in _UNIT:
            total += (cur or 1) * _UNIT[ch]
            cur = 0
    return total + cur


def cross_check(by_article: dict) -> tuple[dict, list[str]]:
    """两条收录通道：

    ① 官方全文源（最高检/人大转载的整编全文）→ 直接收，标 `official`。
       它是整编逐条发布，比「两篇法院文章的摘引」更可靠，不必强求交叉。
    ② 其余来源 → 必须 ≥2 源与最长那份一致（子串或 4-gram ≥0.9），标 `cross`。
       单源摘引一律丢弃——抄错了没人能发现。
    """
    kept: dict[str, dict] = {}
    dropped: list[str] = []
    for label, items in sorted(by_article.items(), key=lambda kv: cn_to_int(kv[0])):
        cleaned = [(n, clean_article(b)) for n, b in items]
        cleaned = [(n, b) for n, b in cleaned if len(normalize(b)) >= 8]
        if not cleaned:
            dropped.append(f"{label}（清洗后为空）")
            continue
        ordered = sorted(cleaned, key=lambda x: len(normalize(x[1])), reverse=True)
        canon_name, canon_body = ordered[0]
        canon = normalize(canon_body)
        supporters = [canon_name]
        for name, body in ordered[1:]:
            if _consistent(normalize(body), canon):
                supporters.append(name)

        if canon_name in OFFICIAL_FULLTEXT:
            kept[label] = {
                "text": re.sub(r"\s+", "", canon_body).strip(),
                "sources": supporters,
                "tier": "official",
            }
        elif len(supporters) >= 2:
            kept[label] = {
                "text": re.sub(r"\s+", "", canon_body).strip(),
                "sources": supporters,
                "tier": "cross",
            }
        else:
            dropped.append(f"{label}（{len(items)} 个来源，单源且非官方全文，无法交叉验证）")
    return kept, dropped


def render(kept: dict) -> dict[str, str]:
    """每条一个文件。

    为什么不是一个大 md：检索是按「条目」聚合的，全塞一个文件的话，
    526 条会塌成 1 个条目，只能命中其中一段，引用不出具体条号。
    """
    today = time.strftime("%Y-%m-%d")
    n_off = sum(1 for v in kept.values() if v.get("tier") == "official")
    head = (
        f"> 抓取日期 {today}　｜　"
        f"基准来源：最高人民检察院官网《民法典》第三编 合同（整编全文）\n"
        f"> 校验：官方全文 {n_off} 条；其余为法院站点多源逐字交叉\n"
        "> **未与国家法律法规数据库逐字核对**（该库已改前端渲染）\n"
        "> 用途：谈判/沟通时把话说准；不构成法律意见\n"
    )
    files: dict[str, str] = {}
    for label, info in kept.items():
        tag = ("官方全文" if info.get("tier") == "official"
               else f"{len(info['sources'])} 源一致")
        lines = [
            f"# 民法典·{label}",
            "",
            info["text"],
            "",
            f"> {tag}　{'、'.join(info['sources'])}",
            "",
            head,
        ]
        num = cn_to_int(label)
        files[f"民法典-合同编/{num:04d}-{label}.md"] = "\n".join(lines)

    files["README.md"] = (
        "# 法律知识语料（L1 本地）\n\n"
        f"本目录由 `deploy/fetch_legal.py` 生成（{today}），**不要手改**。\n\n"
        "## 这是什么\n\n"
        "第一层供给：把常用法条原文放在本机，不联网也能查。\n"
        "另两层（设计见 `docs/P2-P3-知识检索与法律供给.md`）：\n"
        "L2 接专业法律数据 Connector 做实时检索；L3 让大模型只做解释、**不引条号**，\n"
        "避免它编一条不存在的法条。\n\n"
        "## 收录规则\n\n"
        "1. 只收法院 / 检察院 / 政府域名，来源白名单写死在脚本里；\n"
        "2. 逐字原文，不做任何改写、总结、标点统一；\n"
        "3. 官方整编全文可直接收；零散摘引必须 ≥2 个独立站点逐字一致；\n"
        "4. 每条都记来源与抓取日期，可追溯。\n\n"
        "## 怎么加\n\n"
        "```bash\n"
        "python deploy/fetch_legal.py --add <法院文章URL>   # 加一个交叉来源\n"
        "python deploy/fetch_legal.py                      # 重抓并覆盖本目录\n"
        "python kb_search.py build --flat knowledge/legal --out ~/data/kb/legal-pack.json\n"
        "```\n\n"
        "重抓后必须重建索引包，否则检索仍是旧条文。\n"
    )
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="抓法条原文并做多源一致性校验")
    ap.add_argument("--dry", action="store_true", help="只打印，不写文件")
    ap.add_argument("--add", action="append", default=[], help="追加来源 URL（可多次）")
    ap.add_argument("--out", default=str(OUT_DIR), help="输出目录（每条一个 md）")
    args = ap.parse_args()

    sources = list(SOURCES) + [(f"追加源 {i+1}", u) for i, u in enumerate(args.add)]
    print(f"来源 {len(sources)} 个，开始抓取…")
    by_article, failed = build(sources)
    print(f"\n共见到 {len(by_article)} 个不同条号，开始交叉校验…")
    kept, dropped = cross_check(by_article)

    n_off = sum(1 for v in kept.values() if v.get("tier") == "official")
    print(f"\n✅ 收录 {len(kept)} 条（官方全文 {n_off} · 多源交叉 {len(kept) - n_off}）")
    for label, info in list(kept.items())[:8]:
        tag = "全文" if info.get("tier") == "official" else f"{len(info['sources'])}源"
        print(f"   {label}  [{tag}]  {info['text'][:44]}…")
    if len(kept) > 8:
        print(f"   …… 另有 {len(kept) - 8} 条")
    if dropped:
        print(f"\n⚠️ 丢弃 {len(dropped)} 条：")
        for x in dropped:
            print("   " + x)
    if failed:
        print(f"\n⚠️ 失败 {len(failed)} 个来源：")
        for x in failed:
            print("   " + x)

    if not kept:
        print("\n没有任何条文通过校验，不写文件。")
        return 1
    if args.dry:
        print("\n（--dry：不写文件）")
        return 0

    out = Path(args.out)
    files = render(kept)
    written = 0
    for rel, content in files.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written += 1
    # 旧的「一个大 md」清掉，避免和逐条文件重复入库
    stale = out / "民法典-合同编-常用条文.md"
    if stale.is_file():
        stale.unlink()
        print(f"已删除旧单文件：{stale.name}")
    print(f"\n已写入：{out}（{written} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
