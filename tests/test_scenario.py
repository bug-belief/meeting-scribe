"""场景包自检：同一个 KB，不同场合进来的是不同的东西。

这条线的价值全在「对外场合不该冒出私人生活」这种**排除**上，
所以测试重点不是「能查到」，而是「该挡的确实挡掉了」。

跑法（仓库根目录）：
    python tests/test_scenario.py
不依赖 pytest，场景包写到临时目录，不碰 ~/data/kb。

⚠️ 语料全部是虚构的（订单系统 / 私人生活 / 房屋租赁 / 生活杂事）。
不要换成任何真实项目、真实经历或真实人物 —— 这份文件是公开的。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}：得到 {got!r}，期望 {want!r}")


def check_true(name: str, cond, detail: str = "") -> None:
    check(name + (f"（{detail}）" if (detail and not cond) else ""), bool(cond), True)


SCENARIOS_YAML = """
version: 1
default: default
scenarios:
  default:
    label: 通用
    scopes: []
    audience: ""
    top_k: 3
    budget: 700
  interview:
    label: 面试
    scopes: [career, tech, general]
    audience: recruiter
    top_k: 3
    budget: 900
  call:
    label: 电话
    scopes: []
    audience: friend
    top_k: 2
    budget: 400
  negotiation:
    label: 谈判
    scopes: [legal, finance, general]
    audience: client
    top_k: 3
    budget: 800
"""


def make_kb(root: Path) -> Path:
    """4 条：技术项目 / 私人生活 / 法律 / 通用。全部虚构。"""
    (root / "index").mkdir(parents=True)
    entries = root / "entries" / "2026"

    def put(eid: str, body: str) -> None:
        d = entries / eid
        d.mkdir(parents=True)
        (d / f"{eid}.md").write_text(body, encoding="utf-8")

    put("t-order", "# 订单系统\n\n## 拆单合并\n\n拆单合并要解决同一笔订单被拆成多张子单后"
                   "库存如何对账的问题，核心是同一套账。分三期推进，每期都要过验收。\n")
    put("p-life", "# 个人生活\n\n## 作息\n\n最近在调作息，医生建议每天早睡，"
                  "另外把家里的健身计划也排进了日程。\n")
    put("l-rent", "# 房屋租赁\n\n## 押金纠纷\n\n租房押金被扣，房东主张家具折旧，"
                 "双方对赔偿金额有争议，按合同条款协商。\n")
    put("g-misc", "# 杂项\n\n## 代办\n\n周末去取快递，顺便把旧书整理一下送人。\n")

    metas = [
        {"id": "t-order", "title": "订单系统", "type": "work",
         "tags": ["订单", "技术"], "keywords": ["拆单合并", "系统"],
         "visibility": "internal", "importance": 4, "summary": "订单系统改造"},
        {"id": "p-life", "title": "个人生活", "type": "experience",
         "tags": ["家庭", "健康"], "keywords": ["作息", "健身"],
         "visibility": "internal", "importance": 3, "summary": "日常作息"},
        {"id": "l-rent", "title": "房屋租赁", "type": "experience",
         "tags": ["法律", "合同"], "keywords": ["押金", "赔偿"],
         "visibility": "internal", "importance": 3, "summary": "租赁纠纷"},
        {"id": "g-misc", "title": "杂项", "type": "tool",
         "tags": ["杂记"], "keywords": ["快递"],
         "visibility": "internal", "importance": 1, "summary": "生活杂事"},
    ]
    (root / "index" / "index.json").write_text(
        json.dumps({"count": len(metas), "entries": metas}, ensure_ascii=False),
        encoding="utf-8")
    return root


def main() -> int:
    import kb_search as K

    with tempfile.TemporaryDirectory() as td:
        root = make_kb(Path(td) / "kb")
        pack = K.build_kb_pack(root)
        by_id = {e["id"]: e for e in pack["entries"]}

        print("1) 领域推断（不手填 meta.yaml 也要有个说法）")
        check("订单系统 → tech", by_id["t-order"]["scopes"], ["tech"])
        check("个人生活 → personal", by_id["p-life"]["scopes"], ["personal"])
        check("房屋租赁 → legal", by_id["l-rent"]["scopes"], ["legal"])
        check("杂项 → general", by_id["g-misc"]["scopes"], ["general"])

        print("\n2) 听众推断：personal 自动不给客户/面试官")
        check("个人生活只给 self/friend", by_id["p-life"]["audiences"], ["self", "friend"])
        check_true("订单系统可给客户", "client" in by_id["t-order"]["audiences"])
        check_true("订单系统可给面试官", "recruiter" in by_id["t-order"]["audiences"])

        print("\n3) 场景闸门（直接问 kb_search）")
        ids_all = {h["id"] for h in K.search(pack, "作息 健身 身体", top_k=5)}
        check_true("不限场景能查到个人生活", "p-life" in ids_all, str(ids_all))
        ids_rec = {h["id"] for h in K.search(pack, "作息 健身 身体", top_k=5,
                                             scopes={"career", "tech", "general"},
                                             audiences={"recruiter"})}
        check_true("对外场合挡住个人生活", "p-life" not in ids_rec, str(ids_rec))

        ids_neg = {h["id"] for h in K.search(pack, "押金 赔偿 合同", top_k=5,
                                             scopes={"legal", "finance", "general"},
                                             audiences={"client"})}
        check_true("谈判场景能查到租赁纠纷", "l-rent" in ids_neg, str(ids_neg))
        check_true("谈判场景挡住订单项目", "t-order" not in ids_neg, str(ids_neg))

        print("\n4) resolve_scenario：文件在手走文件，档名不认识退默认")
        scen_dir = Path(td) / "kb"
        scen_path = scen_dir / "scenarios.yaml"
        scen_path.write_text(SCENARIOS_YAML, encoding="utf-8")
        os.environ["KB_SCENARIOS"] = str(scen_path)
        os.environ["KB_PACK"] = str(scen_dir / "kb-pack.json")
        (scen_dir / "kb-pack.json").write_text(json.dumps(pack, ensure_ascii=False),
                                               encoding="utf-8")
        # 指向一个不存在的路径而不是 unset：服务器上 ~/data/kb/legal-pack.json 是真的，
        # unset 会让法条挤进结果里，测试就不是在测自己造的那 4 条了。
        os.environ["KB_LEGAL_PACK"] = str(Path(td) / "没有法条.json")

        import kb_bridge as B
        B._scen.update(path=None, mtime=0.0, data=None)
        B._cache.update(path=None, mtime=0.0, pack=None)

        check("interview 档的 audience", B.resolve_scenario("interview")["audiences"], {"recruiter"})
        check("interview 档的 budget", B.resolve_scenario("interview")["budget"], 900)
        check("call 档 top_k", B.resolve_scenario("call")["top_k"], 2)
        check("不认识的档名退回 default", B.resolve_scenario("不存在的档")["name"], "default")
        names = [x["name"] for x in B.scenario_names()]
        check("场景清单", names, ["default", "interview", "call", "negotiation"])

        print("\n5) hits 端到端：同一句问话，不同场景不同结果")
        q = "作息 健身 身体 赔偿"
        all_ids = {h["id"] for h in B.hits(query=q, scenario="default", top_k=5)}
        check_true("通用档能拿到个人生活", "p-life" in all_ids, str(all_ids))
        call_ids = {h["id"] for h in B.hits(query=q, scenario="call", top_k=5)}
        check_true("电话档（朋友）拿到个人生活", "p-life" in call_ids, str(call_ids))
        itv_ids = {h["id"] for h in B.hits(query=q, scenario="interview", top_k=5)}
        check_true("面试档拿不到个人生活", "p-life" not in itv_ids, str(itv_ids))
        check_true("面试档结果都在允许领域",
                   all(set(h["scopes"]) & {"career", "tech", "general"} or
                       "general" in h["scopes"] for h in B.hits(query=q, scenario="interview", top_k=5)))

        print("\n6) 预算与条数由场景决定")
        ctx_call = B.context_for(query="作息 健身 身体 赔偿", scenario="call")
        ctx_itv = B.context_for(query="作息 健身 身体 赔偿", scenario="interview")
        check_true("电话档上下文更短或等长", len(ctx_call) <= 700, str(len(ctx_call)))
        check_true("面试档上下文不含私人生活", "作息" not in ctx_itv and "健身" not in ctx_itv,
                   ctx_itv[:80])
        check("call 档最多 2 条", len(B.hits(query=q, scenario="call", top_k=None)), 2)

        print("\n7) 文件丢了也能跑（内置兜底）")
        os.environ["KB_SCENARIOS"] = str(Path(td) / "不存在.yaml")
        B._scen.update(path=None, mtime=0.0, data=None)
        check("退回内置 default", B.resolve_scenario("")["name"], "default")
        check("内置档位可用", B.resolve_scenario("meeting")["name"], "meeting")

        print("\n8) 旧包（没有 scopes/audiences 字段）不被误杀")
        legacy = {"version": 1, "corpus": "old", "count": 1,
                  "entries": [{"id": "o1", "title": "旧条目", "type": "work",
                               "tags": [], "keywords": ["拆单合并"],
                               "visibility": "internal", "importance": 3,
                               "summary": "", "path": "x.md"}],
                  "chunks": [{"eid": "o1", "heading": "a",
                              "text": "拆单合并 是 核心 问题", "start": 0}]}
        rows = K.search(legacy, "拆单合并", top_k=3, scopes={"career", "tech"},
                        audiences={"recruiter"})
        check("旧包缺字段仍可命中", len(rows), 1)

    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
