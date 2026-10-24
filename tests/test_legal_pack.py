"""法律语料层自检：条号转换、两库去重、口语扩展、重排。

三条主线，每条都带**反向对照**（把护栏拆掉，证明测试真的会红）：

1. `num_to_cn` —— 法条写法有坑：「第十条」不是「第一十条」，「第一百零一条」要带零。
2. 两库合并 —— B 内部同一部法律有多个版本记录，不选版本会把《民用航空法》第 1 条
   收 4 次；A 与 B 重叠时以 B（官方 DOCX）为准。
3. 检索 —— BM25 必须**求和**不是取最大；口语必须扩展，否则「假货」召不回写着
   「欺诈」的条文。

跑法（仓库根目录）：
    python tests/test_legal_pack.py
不依赖 pytest，不碰线上包（临时目录现造 SQLite 与索引包）。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy"))

from build_legal_pack import (  # noqa: E402
    dedupe,
    load_a,
    load_b,
    norm_law,
    num_to_cn,
    to_pack,
)

import kb_search as K  # noqa: E402

PASS = FAIL = 0


def ok(name: str, cond, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"：{detail}" if detail else ""))


def eq(name: str, got, want) -> None:
    ok(name, got == want, f"得到 {got!r}，期望 {want!r}")


# --------------------------------------------------------------- 1. 条号

def test_num_to_cn() -> None:
    print("\n[1] 条号转中文（法条写法）")
    cases = {
        1: "一", 9: "九", 10: "十", 11: "十一", 19: "十九", 20: "二十",
        21: "二十一", 99: "九十九", 100: "一百", 101: "一百零一", 110: "一百一十",
        111: "一百一十一", 120: "一百二十", 200: "二百", 999: "九百九十九",
        1000: "一千", 1001: "一千零一", 1010: "一千零一十", 1100: "一千一百",
        1260: "一千二百六十",
    }
    for n, want in cases.items():
        eq(f"{n} → 第{want}条", num_to_cn(n), want)

    # 反向对照：最容易写错的两种，确认不是「碰巧对」
    ok("10 不是「一十」", num_to_cn(10) != "一十")
    ok("101 必须带零", "零" in num_to_cn(101))
    ok("1260 不带多余的零", num_to_cn(1260) == "一千二百六十")


def test_norm_law() -> None:
    print("\n[2] 法律名归一化")
    eq("去中华人民共和国前缀", norm_law("中华人民共和国民法典"), "民法典")
    eq("去首尾空白", norm_law("  中华人民共和国道路交通安全法 "), "道路交通安全法")
    eq("本来就短的不动", norm_law("民法典"), "民法典")
    # 不做简称映射：猜错会把两部法合并成一部
    ok("「民法典」与「民法」不互相映射", norm_law("民法") == "民法")


# --------------------------------------------------------------- 2. 合并

def make_dbs(tmp: Path) -> tuple[Path, Path]:
    """造一对最小 A/B 库，覆盖「B 内部多版本」和「A 补 B 没有」两种情形。"""
    a = tmp / "a.db"
    con = sqlite3.connect(str(a))
    con.executescript("""
        create table articles(id integer primary key, law_id text, law_name text,
            law_department text, level text, status text, chapter text, section text,
            article_no text, article_no_arabic integer, article_text text, char_count integer);
    """)
    con.executemany(
        "insert into articles(law_name, article_no_arabic, chapter, status, level, article_text) "
        "values (?,?,?,?,?,?)",
        [
            ("中华人民共和国民法典", 586, "第八章 违约责任", "现行有效", "法律", "定金不得超过百分之二十。"),
            # 只在 A 里有：B 未收录的新法
            ("中华人民共和国生态环境法典", 1, "第一编 总则", "现行有效", "法律", "为了保护生态环境，制定本法。"),
        ],
    )
    con.commit()
    con.close()

    b = tmp / "b.db"
    con = sqlite3.connect(str(b))
    con.executescript("""
        create table legal_documents(id text primary key, title text, type text,
            status text, issued_date text);
        create table legal_provisions(id integer primary key, document_id text,
            provision_ref text, chapter text, section text, title text, content text,
            language text, metadata text);
    """)
    con.executemany(
        "insert into legal_documents(id,title,type,status,issued_date) values (?,?,?,?,?)",
        [
            ("old", "中华人民共和国民法典", "statute", "amended", "2015-01-01"),
            ("cur", "中华人民共和国民法典", "statute", "in_force", "2020-05-28"),
            ("adm", "机动车交通事故责任强制保险条例", "administrative_regulation", "in_force", "2020-01-01"),
        ],
    )
    con.executemany(
        "insert into legal_provisions(document_id,provision_ref,chapter,content,language) "
        "values (?,?,?,?,?)",
        [
            ("old", "586", "第八章", "旧版本定金条文（不该进库）。", "zh"),
            ("cur", "586", "第八章 违约责任", "定金不得超过主合同标的额的百分之二十。", "zh"),
            ("cur", "587", "第八章 违约责任", "收受定金的一方不履行债务的，应当双倍返还定金。", "zh"),
            ("adm", "5", "第一章 总则", "交强险实行统一的责任限额。", "zh"),
        ],
    )
    con.commit()
    con.close()
    return a, b


def test_merge(tmp: Path) -> None:
    print("\n[3] 两库合并与去重")
    a_db, b_db = make_dbs(tmp)

    b_rows, _ = load_b(b_db)
    a_rows, _ = load_a(a_db)

    # B 内部：同一部法律两条记录，只该取 in_force 那条
    eq("B 只收现行版本（不重复收 amended）", len(b_rows), 3)
    texts = [r["text"] for r in b_rows]
    ok("B 用的是 in_force 正文", any("主合同标的额" in t for t in texts))
    ok("B 没混进 amended 正文", not any("旧版本" in t for t in texts))

    merged, stats = dedupe(b_rows, a_rows)
    eq("B 优先：民法典 586 只有一条", sum(1 for r in merged if r["law"] == "民法典" and r["article"] == 586), 1)
    eq("A 独有条目补进来", sum(1 for r in merged if r["law"] == "生态环境法典"), 1)
    eq("A 重复的被丢掉", stats["a_duplicate"], 1)
    eq("A 补入 1 条", stats["a_added"], 1)

    # 反向对照：B 已经覆盖的，A 不能顶掉它
    m586 = [r for r in merged if r["law"] == "民法典" and r["article"] == 586][0]
    eq("民法典 586 来源是 B", m586["source"], "B")

    pack = to_pack(merged, {})
    eq("条目数 = 段数", len(pack["entries"]), len(pack["chunks"]))
    ids = [e["id"] for e in pack["entries"]]
    eq("id 不重复", len(ids), len(set(ids)))
    titles = [e["title"] for e in pack["entries"]]
    ok("title 用中文条号", "民法典·第五百八十六条" in titles, str(titles[:3]))
    ok("行政法规也进来了", any(e["doc_type"] == "行政法规" for e in pack["entries"]))
    ok("带时效字段", all("status" in e for e in pack["entries"]))
    ok("法律条文对谁都可见", all("public" in e["audiences"] for e in pack["entries"]))


# --------------------------------------------------------------- 3. 检索

def make_pack() -> dict:
    """造一个能复现线上真实毛病的小法律包。

    民法典 585 含「违约金」；刑法条文含「不能少于」；消保法 55 含「欺诈」。
    查询「买到假货能退一赔三吗」时，只有扩展才能召回到消保法。
    """
    rows = [
        {"law": "民法典", "article": 585, "status": "现行有效", "doc_type": "法律",
         "chapter": "第八章 违约责任", "source": "B",
         "text": "当事人可以约定一方违约时向对方支付违约金。约定的违约金过分高于造成的损失的，可以请求适当减少。"},
        {"law": "刑法", "article": 78, "status": "现行有效", "doc_type": "法律",
         "chapter": "第三章 刑罚", "source": "B",
         "text": "减刑以后实际执行的刑期不能少于下列期限：判处管制、拘役、有期徒刑的，不能少于原判刑期的二分之一。"},
        {"law": "消费者权益保护法", "article": 55, "status": "现行有效", "doc_type": "法律",
         "chapter": "第七章 法律责任", "source": "B",
         "text": "经营者提供商品或者服务有欺诈行为的，应当按照消费者的要求增加赔偿其受到的损失，增加赔偿的金额为消费者购买商品的价款的三倍。"},
        {"law": "食品安全法", "article": 148, "status": "现行有效", "doc_type": "法律",
         "chapter": "第十章 法律责任", "source": "B",
         "text": "生产不符合食品安全标准的食品或者经营明知是不符合食品安全标准的食品，消费者可以要求赔偿损失。"},
    ]
    return to_pack(rows, {})


def titles(rows: list[dict]) -> list[str]:
    return [r["title"].split("·")[0] for r in rows]


def test_expand() -> None:
    print("\n[4] 口语 → 法言法语扩展")
    ok("假货 → 欺诈", "欺诈" in K.expand_legal("买到假货能退一赔三吗"))
    ok("醉驾 → 醉酒", "醉酒" in K.expand_legal("醉驾怎么处罚"))
    ok("异物 → 混有", "混有" in K.expand_legal("外卖吃出异物"))
    ok("押金 → 租赁", "租赁" in K.expand_legal("房东不退押金"))
    eq("没有规则命中时为空", K.expand_legal("量子力学"), [])

    # 反向对照：规则表是数据，改掉就该不生效
    saved = dict(K.LEGAL_SYNONYMS)
    try:
        K.LEGAL_SYNONYMS.clear()
        eq("清空规则表后不再扩展", K.expand_legal("买到假货能退一赔三吗"), [])
    finally:
        K.LEGAL_SYNONYMS.update(saved)


def test_phrase_bonus() -> None:
    print("\n[5] 最长公共子串加成")
    q = "违约金太高能少给吗"
    good = K.phrase_bonus(q, "当事人可以约定违约金")
    bad = K.phrase_bonus(q, "不能少于十三年")
    ok("含「违约金」的得分更高", good > bad, f"{good} vs {bad}")
    ok("加成封顶 1.0", K.phrase_bonus("一二三四五六七八九十一二三四", "一二三四五六七八九十一二三四") <= 1.0)
    eq("无公共子串为 0", K.phrase_bonus("违约金", "完全无关的一句话"), 0.0)
    eq("查询太短不启用", K.phrase_bonus("定金", "定金不得超过百分之二十"), 0.0)


def test_bm25_sum() -> None:
    print("\n[6] BM25 求和（不是取最大值）")
    pack = {
        "entries": [
            {"id": "two", "title": "命中两个词", "type": "legal", "tags": [], "visibility": "public",
             "scopes": ["legal"], "audiences": ["self"], "importance": None, "summary": "", "path": ""},
            {"id": "one", "title": "命中一个词", "type": "legal", "tags": [], "visibility": "public",
             "scopes": ["legal"], "audiences": ["self"], "importance": None, "summary": "", "path": ""},
        ],
        "chunks": [
            {"eid": "two", "heading": "", "text": "违约 约金"},
            {"eid": "one", "heading": "", "text": "违约"},
        ],
    }
    rows = K.search(pack, "违约 约金", top_k=2)
    eq("两条都召回", len(rows), 2)
    eq("命中两个词的排前面", rows[0]["id"], "two")
    ok("两个词的分数严格更高", rows[0]["score"] > rows[1]["score"],
       f"{rows[0]['score']} vs {rows[1]['score']}")


def test_search_on_pack() -> None:
    print("\n[7] 扩展开关对结果的影响")
    pack = make_pack()
    q = "买到假货能退一赔三吗"

    with_ext = K.search(pack, q, top_k=3, expand=True)
    eq("开了扩展：消保法排第一", titles(with_ext)[0], "消费者权益保护法")

    without = K.search(pack, q, top_k=3, expand=False)
    ok("关掉扩展：消保法进不来（条文里没有「假货」字面）",
       "消费者权益保护法" not in titles(without), str(titles(without)))

    q2 = "违约金太高能少给吗"
    r2 = K.search(pack, q2, top_k=2, expand=True)
    eq("违约金查询首位是民法典", titles(r2)[0], "民法典")
    ok("「不能少于」不再抢位", "刑法" not in titles(r2), str(titles(r2)))

    # 停用词是查询侧和索引侧一致的：拆掉它就回到被碎片带偏的状态
    saved = set(K.STOP_BIGRAMS)
    try:
        K.STOP_BIGRAMS.clear()
        pack2 = make_pack()
        K.search(pack2, "预热", top_k=1)  # 触发索引（索引里也不过滤了）
        bad = K.search(pack2, q2, top_k=2, expand=False)
        ok("不挡碎片时刑法会顶上来（反向对照）", "刑法" in titles(bad), str(titles(bad)))
    finally:
        K.STOP_BIGRAMS.update(saved)


def test_scope_gate() -> None:
    print("\n[8] 场景闸门在扩展模式下仍然有效")
    pack = make_pack()
    rows = K.search(pack, "违约金", top_k=5, expand=True,
                    scopes={"finance"}, audiences={"client"})
    eq("finance 场景搜不到 legal 领域条目", len(rows), 0)
    rows2 = K.search(pack, "违约金", top_k=5, expand=True, scopes={"legal"})
    ok("legal 场景搜得到", len(rows2) >= 1)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_num_to_cn()
        test_norm_law()
        test_merge(tmp)
        test_expand()
        test_phrase_bonus()
        test_bm25_sum()
        test_search_on_pack()
        test_scope_gate()
    print("\n" + "=" * 46)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
