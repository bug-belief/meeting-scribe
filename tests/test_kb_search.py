"""知识检索层自检：分词、切块、过滤、排序、注入预算。

重点验「过滤在检索之前」——这是整套设计的地基。如果 secret 条目能被搜到，
后面再怎么打码都晚了。

跑法（仓库根目录）：
    python tests/test_kb_search.py
不依赖 pytest，不碰真实索引包（临时目录现造）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = FAIL = 0


def check(name: str, got, want, tol: float = 1e-9) -> None:
    global PASS, FAIL
    ok = (abs(float(got) - float(want)) <= tol) if isinstance(want, (int, float)) else (got == want)
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}：得到 {got!r}，期望 {want!r}")


def check_true(name: str, cond, detail: str = "") -> None:
    check(name, bool(cond), True) if cond else check(name + (f"（{detail}）" if detail else ""), False, True)


def make_kb(root: Path) -> Path:
    """造一个 3 条目的小 KB：一条正常、一条 secret、一条只有 background。"""
    (root / "index").mkdir(parents=True)
    entries = root / "entries" / "2026"
    (entries / "a-order").mkdir(parents=True)
    (entries / "a-order" / "a-order.md").write_text(
        "# 订单系统\n\n## 拆单合并\n\n拆单合并要解决同一笔订单被拆成多张子单后库存如何对账的问题，核心是同一套账。\n"
        "## 落地\n\n分三期推进，每期都要过验收，最后一期才允许下线旧流程。\n", encoding="utf-8")
    (entries / "a-order" / "background.md").write_text(
        "# 底稿\n\n这段是给自己看的，内含拆单合并四个字的内部备注。\n", encoding="utf-8")
    (entries / "b-secret").mkdir(parents=True)
    (entries / "b-secret" / "b-secret.md").write_text(
        "# 私人条目\n\n## 银行卡\n\n拆单合并 某银行 6222 开头那个账户。\n", encoding="utf-8")
    (entries / "c-mini").mkdir(parents=True)
    (entries / "c-mini" / "c-mini.md").write_text(
        "# 小程序\n\n## 避坑\n\n小程序提审时按钮文案不能出现诱导分享的字眼。\n", encoding="utf-8")

    metas = [
        {"id": "a-order", "title": "订单系统", "type": "work", "tags": ["订单"],
         "keywords": ["拆单合并"], "visibility": "internal", "importance": 4,
         "summary": "订单系统改造"},
        {"id": "b-secret", "title": "私人条目", "type": "personal", "tags": ["私事"],
         "keywords": ["银行卡"], "visibility": "secret", "importance": 1,
         "summary": "不该被检索到"},
        {"id": "c-mini", "title": "小程序避坑", "type": "ai-project", "tags": ["小程序"],
         "keywords": ["提审"], "visibility": "public", "importance": 3,
         "summary": "提审注意事项"},
    ]
    (root / "index" / "index.json").write_text(
        json.dumps({"count": len(metas), "entries": metas}, ensure_ascii=False),
        encoding="utf-8")
    return root


def main() -> int:
    import kb_search as K

    print("1) 分词：中文二元组 + 英文小写")
    toks = K.tokenize("拆单合并 SQL Server")
    check_true("中文切成二元组", "拆单" in toks and "单合" in toks, str(toks[:6]))
    check_true("英文转小写", "sql" in toks and "server" in toks)
    check_true("虚词二元组被丢掉", "的的" not in K.tokenize("的的的"))
    check("空串返回空列表", K.tokenize(""), [])

    print("\n2) 切块：按标题切、短块丢弃")
    chunks = K.split_chunks("# 一\n\n## 甲\n\n这是一段足够长的正文内容，用来测试切块是否按标题正确分开。\n"
                            "## 乙\n\n短\n")
    check("块数", len(chunks), 1)  # 「# 一」下无正文，「## 乙」下太短
    check("标题记下来了", chunks[0]["heading"], "甲")
    check_true("超长截断", len(K.split_chunks("## 长\n\n" + "字" * 2000)[0]["text"]) <= K.MAX_CHUNK_CHARS)

    with tempfile.TemporaryDirectory() as td:
        root = make_kb(Path(td) / "kb")

        print("\n3) 构建：secret 与 background 物理不进包")
        pack = K.build_kb_pack(root)
        ids = {e["id"] for e in pack["entries"]}
        check("收录条数", pack["count"], 2)
        check_true("有 a-order", "a-order" in ids)
        check_true("有 c-mini", "c-mini" in ids)
        check_true("secret 条目不在包里", "b-secret" not in ids, str(sorted(ids)))
        blob = json.dumps(pack, ensure_ascii=False)
        check_true("包里搜不到「某银行」", "某银行" not in blob)
        check_true("包里搜不到底稿内容", "内部备注" not in blob)

        print("\n4) 检索：能查到、排序对")
        hits = K.search(pack, "拆单合并", top_k=5)
        check_true("有结果", len(hits) >= 1)
        check("首位是目标条目", hits[0]["id"], "a-order")
        check_true("带片段", bool(hits[0]["snippet"].strip()))
        check("查不存在的词返回空", K.search(pack, "量子纠缠"), [])

        print("\n4b) 片段清洗：markdown 残渣不能进 prompt")
        dirty = ("- 打通示例链路\n[[2020-01-x|示例条目]] <!-- END kb-managed links -->\n"
                 "**要点**：`拆单` 并行\n- 第二条")
        clean = K.clean_snippet(dirty)
        for bad, why in [("[[", "wiki 链接"), ("<!--", "HTML 注释"), ("**", "加粗"),
                         ("`", "反引号"), ("- ", "列表符号")]:
            check_true(f"已去掉{why}", bad not in clean, clean[:60])
        check_true("保留正文", "打通示例链路" in clean and "示例条目" in clean)
        dirty2 = ("定金罚则正文在这里。\n> 官方全文　某来源\n> 抓取日期 2026-10-19　｜　基准来源：某官网\n")
        clean2 = K.clean_snippet(dirty2)
        check_true("溯源注脚被截掉", "抓取日期" not in clean2 and "基准来源" not in clean2, clean2)
        check_true("注脚前的正文还在", "定金罚则正文在这里" in clean2)

        print("\n5) 闸门：即便包里有 secret，visible 过滤也要挡")
        pack2 = K.build_kb_pack(root, include_secret=True)
        check("强制收录后变成 3 条", pack2["count"], 3)
        raw = K.search(pack2, "某银行", top_k=5)
        check_true("不过滤时能搜到 secret（证明闸门确有必要）", len(raw) >= 1)
        gated = K.search(pack2, "某银行", top_k=5, visibility={"public", "internal"})
        check("过滤后为空", gated, [])

        print("\n6) 注入：预算与格式")
        os.environ["KB_PACK"] = str(Path(td) / "pack.json")
        Path(os.environ["KB_PACK"]).write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        os.environ["KB_BUDGET"] = "60"
        # 法律包必须显式指到**不存在的路径**：不设的话 load_legal() 会去读线上真的
        # ~/data/kb/legal-pack.json。以前那包只有 526 条合同编、命中不了这里的问题，
        # 测试是侥幸绿的；扩到 4.8 万条后法条挤进注入，预算断言立刻爆掉。
        # 这属于测试隔离缺陷，不是产品 bug —— 修法是隔离，不是放宽断言。
        os.environ["KB_LEGAL_PACK"] = str(Path(td) / "no-legal.json")
        import kb_bridge as B

        B._cache.update(path=None, mtime=0.0, pack=None)
        B._legal.update(path=None, mtime=0.0, pack=None)
        ctx = B.context_for([{"who": "对方", "text": "拆单合并那块你们怎么做的"}])
        check_true("有注入", ctx.startswith("【背景知识】"))
        check_true("预算生效（含标题不超 2 倍）", len(ctx) < 260, f"len={len(ctx)}")
        check_true("命中内容在里头", "拆单" in ctx)

        print("\n7) 开关与缺包：故障时静默降级")
        os.environ["KB_ENABLED"] = "0"
        B._cache.update(path=None, mtime=0.0, pack=None)
        check("关掉后返回空", B.context_for([{"text": "拆单合并"}]), "")
        os.environ["KB_ENABLED"] = "1"
        os.environ["KB_PACK"] = str(Path(td) / "nope.json")
        B._cache.update(path=None, mtime=0.0, pack=None)
        check("包不存在返回空", B.context_for([{"text": "拆单合并"}]), "")
        check("包不存在 hits 为空", B.hits(None, "", "拆单合并"), [])
        os.environ.pop("KB_BUDGET", None)
        os.environ.pop("KB_PACK", None)

        print("\n8) 扁平语料：一条一个文件，README 不入库")
        legal_dir = Path(td) / "legal"
        (legal_dir / "民法典").mkdir(parents=True)
        (legal_dir / "README.md").write_text("# 说明\n\n这是目录说明，不该被检索到。\n", encoding="utf-8")
        (legal_dir / "民法典" / "0587-定金罚则.md").write_text(
            "# 民法典·第五百八十七条\n\n给付定金的一方不履行债务，致使不能实现合同目的的，"
            "无权请求返还定金；收受定金的一方不履行的，应当双倍返还定金。\n", encoding="utf-8")
        (legal_dir / "民法典" / "0585-违约金.md").write_text(
            "# 民法典·第五百八十五条\n\n当事人可以约定违约金，约定的违约金过分高于造成的损失的，"
            "可以根据当事人的请求予以适当减少。\n", encoding="utf-8")
        lpack = K.build_flat_pack(legal_dir)
        check("条目数（README 已跳过）", lpack["count"], 2)
        check_true("标题取一级标题", any("第五百八十七条" in e["title"] for e in lpack["entries"]))
        lhits = K.search(lpack, "定金双倍返还", top_k=2)
        check_true("能查到定金条", lhits and "第五百八十七条" in lhits[0]["title"], str(lhits[:1]))
        check("违约金也能查到", "第五百八十五条" in K.search(lpack, "违约金过高", top_k=1)[0]["title"], True)

        print("\n9) 双语料合并：个人 KB + 法律")
        lpath = Path(td) / "legal-pack.json"
        lpath.write_text(json.dumps(lpack, ensure_ascii=False), encoding="utf-8")
        kpath = Path(td) / "kb-pack.json"
        kpath.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        os.environ["KB_PACK"] = str(kpath)
        os.environ["KB_LEGAL_PACK"] = str(lpath)
        os.environ["KB_ENABLED"] = "1"
        os.environ["KB_BUDGET"] = "700"
        B._cache.update(path=None, mtime=0.0, pack=None)
        B._legal.update(path=None, mtime=0.0, pack=None)
        corpora = [c for c, _ in B.packs()]
        check("两个语料都载入", corpora, ["kb", "legal"])
        mixed = B.hits(None, "", "拆单合并", 3)
        check("个人语料有命中", mixed[0]["corpus"], "kb")
        legal_only = B.hits(None, "", "定金双倍返还", 3)
        check_true("法律语料有命中", legal_only and legal_only[0]["corpus"] == "legal",
                   str(legal_only[:1]))
        ctx2 = B.context_for([{"text": "定金他不退我怎么办"}])
        check_true("注入里带法条", "第五百八十七条" in ctx2 or "定金" in ctx2, ctx2[:80])
        os.environ.pop("KB_LEGAL_PACK", None)

    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
