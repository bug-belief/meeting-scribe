#!/usr/bin/env python3
"""kb_search.py —— 知识检索层（个人 KB / 法律知识 共用一套引擎）

为什么单独一个文件：它要被两处使用——
  ① 本机（Windows）构建索引包；
  ② <你的主机> 上被 assistant.py / prompter.py 在运行时调用。
一个文件两头用，才不会出现「本机查得到、线上查不到」。

为什么不用 RAG / 向量库：条目量级只有两位数、正文 1.2MB，BM25 + 中文二元组
已经足够，且零依赖、可离线、结果可解释（能指出命中在哪一段）。
真到几千条再换向量检索。

过滤铁律：**过滤发生在检索之前**，不是在结果上打码。
  · `visibility: secret` 的条目在构建期就不进索引包，线上根本不存在；
  · `background.md` 永不进包（它是给自己看的底稿，不是知识）；
  · 字段级 secret 字段在构建期剔除，不进正文。

用法：
    python kb_search.py build                    # 建个人 KB 索引包
    python kb_search.py build --flat <目录>       # 建扁平语料（法律知识等）
    python kb_search.py search "拆单 合并"
    python kb_search.py search "违约金" --pack ~/data/kb/legal-pack.json
    python kb_search.py stats
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

PACK_VERSION = 1
DEFAULT_TOP_K = 5
MAX_CHUNK_CHARS = 700
# 短块单独成块往往只有标题价值，但阈值太高会漏掉「话少但关键」的条目
# （实测 24 字会丢掉 20 字左右的小节），定在 16 字。
MIN_CHUNK_CHARS = 16

# 中文按二元组切；英文/数字按词切。不引 jieba：多一个依赖，多一次环境不一致。
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
ASCII_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+#.\-]*")

# 中文虚词二元组：命中它们不说明任何问题，反而把短查询带偏
STOP_BIGRAMS = {
    "的的", "了的", "是的", "这个", "那个", "我们", "你们", "他们", "什么",
    "怎么", "可以", "一个", "就是", "不是", "没有", "还有", "因为", "所以",
    "如果", "但是", "然后", "现在", "已经", "需要", "进行", "通过", "以及",
    # 跨词边界拼出来的碎片。二元组切「违约金太高能少给吗」会得到「能少」「少给」，
    # 而「不能少于」在刑法减刑条款里反复出现 —— IDF 极高，于是命中的是碎片而不是语义：
    # 实测 4.8 万条法律语料下，这句话首位是《刑法修正案（八）》第十五条，
    # 真正该出的《民法典》585 掉到第 4。挡掉这批碎片后纠正回 585，
    # 且另外四个对照组查询（定金/交通/劳动报酬/试用期）排序一字未变。
    "能少", "少给", "太高", "高能", "金太", "给吗", "太多", "多少",
    "不够", "咋办", "能不", "不能",
}


# ---------------------------------------------------------------- 口语 → 法言法语
#
# 二元组 BM25 在 75 条个人 KB 上够用，铺到 4.8 万条法律语料就不够了：
# 「买到假货能退一赔三吗」会被切成「假货」「一赔」「赔三」，首位居然是《监狱法》；
# 「外卖吃出异物」切出「异物」,首位是《反食品浪费法》。原因不是排序坏了，
# 是**召回阶段就没有正确条文**——法条里写的是「欺诈」「混有异物」，不是这些口语词。
#
# 规则来自 thu-lawyer/china-law-mcp 的 retrieval.py（MIT），按本项目场景增删：
# 删掉了会误伤的宽泛项（「赔偿」→「损害」这类自反规则），补了消费维权几条。
# 命中 key 时把右侧词并入查询，权重 0.5（不是 1.0）——扩展词是猜的，不能压过原话。
LEGAL_SYNONYMS: dict[str, tuple[str, ...]] = {
    # 交通
    "醉驾": ("醉酒", "驾驶", "机动车"), "酒驾": ("饮酒", "醉酒", "驾驶", "机动车"),
    "酒后开车": ("饮酒", "驾驶", "机动车"), "开车": ("机动车", "驾驶"),
    "车祸": ("交通事故", "机动车", "损害"), "撞人": ("交通事故", "损害", "赔偿"),
    # 劳动
    "试用期": ("劳动合同", "用人单位"), "辞退": ("解除", "劳动合同", "经济补偿"),
    "开除": ("解除", "劳动合同"), "离职": ("解除", "劳动合同"),
    "加班": ("加班", "工资"), "拖欠工资": ("劳动报酬", "工资"),
    "社保": ("社会保险", "社会保险费"), "工伤": ("工伤", "社会保险"),
    # 消费
    "假货": ("欺诈", "假冒"), "退一赔三": ("欺诈", "增加赔偿"),
    "无理由退货": ("退货", "七日"), "网购": ("网络", "经营者"),
    "网上购物": ("网络", "经营者"), "异物": ("混有", "食品"),
    "过期食品": ("食品", "食品安全"),
    # 民事
    "借钱": ("借款", "返还"), "欠钱": ("借款", "返还"),
    "借钱不还": ("借款", "返还", "诉讼时效"), "押金": ("租赁", "返还"),
    "房东": ("租赁", "出租人", "承租人"), "租房": ("租赁", "出租人"),
    "彩礼": ("婚约", "返还"), "高空抛物": ("抛掷", "坠落", "建筑物"),
    "高空坠物": ("抛掷", "坠落", "建筑物"), "遗产": ("继承", "遗产"),
    "物业": ("物业服务", "业主"), "噪音": ("噪声", "污染", "相邻"),
    "漏水": ("相邻", "不动产", "排水"), "隐私": ("隐私权", "个人信息"),
    "泄露": ("泄露", "个人信息"), "抚养费": ("抚养", "子女"),
    "精神损失": ("精神损害", "严重"), "正当防卫": ("不法侵害", "防卫"),
    # 刑事
    "诈骗": ("诈骗", "骗取"), "偷窃": ("盗窃",), "打架": ("殴打", "故意伤害"),
}

# 共现规则：口语常被切成单字，整词命不中；要求这些字**同时出现**才补术语。
LEGAL_COOCCUR: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("外卖", "吃"), ("食品", "食品安全", "消费者")),
    (("吃出",), ("食品", "食品安全", "赔偿")),
    (("变质",), ("食品", "食品安全")),
    (("快递", "丢"), ("运输", "赔偿", "承运人")),
    (("离婚", "孩子"), ("抚养", "离婚", "子女")),
    (("离婚", "财产"), ("夫妻共同财产", "分割")),
    (("公司", "辞"), ("劳动合同", "解除", "经济补偿")),
    (("狗", "咬"), ("饲养", "动物", "损害")),
    (("宠物", "咬"), ("饲养", "动物", "损害")),
    (("东西", "坏"), ("消费者", "质量", "赔偿")),
    (("退", "钱"), ("退货", "返还", "消费者")),
    (("高空", "掉"), ("建筑物", "坠落", "损害")),
)

# 召回多少条再重排。法律语料条多，60 足够把正确条文捞进候选。
RECALL_K = 60
# 扩展词的权重：0.5 是实测出来的。等权（1.0）时「个人信息被泄露」里的「提供」
# 会把《民法典》顶到首位；太低（0.35）又救不回「外卖吃出异物」这种需要扩展的查询。
EXT_WEIGHT = 0.5


def expand_legal(query: str) -> list[str]:
    """口语查询 → 补充的法律术语（只返回扩展出来的部分）。"""
    out: list[str] = []
    for key, extra in LEGAL_SYNONYMS.items():
        if key in query:
            out.extend(extra)
    for keys, extra in LEGAL_COOCCUR:
        if all(k in query for k in keys):
            out.extend(extra)
    return out


def phrase_bonus(core: str, text: str, cap: int = 12) -> float:
    """查询与文本的最长公共子串 / cap。

    这是纠正「稀有碎片压过真词」的关键信号：查「违约金太高能少给吗」时，
    《民法典》585 与它的最长公共子串是「违约金」(3)，
    而《刑法修正案（八）》只有「能少」(2) —— 长的那条该赢。
    """
    n = len(core)
    if n < 4:
        return 0.0
    best = 0
    for i in range(n):
        for j in range(n, i + 1, -1):
            if j - i > best and core[i:j] in text:
                best = j - i
                break
    return min(best, cap) / cap



# ---------------------------------------------------------------- 领域 / 听众

# 条目的「领域」和「能说给谁听」。
# 为什么不在 meta.yaml 里逐个手填：69 条手工打标必漏，而且以后新增条目还会忘。
# 这里按 type/tags/keywords/summary 自动推断，meta.yaml 里写了 `scope:` / `audience:`
# 就以手填的为准（想覆盖推断结果时用）。
SCOPE_TERMS: dict[str, tuple[str, ...]] = {
    "career": ("简历", "求职", "面试", "招聘", "岗位", "薪资", "年薪", "离职", "入职",
               "职业", "工作经历", "跳槽", "offer", "hr", "晋升", "绩效", "团队管理",
               "个人简历", "求职信"),
    "tech": ("技术", "架构", "代码", "开发", "系统", "部署", "服务器", "数据库", "python",
             "java", "算法", "模型", "大模型", "ai", "api", "前端", "后端", "接口",
             "性能", "优化", "框架", "微服务", "容器", "运维", "本地部署", "asr", "tts"),
    "legal": ("法律", "诉讼", "起诉", "法院", "判决", "法条", "民法典", "合同", "赔偿",
              "维权", "保险", "交警", "事故", "立案", "调解", "举证", "折旧"),
    "finance": ("成本", "报价", "价格", "费用", "账单", "投资", "股票", "基金", "理财",
                "预算", "报销", "付费", "计价", "省钱", "水分", "单价", "总价"),
    "personal": ("家庭", "孩子", "父母", "健康", "医院", "旅行", "猫", "宠物",
                 "生日", "亲戚", "方言", "家乡", "体检", "体重", "看病"),
}

ALL_SCOPES = tuple(SCOPE_TERMS) + ("general",)
ALL_AUDIENCES = ("self", "friend", "client", "recruiter", "public")


def _haystack(meta: dict) -> str:
    """只看标题 / 类型 / 标签 / 关键词，**不看 summary 和正文**。

    实测：把 summary 也算进来的话「系统」「开发」这类词几乎每条都中，
    tech 会覆盖 66/75 条，场景过滤等于没做。标题 + 标签 + 关键词才是这条
    「是什么」的浓缩，判领域只用这一段最准。
    """
    parts = [
        str(meta.get("title") or ""),
        str(meta.get("type") or ""),
    ]
    for key in ("tags", "keywords"):
        v = meta.get(key) or []
        parts.append(" ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v))
    return " ".join(parts).lower()


def infer_scopes(meta: dict) -> list[str]:
    """按关键词给条目分领域，最多两个；一个都不沾就归 general（通用，永不被场景挡住）。"""
    explicit = meta.get("scope")
    if isinstance(explicit, str) and explicit.strip():
        explicit = [x.strip() for x in re.split(r"[,，\s]+", explicit) if x.strip()]
    if isinstance(explicit, (list, tuple)) and explicit:
        return [str(x) for x in explicit if str(x) in ALL_SCOPES] or ["general"]

    hay = _haystack(meta)
    scores: list[tuple[int, str]] = []
    for scope, terms in SCOPE_TERMS.items():
        hit = sum(1 for t in terms if t.lower() in hay)
        if hit:
            scores.append((hit, scope))
    if not scores:
        return ["general"]
    scores.sort(key=lambda x: (-x[0], x[1]))
    top = scores[0][0]
    # 只保留跟第一名差距不大的（差一半以内），避免把「顺带提到一次」也算进来
    return [s for n, s in scores if n * 2 >= top][:2]


def infer_audiences(meta: dict, scopes: list[str] | None = None) -> list[str]:
    """这条能说给谁听。默认按 visibility 推，`personal` 领域的自动不给客户/面试官。"""
    explicit = meta.get("audience")
    if isinstance(explicit, str) and explicit.strip():
        explicit = [x.strip() for x in re.split(r"[,，\s]+", explicit) if x.strip()]
    if isinstance(explicit, (list, tuple)) and explicit:
        got = [str(x) for x in explicit if str(x) in ALL_AUDIENCES]
        return got or ["self"]

    vis = str(meta.get("visibility") or "internal")
    if vis in {"secret", "background"}:
        return ["self"]
    out = ["self", "friend", "client", "recruiter"]
    if vis == "public":
        out.append("public")
    # 只有「**除私人生活以外没有别的身份**」的条目才收窄听众。
    # personal + tech 双领域的条目（比如某个技术类个人作品），是作品不是家务事，
    # 不能因为沾了生活领域的关键词就不许在对外场合讲。
    if scopes and len(scopes) == 1 and scopes[0] == "personal":
        out = [a for a in out if a in {"self", "friend", "public"}]
    return out


def _as_set(v) -> set[str]:
    if isinstance(v, str):
        return {x.strip() for x in re.split(r"[,，\s]+", v) if x.strip()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return {str(x).strip() for x in v if str(x).strip()}
    return set()


def entry_allows(entry: dict, scopes: set[str] | None = None,
                 audiences: set[str] | None = None) -> bool:
    """条目是否被场景放行。缺字段的旧包按 general / 全放行处理，保证不误伤。"""
    if scopes:
        e_scopes = _as_set(entry.get("scopes")) or {"general"}
        if not (e_scopes & scopes) and "general" not in e_scopes:
            return False
    if audiences:
        e_aud = _as_set(entry.get("audiences")) or set(ALL_AUDIENCES)
        if not (e_aud & audiences):
            return False
    return True


# ---------------------------------------------------------------- 分词

def tokenize(text: str) -> list[str]:
    """中文二元组 + 英文词。纯函数，可被测试直接抽出来跑。"""
    if not text:
        return []
    out: list[str] = []
    for run in CJK_RE.findall(text):
        if len(run) == 1:
            out.append(run)
            continue
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in STOP_BIGRAMS:
                out.append(bg)
    for w in ASCII_RE.findall(text):
        out.append(w.lower())
    return out


def cjk_terms(text: str) -> set[str]:
    """查询词的中文片段（用于标题/标签的命中加成）。"""
    terms: set[str] = set()
    for run in CJK_RE.findall(text or ""):
        if 1 < len(run) <= 6:
            terms.add(run)
        elif len(run) > 6:
            terms.update(run[i:i + 2] for i in range(0, len(run) - 1, 2))
    for w in ASCII_RE.findall(text or ""):
        if len(w) >= 2:
            terms.add(w.lower())
    return terms


# ---------------------------------------------------------------- 切块

# 语料自带的溯源注脚（`> 抓取日期 …`）。给模型看只会占预算，还可能被它复述出来。
PROVENANCE_MARKERS = (
    "抓取日期", "基准来源", "未与国家法律法规", "不构成法律意见",
    "校验：官方全文", "校验:官方全文", "用途：谈判", "用途:谈判",
    "官方全文", "源一致",
)


def clean_snippet(text: str, limit: int = 280) -> str:
    """把片段洗成「能给模型看的一句话」。

    正文里混着 markdown 结构（列表符号、加粗、wiki 链接、kb-managed 注释），
    直接塞进 prompt 会让模型学到这些符号，甚至在回答里复述出来。
    语料文件末尾的溯源注脚（`>` 开头那几行）在这里截掉。
    """
    s = text or ""
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)          # HTML 注释
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", s)    # [[id|标题]] → 标题
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)          # [文本](链接) → 文本
    # 溯源注脚：从出现处整段截掉
    for line in s.splitlines():
        if line.lstrip().startswith(">") and any(m in line for m in PROVENANCE_MARKERS):
            s = s[: s.find(line)]
            break
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)          # 行首列表符号
    s = re.sub(r"^\s*#{1,6}\s+", "", s, flags=re.M)         # 行首井号
    s = s.replace("**", "").replace("`", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s).strip()
    return s[:limit]


def split_chunks(body: str) -> list[dict]:
    """按 markdown 标题切块；无标题的长文按段落兜底切。

    切块的意义：检索命中要能定位到「哪一段」，否则整篇 8KB 塞进 prompt，
    既费 token 又冲淡重点。
    """
    lines = body.splitlines()
    chunks: list[dict] = []
    heading = ""
    buf: list[str] = []

    def flush():
        text = "\n".join(buf).strip()
        # 太短的块（一行标题、一句备注）单独成块往往没有检索价值，丢弃
        if len(text) >= MIN_CHUNK_CHARS:
            chunks.append({"heading": heading, "text": text[:MAX_CHUNK_CHARS]})

    for line in lines:
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush()
            buf = []
            heading = m.group(2).strip()
            continue
        buf.append(line)
    flush()
    return chunks


# ---------------------------------------------------------------- 构建

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _strip_front_matter(body: str) -> str:
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            return body[end + 4:]
    return body


def build_kb_pack(kb_root: Path, include_secret: bool = False,
                  redact: str = "mask", redact_min_severity: str = "medium") -> dict:
    """个人 KB → 索引包。元数据取 index/index.json，正文取 entries/**/<id>.md。

    `redact`：`mask`（默认，构建期就把手机号/身份证/卡号/密钥替换掉）/
    `warn`（只统计不替换，用来先看误伤率）/ `off`。
    """
    idx_path = kb_root / "index" / "index.json"
    if not idx_path.is_file():
        raise SystemExit(f"找不到 {idx_path}，先跑 tools/kb.py index")
    meta_all = json.loads(_read_text(idx_path))
    metas = {e["id"]: e for e in meta_all.get("entries", [])}

    redactor = None
    if redact in {"mask", "warn"}:
        try:
            import redact as redact_mod
            redactor = redact_mod
        except Exception as exc:      # 缺文件时不要假装脱敏成功
            raise SystemExit(f"脱敏模块加载失败（--redact={redact}）：{exc}")

    entries: list[dict] = []
    chunks: list[dict] = []
    skipped: list[str] = []
    redactions: list[dict] = []

    for md in sorted((kb_root / "entries").rglob("*.md")):
        if md.name == "background.md":       # 底稿永不进包
            skipped.append(f"{md.name}(底稿)")
            continue
        eid = md.parent.name
        meta = metas.get(eid)
        if meta is None:
            skipped.append(f"{md.parent.name}(无 meta)")
            continue
        vis = str(meta.get("visibility") or "internal")
        if vis == "secret" and not include_secret:
            skipped.append(f"{eid}(secret)")
            continue

        scopes = infer_scopes(meta)
        entries.append({
            "id": eid,
            "title": meta.get("title") or eid,
            "type": meta.get("type") or "",
            "tags": meta.get("tags") or [],
            "keywords": meta.get("keywords") or [],
            "visibility": vis,
            "scopes": scopes,
            "audiences": infer_audiences(meta, scopes),
            "importance": meta.get("importance"),
            "summary": meta.get("summary") or "",
            "path": str(md.relative_to(kb_root)).replace("\\", "/"),
        })
        body = _strip_front_matter(_read_text(md))
        if redactor is not None:
            body2, found = redactor.redact_text(body, mode=redact,
                                                min_severity=redact_min_severity)
            if found:
                redactions.append({
                    "id": eid,
                    "count": len(found),
                    "kinds": redactor.summarize(found),
                    "applied": redact == "mask",
                })
                body = body2
        for c in split_chunks(body):
            chunks.append({"eid": eid, **c})

    return {
        "version": PACK_VERSION,
        "corpus": "personal-kb",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(entries),
        "skipped": skipped,
        "redactions": redactions,
        "entries": entries,
        "chunks": chunks,
    }


def build_flat_pack(root: Path) -> dict:
    """扁平语料（法律条文等）：目录下每个 .md 视为一条，文件名即 id。"""
    entries: list[dict] = []
    chunks: list[dict] = []
    for md in sorted(root.rglob("*.md")):
        if md.name.startswith("_") or md.name.lower() in {"readme.md", "index.md"}:
            continue
        eid = md.relative_to(root).as_posix()[: -len(".md")]
        body = _read_text(md)
        title = md.stem
        for line in body.splitlines():
            m = re.match(r"^#\s+(.*)$", line)
            if m:
                title = m.group(1).strip()
                break
        entries.append({
            "id": eid,
            "title": title,
            "type": "legal",
            "tags": [],
            "keywords": [],
            "visibility": "public",
            "scopes": ["legal"],
            "audiences": list(ALL_AUDIENCES),
            "importance": None,
            "summary": "",
            "path": md.relative_to(root).as_posix(),
        })
        for c in split_chunks(body):
            chunks.append({"eid": eid, **c})
    return {
        "version": PACK_VERSION,
        "corpus": root.name,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(entries),
        "skipped": [],
        "entries": entries,
        "chunks": chunks,
    }


# ---------------------------------------------------------------- 检索

def build_index(pack: dict) -> dict:
    """倒排索引 + 文档长度。结果缓存在 pack['_index']，同一份包只算一次。"""
    if pack.get("_index"):
        return pack["_index"]
    postings: dict[str, list[tuple[int, int]]] = {}
    lengths: list[int] = []
    for i, ch in enumerate(pack.get("chunks", [])):
        toks = tokenize(ch.get("heading", "") + "\n" + ch.get("text", ""))
        lengths.append(len(toks) or 1)
        for tok, tf in Counter(toks).items():
            postings.setdefault(tok, []).append((i, tf))
    total = len(lengths) or 1
    avgdl = sum(lengths) / total
    idf = {
        tok: math.log(1 + (total - len(pl) + 0.5) / (len(pl) + 0.5))
        for tok, pl in postings.items()
    }
    idx = {"postings": postings, "lengths": lengths, "avgdl": avgdl, "idf": idf}
    pack["_index"] = idx
    return idx


def search(pack: dict, query: str, top_k: int = DEFAULT_TOP_K,
           min_score: float = 0.0, visibility: set[str] | None = None,
           scopes: set[str] | None = None, audiences: set[str] | None = None,
           expand: bool = False) -> list[dict]:
    """BM25 + 标题/标签加成。返回按分数降序的条目级结果。

    同一篇只保留最高分的那一段，避免一条吃掉整个结果列表。

    `scopes` / `audiences` 是**场景闸门**：先算出放行的条目集合，命中才参与排序，
    而不是排完了再筛——否则 top_k 会被不该出现的条目占满。

    `expand=True` 打开法律语料的「口语→法言法语」扩展与重排（见 LEGAL_SYNONYMS）。
    默认关闭：个人 KB 条目少、同为口语体，扩展反而会引入噪声。
    """
    q = tokenize(query)
    if not q:
        return []
    idx = build_index(pack)
    postings, lengths, avgdl, idf = (
        idx["postings"], idx["lengths"], idx["avgdl"], idx["idf"])
    k1, b = 1.2, 0.75
    chunks = pack["chunks"]

    meta = {e["id"]: e for e in pack.get("entries", [])}
    allowed: set[str] | None = None
    if visibility or scopes or audiences:
        allowed = set()
        for eid, e in meta.items():
            if visibility is not None and (e.get("visibility") or "internal") not in visibility:
                continue
            if not entry_allows(e, scopes, audiences):
                continue
            allowed.add(eid)

    qset = set(q)
    ext_tokens: list[str] = []
    if expand:
        seen = set(qset)
        for term in expand_legal(query):
            for tok in tokenize(term):
                if tok not in seen:
                    seen.add(tok)
                    ext_tokens.append(tok)

    # 先在 chunk 内把多个 token 的贡献**加起来**，再在条目内取最高分的那段。
    # 早先这里写成了「逐 token 取最大值」，等于只认命中的那一个词 ——
    # 命中两个常见词（违约金）反而输给命中一个稀有碎片（能少），那是取最大而非求和的锅。
    chunk_score: dict[int, float] = {}

    def _add(tokens: list[str], weight: float) -> None:
        for tok in tokens:
            idf_v = idf.get(tok)
            if not idf_v:
                continue
            for ci, tf in postings.get(tok, []):
                if allowed is not None and chunks[ci]["eid"] not in allowed:
                    continue
                dl = lengths[ci]
                chunk_score[ci] = chunk_score.get(ci, 0.0) + weight * idf_v * (
                    tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))

    _add(q, 1.0)
    if ext_tokens:
        _add(ext_tokens, EXT_WEIGHT)

    best: dict[str, dict] = {}
    for ci, s in chunk_score.items():
        ch = chunks[ci]
        eid = ch["eid"]
        rec = best.get(eid)
        if rec is None or s > rec["chunk_score"]:
            best[eid] = {
                "id": eid,
                "chunk_score": s,
                "ci": ci,
                "heading": ch.get("heading", ""),
                "snippet": clean_snippet(ch.get("text", "")),
            }

    qterms = cjk_terms(query)
    out: list[dict] = []

    if expand and best:
        # 两段式：BM25 召回 top RECALL_K，再用「覆盖率 + 最长公共子串」重排。
        # 归一化 BM25 是为了消掉绝对分数差异 —— 尾部条文的文本短，BM25 天然偏低。
        mx = max(r["chunk_score"] for r in best.values()) or 1.0
        core = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", query)
        eset = set(ext_tokens)
        ranked = sorted(best.values(), key=lambda x: -x["chunk_score"])[:RECALL_K]
        for rec in ranked:
            ch = chunks[rec["ci"]]
            text = (ch.get("heading", "") or "") + "\n" + (ch.get("text", "") or "")
            cov = sum(1 for t in qset if t in text) / max(len(qset), 1)
            ecov = (sum(1 for t in eset if t in text) / len(eset)) if eset else 0.0
            rec["score"] = (rec["chunk_score"] / mx
                            + 0.45 * cov + 0.25 * ecov + 0.55 * phrase_bonus(core, text))
        for rec in ranked:
            e = meta.get(rec["id"], {})
            out.append({
                "id": rec["id"],
                "title": e.get("title") or rec["id"],
                "type": e.get("type") or "",
                "tags": e.get("tags") or [],
                "visibility": e.get("visibility") or "internal",
                "scopes": e.get("scopes") or ["general"],
                "audiences": e.get("audiences") or list(ALL_AUDIENCES),
                "importance": e.get("importance"),
                "path": e.get("path") or "",
                "heading": rec["heading"],
                "snippet": rec["snippet"],
                "score": round(rec["score"], 4),
            })
    else:
        for eid, rec in best.items():
            e = meta.get(eid, {})
            score = rec["chunk_score"]
            # 标题/标签命中给加成：BM25 只看正文，容易把「标题就是这个意思」的条目排低
            hay = " ".join([
                str(e.get("title") or ""),
                " ".join(str(x) for x in (e.get("tags") or [])),
                " ".join(str(x) for x in (e.get("keywords") or [])),
                str(e.get("summary") or ""),
            ])
            hits = sum(1 for t in qterms if t in hay)
            if hits:
                score *= 1 + min(hits, 4) * 0.35
            out.append({
                "id": eid,
                "title": e.get("title") or eid,
                "type": e.get("type") or "",
                "tags": e.get("tags") or [],
                "visibility": e.get("visibility") or "internal",
                "scopes": e.get("scopes") or ["general"],
                "audiences": e.get("audiences") or list(ALL_AUDIENCES),
                "importance": e.get("importance"),
                "path": e.get("path") or "",
                "heading": rec["heading"],
                "snippet": rec["snippet"],
                "score": round(score, 4),
            })

    if visibility is not None:
        # 兜底闸门：包本该已经过滤过，这里再按可见性挡一次
        out = [x for x in out if x["visibility"] in visibility]
    out.sort(key=lambda x: (-x["score"], -(x["importance"] or 0)))
    out = [x for x in out if x["score"] >= min_score]
    return out[:top_k]


def load_pack(path) -> dict:
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(f"索引包不存在：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- CLI

def default_pack() -> Path:
    raw = (os.getenv("KB_PACK") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path("~/data/kb/kb-pack.json").expanduser()


def cmd_build(args) -> int:
    if args.flat:
        root = Path(args.flat).expanduser()
        if not root.is_dir():
            raise SystemExit(f"目录不存在：{root}")
        pack = build_flat_pack(root)
    else:
        kb_root = Path(args.kb or Path(__file__).resolve().parent.parent)
        pack = build_kb_pack(kb_root, include_secret=args.include_secret,
                             redact=args.redact,
                             redact_min_severity=args.redact_min_severity)

    out = Path(args.out).expanduser() if args.out else default_pack()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    size = out.stat().st_size
    print(f"✅ 索引包：{out}")
    print(f"   语料 {pack['corpus']} · 条目 {pack['count']} · 段落 {len(pack['chunks'])} · {size / 1024:.0f} KB")
    if pack.get("skipped"):
        print(f"   跳过 {len(pack['skipped'])} 项：{'、'.join(pack['skipped'][:8])}"
              + ("…" if len(pack["skipped"]) > 8 else ""))
    if args.redact != "off" and not args.flat:
        rows = pack.get("redactions") or []
        total = sum(r["count"] for r in rows)
        kinds: Counter = Counter()
        for r in rows:
            kinds.update(r["kinds"])
        verb = "已脱敏" if args.redact == "mask" else "待脱敏（warn，未改动）"
        if total:
            print(f"   {verb} {total} 处 / {len(rows)} 条："
                  + "、".join(f"{k}={v}" for k, v in kinds.most_common()))
            for r in rows[:5]:
                print(f"     · {r['id']}：{r['kinds']}")
        else:
            print(f"   {verb} 0 处（正文无手机号/身份证/卡号/密钥）")
    return 0


def cmd_search(args) -> int:
    pack = load_pack(args.pack or default_pack())
    hits = search(pack, args.query, top_k=args.top,
                  min_score=args.min_score,
                  scopes=_as_set(args.scope) or None,
                  audiences=_as_set(args.audience) or None)
    if args.json:
        print(json.dumps({"query": args.query, "hits": hits}, ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print(f"（无命中）{args.query}")
        return 0
    print(f"查询：{args.query}    命中 {len(hits)} 条\n")
    for i, h in enumerate(hits, 1):
        print(f"{i}. [{h['score']:.3f}] {h['title']}")
        print(f"   id={h['id']}  type={h['type']}  visibility={h['visibility']}"
              f"  scope={'/'.join(h.get('scopes') or [])}")
        if h["heading"]:
            print(f"   §{h['heading']}")
        snippet = re.sub(r"\s+", " ", h["snippet"]).strip()
        print(f"   {snippet[:160]}")
        print()
    return 0


def cmd_stats(args) -> int:
    pack = load_pack(args.pack or default_pack())
    lens = [len(c["text"]) for c in pack.get("chunks", [])]
    print(f"语料     {pack['corpus']}")
    print(f"生成时间 {pack['generated_at']}")
    print(f"条目     {pack['count']}")
    print(f"段落     {len(lens)}   平均 {sum(lens) // max(len(lens), 1)} 字   最长 {max(lens) if lens else 0}")
    vis = Counter(e.get("visibility") for e in pack.get("entries", []))
    print(f"可见性   {dict(vis)}")
    sc = Counter()
    for e in pack.get("entries", []):
        for s in (e.get("scopes") or ["general"]):
            sc[s] += 1
    if sc:
        print(f"领域     {dict(sc.most_common())}")
    au = Counter()
    for e in pack.get("entries", []):
        for a in (e.get("audiences") or []):
            au[a] += 1
    if au:
        print(f"可听对象 {dict(au.most_common())}")
    rd = pack.get("redactions") or []
    if rd:
        kinds = Counter()
        for r in rd:
            kinds.update(r.get("kinds") or {})
        print(f"脱敏     {sum(r['count'] for r in rd)} 处 / {len(rd)} 条  {dict(kinds.most_common())}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="知识检索层")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="构建索引包")
    p.add_argument("--kb", help="个人 KB 根目录（默认本文件上一级）")
    p.add_argument("--flat", help="扁平语料目录（法律条文等），给了就忽略 --kb")
    p.add_argument("--out", help="输出路径（默认 ~/data/kb/kb-pack.json）")
    p.add_argument("--include-secret", action="store_true", help="调试用：把 secret 条目也收进来")
    p.add_argument("--redact", choices=["mask", "warn", "off"], default="mask",
                   help="字段级脱敏：mask=构建期替换（默认）/ warn=只统计 / off=不处理")
    p.add_argument("--redact-min-severity", choices=["low", "medium", "high"],
                   default="medium", help="脱敏门槛，默认 medium（住址等 low 只提醒不改）")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("search", help="查询")
    p.add_argument("query")
    p.add_argument("--pack", help="索引包路径")
    p.add_argument("--top", type=int, default=DEFAULT_TOP_K)
    p.add_argument("--min-score", type=float, default=0.0)
    p.add_argument("--scope", help="只看这些领域（逗号分隔，如 career,tech）")
    p.add_argument("--audience", help="模拟听众（self/friend/client/recruiter/public）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("stats", help="看索引包概况")
    p.add_argument("--pack")
    p.set_defaults(func=cmd_stats)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
