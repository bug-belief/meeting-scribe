"""把两份外部法律语料合并成一个 legal-pack.json，供 `kb_search` 直接检索。

    python deploy/build_legal_pack.py \
        --a-db ~/data/law-src/a-laws.db \
        --b-db ~/data/law-src/b-database.db \
        --out  ~/data/kb/legal-pack.json

为什么要两份
------------
- **B（Ansvar chinese-law-mcp）**：1,188 部 / 62,981 条，含 **611 部行政法规**，
  条文从 flk.npc.gov.cn 的官方 DOCX 提取，零 LLM 生成。这是主库。
- **A（thu-lawyer china-law-mcp）**：378 部 / 26,196 条，只收宪法·法律·立法解释，
  但它的**条级时效**更细（现行有效/历史版本/已废止/已被修改），且含
  《生态环境法典》《宪法》《社会救助法》等 B 没收录的新法。

实测重叠率 91.0%，A 真正独有的只有 2,352 条 —— 所以合并的意义不是「更多」，
而是「B 的行政法规 + A 的立法解释与新法」。

去重规则（关键）
----------------
1. **B 内部**同一部法律常有多条 document 记录（`amended` / `in_force` /
   `not_yet_in_force`），每个版本各带一套完整条文。**先按标题选一版**，
   否则《民用航空法》第 1 条会进库 4 次（实测 62,981 条里近 1.7 万条是这种重复）。
   选优顺序：`in_force` > `not_yet_in_force` > `amended`。
2. **A 与 B 之间**按「归一化法律名 + 条号」去重，**B 优先**（官方 DOCX 更权威）。
   归一化只削掉开头的「中华人民共和国」和空白，不做简称映射 —— 简称映射
   （「民法典」↔「中华人民共和国民法典」）靠猜会误合并，宁可有极少量重复。

出处与许可
----------
本文件的**去重规则**参考了 A 项目的实现思路（选版本优先级、按「归一化法律名 + 条号」
合并），按 MIT 许可借用，在此注明：

- A: thu-lawyer/china-law-mcp — MIT License
  https://github.com/thu-lawyer/china-law-mcp
- B: Ansvar-Systems/chinese-law-mcp — Apache-2.0
  https://github.com/Ansvar-Systems/chinese-law-mcp
  条文源自 flk.npc.gov.cn / cac.gov.cn 的政府公开信息

两家都不对本工具产出的内容作任何担保；法条以官方公布文本为准，
本工具**不构成法律意见**。

输出
----
条目 id 用 `法律简称/条号`（如 `民法典/586`），title 用 `民法典·第五百八十六条`，
与线上已有的 526 条格式保持一致，替换后前端与代答都无感。

每条额外带 `status` / `law` / `article` / `doc_type` / `chapter` / `source`，
检索层可按时效过滤，代答时也能看出「这条是现行还是废止」。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 只取这两个常量，避免 import 整个 kb_search 时带出别的副作用
try:
    from kb_search import ALL_AUDIENCES
except Exception:  # pragma: no cover - 兜底，脚本可独立运行
    ALL_AUDIENCES = ["self", "friend", "client", "recruiter", "public"]

# B 的 document.status → 中文；A 本来就是中文
B_STATUS_CN = {
    "in_force": "现行有效",
    "not_yet_in_force": "尚未生效",
    "amended": "已被修改",
    "repealed": "已废止",
}
B_TYPE_CN = {
    "statute": "法律",
    "administrative_regulation": "行政法规",
    "departmental_rule": "部门规章",
    "judicial_interpretation": "司法解释",
}
# 选优顺序：现行 > 未生效的新法 > 历史修订版
B_STATUS_RANK = {"in_force": 0, "not_yet_in_force": 1, "amended": 2}

PREFIX_RE = re.compile(r"^中华人民共和国")
_DIGITS = "零一二三四五六七八九"


def num_to_cn(n: int) -> str:
    """条号转中文，按法条习惯：第10条写「十」不是「一十」，第101条写「一百零一」。"""
    n = int(n)
    if n <= 0:
        return str(n)
    if n < 10:
        return _DIGITS[n]
    if n < 20:
        return "十" + (_DIGITS[n - 10] if n > 10 else "")
    out: list[str] = []
    zero = False
    for unit, name in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        d = (n // unit) % 10
        if d:
            if zero and out:
                out.append("零")
                zero = False
            out.append(_DIGITS[d] + name)
        elif out and unit > 1:
            zero = True
    return "".join(out)


def norm_law(name: str) -> str:
    """归一化法律名：去前缀、去空白。不做简称映射（猜错的代价比重复大）。"""
    s = (name or "").strip().replace("　", "").replace(" ", "")
    return PREFIX_RE.sub("", s)


def short_name(name: str) -> str:
    """展示用简称：民法典、道路交通安全法。"""
    return norm_law(name)


def load_b(path: Path) -> tuple[list[dict], dict]:
    """读 B：先按标题选一版 document，再取其全部条文。"""
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row

    best: dict[str, sqlite3.Row] = {}
    for r in con.execute(
        "select id, title, type, status, issued_date from legal_documents "
        "order by title, issued_date desc"
    ):
        cur = best.get(r["title"])
        if cur is None or B_STATUS_RANK.get(r["status"], 9) < B_STATUS_RANK.get(cur["status"], 9):
            best[r["title"]] = r

    ids = [r["id"] for r in best.values()]
    if not ids:
        raise SystemExit(f"{path} 里没有 legal_documents 记录")
    marks = ",".join("?" * len(ids))
    rows = con.execute(
        "select p.document_id, p.provision_ref, p.chapter, p.section, p.content "
        f"from legal_provisions p where p.document_id in ({marks}) "
        "and p.language = 'zh' order by p.document_id, cast(p.provision_ref as integer)",
        ids,
    ).fetchall()

    by_doc = {}
    for r in rows:
        by_doc.setdefault(r["document_id"], []).append(r)

    out: list[dict] = []
    seen_versions: dict[str, int] = {}
    for title, doc in best.items():
        refs = by_doc.get(doc["id"]) or []
        if not refs:
            continue
        seen_versions[title] = len(refs)
        for r in refs:
            ref = str(r["provision_ref"]).strip()
            if not ref.isdigit():
                continue
            body = (r["content"] or "").strip()
            if not body:
                continue
            out.append({
                "law_full": title,
                "law": short_name(title),
                "article": int(ref),
                "text": body,
                "status": B_STATUS_CN.get(doc["status"], doc["status"] or ""),
                "doc_type": B_TYPE_CN.get(doc["type"], doc["type"] or ""),
                "chapter": (r["chapter"] or "").strip(),
                "source": "B",
            })
    con.close()
    stats = {
        "documents": len(best),
        "articles": len(out),
        "titles_with_versions": sum(1 for v in seen_versions.values() if v),
    }
    return out, stats


def load_a(path: Path) -> tuple[list[dict], dict]:
    """读 A：条级时效已是最细，原样保留。"""
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    out: list[dict] = []
    for r in con.execute(
        "select law_name, article_no_arabic, chapter, status, level, article_text "
        "from articles order by law_name, article_no_arabic"
    ):
        body = (r["article_text"] or "").strip()
        if not body:
            continue
        try:
            no = int(str(r["article_no_arabic"]).strip())
        except (TypeError, ValueError):
            continue
        out.append({
            "law_full": r["law_name"],
            "law": short_name(r["law_name"]),
            "article": no,
            "text": body,
            "status": (r["status"] or "").strip(),
            "doc_type": (r["level"] or "").strip(),
            "chapter": (r["chapter"] or "").strip(),
            "source": "A",
        })
    con.close()
    return out, {"articles": len(out)}


def dedupe(b_rows: list[dict], a_rows: list[dict]) -> tuple[list[dict], dict]:
    """B 优先；A 只补 B 没有的 (法律, 条号)。"""
    merged: list[dict] = []
    seen: set[tuple[str, int]] = set()

    for r in b_rows:
        key = (r["law"], r["article"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(r)

    b_keys = len(seen)
    a_added = a_dup = 0
    for r in a_rows:
        key = (r["law"], r["article"])
        if key in seen:
            a_dup += 1
            continue
        seen.add(key)
        merged.append(r)
        a_added += 1

    merged.sort(key=lambda x: (x["law"], x["article"], x["status"]))
    return merged, {
        "b_unique": b_keys,
        "a_duplicate": a_dup,
        "a_added": a_added,
        "total": len(merged),
    }


def to_pack(rows: list[dict], stats: dict) -> dict:
    entries: list[dict] = []
    chunks: list[dict] = []
    used_ids: dict[str, int] = {}

    for r in rows:
        base = f"{r['law']}/{r['article']}"
        eid = base
        if eid in used_ids:
            # 同一法律同一条号的多个版本（历史版本/已废止）留不同 id
            used_ids[base] += 1
            eid = f"{base}#{r['status'] or used_ids[base]}"
        else:
            used_ids[base] = 1

        heading_law = r["law"]
        chapter = r["chapter"].replace(" > ", " · ")
        heading = f"{heading_law} · {chapter}" if chapter else heading_law

        entries.append({
            "id": eid,
            "title": f"{heading_law}·第{num_to_cn(r['article'])}条",
            "type": "legal",
            "tags": [r["doc_type"]] if r["doc_type"] else [],
            "keywords": [],
            "visibility": "public",
            "scopes": ["legal"],
            "audiences": list(ALL_AUDIENCES),
            "importance": None,
            "summary": "",
            "path": "",
            "status": r["status"],
            "law": r.get("law_full") or r["law"],
            "article": r["article"],
            "doc_type": r["doc_type"],
            "chapter": r["chapter"],
            "source": r["source"],
        })
        chunks.append({"eid": eid, "heading": heading, "text": r["text"]})

    return {
        "version": 1,
        "corpus": "legal",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(entries),
        "skipped": [],
        "sources": stats,
        "entries": entries,
        "chunks": chunks,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="合并 A/B 两份法律语料 → legal-pack.json")
    ap.add_argument("--a-db", required=True, help="china-law-mcp 的 laws.db")
    ap.add_argument("--b-db", required=True, help="chinese-law-mcp 的 database.db")
    ap.add_argument("--out", required=True, help="输出 legal-pack.json")
    ap.add_argument("--only-status", default="",
                    help="只收这些状态，逗号分隔（默认全收），如「现行有效,尚未生效」")
    args = ap.parse_args()

    a_path, b_path = Path(args.a_db).expanduser(), Path(args.b_db).expanduser()
    for p in (a_path, b_path):
        if not p.is_file():
            raise SystemExit(f"找不到 {p}")

    t0 = time.time()
    b_rows, b_stats = load_b(b_path)
    a_rows, a_stats = load_a(a_path)
    kept = [s.strip() for s in args.only_status.split(",") if s.strip()]
    if kept:
        b_rows = [r for r in b_rows if r["status"] in kept]
        a_rows = [r for r in a_rows if r["status"] in kept]

    merged, d_stats = dedupe(b_rows, a_rows)
    pack = to_pack(merged, {"b": b_stats, "a": a_stats, "dedupe": d_stats})

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pack, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    size = out.stat().st_size
    by_source: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for e in pack["entries"]:
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1
        by_type[e["doc_type"]] = by_type.get(e["doc_type"], 0) + 1

    print(f"输出   {out}  {size / 1048576:.1f} MB")
    print(f"条目   {pack['count']:,} 条 / {len(pack['chunks']):,} 段")
    print(f"耗时   {time.time() - t0:.1f}s")
    print(f"来源   {by_source}")
    print(f"效力   {dict(sorted(by_status.items(), key=lambda x: -x[1]))}")
    print(f"类型   {dict(sorted(by_type.items(), key=lambda x: -x[1]))}")
    print(f"去重   B 唯一 {d_stats['b_unique']:,} · A 重复 {d_stats['a_duplicate']:,} · A 补入 {d_stats['a_added']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
