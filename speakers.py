"""声纹库：把盲聚类的 `Speaker 1/2/3` 变成跨会议稳定的人名。

设计要点
--------
1. **多模板 + 信道标签**：同一个人用手机免提、耳机麦、会议室麦说话，声纹是三个样子。
   每人最多存 `MAX_TEMPLATES` 条模板，比对时取「该人所有模板的最高分」。
2. **分信道阈值**：电话窄带（8k、二次编码）两人声纹只差 0.05 量级，
   用近场的阈值会大面积误认。所以窄带的自动阈值反而更高，多数情况只给「候选」，
   强制人工点一次确认。
3. **三档结论**：`auto`（直接命名）/ `candidate`（给候选，等人工选）/ `unknown`（新人）。
4. **写盘原子化 + 0600**：声纹属敏感个人信息，只存本地、不落日志、不外传。

存储格式（`data/speakers.json`）
------------------------------
```json
{"version": 1, "people": {"张三": {"created": "...", "templates": [
  {"emb": [...192 floats...], "channel": "phone", "source": "processed/xxx", "created": "...", "duration": 95.2}
]}}}
```
192 维 × 4 字节 ≈ 768 B/条，100 人 × 5 条 ≈ 400 KB，一个 JSON 足够，不需要数据库。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

# 信道 → 阈值。auto 必须高于 cand；窄带的 auto 故意设高，避免"自动认错人"
#
# 2026-10-19 实测重标（tests/emb_diag3.py）：
#   同一录音自比 0.96（提取稳定）｜同语域跨录音 ≈0.56｜跨语域（念稿 vs 对话）≈0.30｜异人 0.11。
#   旧值 cand=0.56 正好压在同人分数上，导致每次新录音都掉成 unknown、被迫重新认领。
#   cand 定 0.28：跨语域的 0.30 还能给出「候选」，异人 0.11 仍差 0.17。
#   auto 保持 0.50：只有同语域（≥0.56）才自动命名，跨语域必须人确认一次。
CHANNEL_THRESHOLDS: dict[str, dict[str, float]] = {
    "close": {"auto": 0.50, "cand": 0.28},
    "far": {"auto": 0.48, "cand": 0.26},
    "phone": {"auto": 0.56, "cand": 0.32},
    "unknown": {"auto": 0.50, "cand": 0.28},
}
CHANNELS = tuple(CHANNEL_THRESHOLDS)

# 自动命名还要求「第一名比第二名高多少」。声纹库里人一多，两个人分数咬得很紧时
# 光看绝对值会认错，这里强制留出间隔，不够就降级成候选让人确认。
AUTO_MARGIN = 0.10

# 场景/语域维度：同一个人闲聊、开会念稿、说方言，声纹不一样。
# 场景**只能人工标注**（无标签聚类分不出"这是方言"），所以这里只做存储与匹配维度。
PROFILE_DEFAULT = "默认"
PROFILE_MAX_LEN = 20


def normalize_profile(value: str | None) -> str:
    """场景名归一化：去空白、限长，空值落到「默认」。"""
    text = (value or "").strip()
    if not text:
        return PROFILE_DEFAULT
    return text[:PROFILE_MAX_LEN]


def template_profile(template: dict) -> str:
    """读模板的场景；老数据没有这个字段，一律视为「默认」。"""
    return normalize_profile(template.get("profile"))

MAX_TEMPLATES = 5
# 单条模板太短（<1.2s）声纹不稳，不建议入库
MIN_ENROLL_SEC = 1.2
# 与已有模板相似度高于该值视为同一条，替换而不是追加（防止反复确认把库撑爆）
DUP_TEMPLATE_SIM = 0.96


def _norm(vec) -> np.ndarray:
    arr = np.asarray(vec, dtype="float32").reshape(-1)
    n = float(np.linalg.norm(arr))
    return arr / n if n > 0 else arr


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def rewrite_transcript_labels(folder: Path | str, mapping: dict[int, str],
                              aliases: dict[int, list[str]] | None = None,
                              display: dict[int, str] | None = None) -> bool:
    """把 transcript.md 里的说话人标签换成人名。

    这里不能整篇替换人名——「我」这种标签在正文里到处都是。所以只动
    **标签位**（`**XXX**：`）和抬头那几个固定位置。

    aliases：cluster → 该簇在稿子里现用的名字（双声道模式下是「我 / 对方」，
    单声道模式下是「Speaker 1」），认领时要把这些旧名字一起换掉。
    """
    folder = Path(folder)
    md = folder / "transcript.md"
    if not md.is_file() or not mapping:
        return False
    text = md.read_text(encoding="utf-8")
    for cluster, name in mapping.items():
        olds = {f"Speaker {int(cluster) + 1}"}
        olds.update((aliases or {}).get(int(cluster), []) or [])
        for old in olds:
            if not old or old == name:
                continue
            # 正文里的标签位：**[00:00:03] 我**：
            text = text.replace(f"] {old}**：", f"] {name}**：")
            # 双声道模式下也可能没有时间戳前缀
            text = text.replace(f"**{old}**：", f"**{name}**：")
            # 抬头里的名单（只在像是「Speaker N」时替换，避免误伤正文）
            if old.startswith("Speaker "):
                text = text.replace(f"{old}、", f"{name}、")
                text = text.replace(f"{old}\n", f"{name}\n")

    # 抬头那行名单也一起刷新，免得正文改了、摘要还写着旧名字
    if display and "> 说话人：" in text:
        line = "、".join(display[c] for c in sorted(display))
        import re

        text = re.sub(r"^> 说话人：.*$", f"> 说话人：{line}", text, flags=re.M)

    md.write_text(text, encoding="utf-8")
    return True


def claim_cluster(
    folder: Path | str,
    store: "SpeakerStore",
    cluster: int,
    name: str,
    channel: str | None = None,
    rewrite: bool = True,
    profile: str | None = None,
) -> dict:
    """把某份归档里的一个簇认领成 `name`（可指定场景 profile）。

    设计要点：**用簇质心入库，不用单个窗口**。质心是同一簇所有窗口的平均，
    抖动小；单窗口容易把这一句的口音/噪声当成特征。

    profile 决定这条声纹落在哪个场景下：不填＝「默认」，填「开会」「方言」等
    则作为同一人的另一套声纹。合成音色也按同一套场景名分目录。
    """
    folder = Path(folder)
    path = folder / "speakers.json"
    if not path.is_file():
        raise ValueError("目录里没有 speakers.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("assignments") or []
    target = next((a for a in items if int(a.get("cluster", -1)) == int(cluster)), None)
    if target is None:
        raise ValueError(f"没有 cluster={cluster}，可选：{[a.get('cluster') for a in items]}")
    emb = target.get("embedding") or []
    if not emb:
        raise ValueError("这个簇没有声纹向量，无法入库（可能是纯静音段）")

    name = (name or "").strip()
    if not name:
        raise ValueError("人名不能为空")

    prof = normalize_profile(profile)
    ch = (channel or data.get("channel") or "unknown").lower()
    result = store.add(
        name,
        emb,
        channel=ch,
        source=str(folder),
        duration=float(target.get("duration") or 0),
        allow_short=True,  # 认领时人手确认过，短样本也收
        profile=prof,
    )

    target["name"] = name
    target["profile"] = prof
    target["status"] = "confirmed"
    target["score"] = 1.0
    data.setdefault("names", {})[str(int(cluster))] = name
    data.setdefault("profiles", {})[str(int(cluster))] = prof
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    mapping = {int(a["cluster"]): a["name"] for a in items if a.get("name")}

    # 双声道模式下稿子里的标签是「我 / 对方」，不是「Speaker N」；
    # 从 transcript.json 的 speaker_names 取回旧标签，认领时一起换掉。
    aliases: dict[int, list[str]] = {}
    old_names: dict[str, str] = {}
    seg_file = folder / "transcript.json"
    if seg_file.is_file():
        try:
            seg = json.loads(seg_file.read_text(encoding="utf-8"))
            old_names = seg.get("speaker_names") or {}
            for key, val in old_names.items():
                aliases[int(key)] = [str(val)]
        except Exception:
            pass

    display = {}
    profiles_out = {}
    for a in items:
        c = int(a["cluster"])
        base = a.get("name") or old_names.get(str(c)) or a.get("label") or f"Speaker {c + 1}"
        p = normalize_profile(a.get("profile"))
        profiles_out[str(c)] = p
        # 抬头里带上场景，正文标签保持纯人名（否则下次认领匹配不上）
        display[c] = f"{base}（{p}）" if (p != PROFILE_DEFAULT and a.get("name")) else base

    rewritten = rewrite_transcript_labels(folder, mapping, aliases, display) if rewrite else False

    # 场景写回 transcript.json，前端和下游合成都能读到
    if seg_file.is_file():
        try:
            seg = json.loads(seg_file.read_text(encoding="utf-8"))
            merged = {**profiles_out, **(seg.get("speaker_profiles") or {})}
            seg["speaker_profiles"] = merged
            seg_file.write_text(json.dumps(seg, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    return {
        "cluster": int(cluster),
        "name": name,
        "channel": ch,
        "profile": prof,
        "added": bool(result.get("added")),
        "reason": result.get("reason", ""),
        "templates": result.get("templates", 0),
        "profiles": store.profiles(name),
        "rewritten": rewritten,
    }


@dataclass
class Candidate:
    name: str
    score: float
    channel: str = ""
    profile: str = PROFILE_DEFAULT


@dataclass
class Assignment:
    """一个聚类簇的识别结论。"""

    cluster: int
    name: str | None
    score: float
    status: str  # auto | candidate | unknown
    candidates: list[Candidate] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    duration: float = 0.0
    windows: int = 0
    profile: str = PROFILE_DEFAULT
    margin: float = 0.0

    @property
    def label(self) -> str:
        return self.name or f"Speaker {self.cluster + 1}"

    def to_dict(self, with_embedding: bool = True) -> dict:
        out = {
            "cluster": self.cluster,
            "name": self.name,
            "label": self.label,
            "score": round(self.score, 4),
            "status": self.status,
            "profile": self.profile,
            "margin": round(self.margin, 4),
            "duration": round(self.duration, 1),
            "windows": self.windows,
            "candidates": [
                {"name": c.name, "score": round(c.score, 4),
                 "channel": c.channel, "profile": c.profile}
                for c in self.candidates
            ],
        }
        if with_embedding:
            out["embedding"] = [round(float(x), 5) for x in self.embedding]
        return out


class SpeakerStore:
    """声纹库。线程内安全；跨进程靠「先读后整体替换」的原子写降低冲突概率。"""

    def __init__(self, path: Path | str, max_templates: int = MAX_TEMPLATES):
        self.path = Path(path)
        self.max_templates = max_templates
        self._lock = threading.RLock()
        self._data: dict = self._load()

    # ---------------- 读写 ----------------
    def _load(self) -> dict:
        if not self.path.is_file():
            return {"version": 1, "people": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            # 坏了也不能让主流程挂掉：另存一份残骸，从空库继续
            broken = self.path.with_suffix(f".broken-{datetime.now():%Y%m%d%H%M%S}.json")
            try:
                self.path.replace(broken)
            except OSError:
                pass
            return {"version": 1, "people": {}}
        if not isinstance(data, dict) or "people" not in data:
            return {"version": 1, "people": {}}
        return data

    def reload(self) -> None:
        with self._lock:
            self._data = self._load()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    # ---------------- 查询 ----------------
    @property
    def people(self) -> dict:
        return self._data.get("people", {})

    def names(self) -> list[str]:
        return sorted(self.people)

    def template_count(self, name: str) -> int:
        return len(self.people.get(name, {}).get("templates", []))

    def summary(self) -> list[dict]:
        rows = []
        for name, person in sorted(self.people.items()):
            temps = person.get("templates", [])
            rows.append({
                "name": name,
                "templates": len(temps),
                "channels": sorted({t.get("channel", "unknown") for t in temps}),
                "profiles": sorted({template_profile(t) for t in temps}),
                "created": person.get("created", ""),
                "last": max((t.get("created", "") for t in temps), default=""),
            })
        return rows

    def profiles(self, name: str) -> list[str]:
        """某人已有哪些场景（按模板里出现过的 profile 归并）。"""
        temps = self.people.get(name, {}).get("templates", [])
        return sorted({template_profile(t) for t in temps})

    # ---------------- 写入 ----------------
    def add(
        self,
        name: str,
        embedding,
        channel: str = "unknown",
        source: str = "",
        duration: float = 0.0,
        allow_short: bool = False,
        profile: str = PROFILE_DEFAULT,
    ) -> dict:
        """登记一条声纹模板。返回 {"added": bool, "reason": str, "templates": n}。

        profile 是场景/语域（默认 / 日常 / 开会 / 方言…）。同一人同一场景同一信道
        才算重复；不同场景各存一条，比对时取全部模板的最高分。
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("人名不能为空")
        if channel not in CHANNEL_THRESHOLDS:
            channel = "unknown"
        profile = normalize_profile(profile)
        if duration and duration < MIN_ENROLL_SEC and not allow_short:
            raise ValueError(f"样本过短（{duration:.1f}s < {MIN_ENROLL_SEC}s），声纹不可靠")

        emb = _norm(embedding)
        with self._lock:
            person = self.people.setdefault(name, {"created": _now(), "templates": []})
            temps = person.setdefault("templates", [])

            # 已有近似模板 → 替换（同一场景反复入库不该堆成一堆）
            for i, t in enumerate(temps):
                same_slot = t.get("channel") == channel and template_profile(t) == profile
                if same_slot and float(_norm(t["emb"]) @ emb) >= DUP_TEMPLATE_SIM:
                    temps[i] = {
                        "emb": [round(float(x), 5) for x in emb],
                        "channel": channel,
                        "profile": profile,
                        "source": source,
                        "created": _now(),
                        "duration": round(float(duration), 1),
                    }
                    self._save()
                    return {"added": False, "reason": "已存在高度相似的模板，已更新",
                            "templates": len(temps), "profile": profile}

            temps.append({
                "emb": [round(float(x), 5) for x in emb],
                "channel": channel,
                "profile": profile,
                "source": source,
                "created": _now(),
                "duration": round(float(duration), 1),
            })
            # 超出上限：先淘汰「同场景同信道」里最旧的一条，避免把刚建的新场景挤掉
            if len(temps) > self.max_templates:
                same = [t for t in temps if t.get("channel") == channel
                        and template_profile(t) == profile]
                victim = min(same, key=lambda t: t.get("created", "")) if len(same) > 1 \
                    else min(temps, key=lambda t: t.get("created", ""))
                temps.remove(victim)
                reason = f"超出 {self.max_templates} 条上限，删除最旧模板（{victim.get('created','')}）"
            else:
                reason = "已登记"
            self._save()
            return {"added": True, "reason": reason, "templates": len(temps), "profile": profile}

    def remove_person(self, name: str) -> bool:
        with self._lock:
            if name not in self.people:
                return False
            self.people.pop(name)
            self._save()
            return True

    def remove_template(self, name: str, index: int) -> bool:
        with self._lock:
            temps = self.people.get(name, {}).get("templates", [])
            if not 0 <= index < len(temps):
                return False
            temps.pop(index)
            if not temps:
                self.people.pop(name, None)
            self._save()
            return True

    def rename(self, old: str, new: str) -> bool:
        with self._lock:
            if old not in self.people or not new:
                return False
            if new in self.people:  # 合并两人
                self.people[new].setdefault("templates", []).extend(self.people[old]["templates"])
                self.people[new]["templates"] = self.people[new]["templates"][-self.max_templates:]
            else:
                person = self.people.pop(old)
                self.people[new] = person
            self.people.pop(old, None)
            self._save()
            return True

    # ---------------- 比对 ----------------
    def scores(self, embedding, channel: str = "unknown") -> list[Candidate]:
        """对每个人的所有模板取最高分（跨场景、跨信道一起比）。"""
        emb = _norm(embedding)
        rows: list[Candidate] = []
        for name, person in self.people.items():
            best, best_channel, best_profile = -1.0, "", PROFILE_DEFAULT
            for t in person.get("templates", []):
                sim = float(_norm(t["emb"]) @ emb)
                if sim > best:
                    best, best_channel = sim, t.get("channel", "unknown")
                    best_profile = template_profile(t)
            if best >= 0:
                rows.append(Candidate(name=name, score=best, channel=best_channel,
                                      profile=best_profile))
        rows.sort(key=lambda c: c.score, reverse=True)
        return rows

    def identify(
        self,
        cluster: int,
        embedding,
        channel: str = "unknown",
        duration: float = 0.0,
        windows: int = 0,
    ) -> Assignment:
        channel = channel if channel in CHANNEL_THRESHOLDS else "unknown"
        thr = CHANNEL_THRESHOLDS[channel]
        rows = self.scores(embedding, channel)
        emb_list = [float(x) for x in _norm(embedding)]

        if not rows:
            return Assignment(cluster, None, 0.0, "unknown", [], emb_list, duration, windows)

        top = rows[0]
        second = rows[1].score if len(rows) > 1 else -1.0
        margin = top.score - second
        # 分数够高还不够，必须和第二名拉开距离，否则两个人咬太紧，宁可让人确认
        if top.score >= thr["auto"] and margin >= AUTO_MARGIN:
            status, name = "auto", top.name
        elif top.score >= thr["cand"]:
            status, name = "candidate", None
        else:
            status, name = "unknown", None
        return Assignment(
            cluster=cluster,
            name=name,
            score=top.score,
            status=status,
            candidates=rows[:3],
            embedding=emb_list,
            duration=duration,
            windows=windows,
            profile=top.profile,
            margin=margin,
        )

    def identify_many(self, items, channel: str = "unknown") -> list[Assignment]:
        """items: [(cluster, embedding, duration, windows), ...]"""
        out = []
        for item in items:
            cluster, embedding, duration, windows = (list(item) + [0, 0])[:4]
            out.append(self.identify(int(cluster), embedding, channel, float(duration or 0), int(windows or 0)))
        return out


def channel_for_audio(sample_rate: int | None, bitrate_kbps: float | None = None) -> str:
    """从原始音频参数粗判信道类型。仅作默认值，可被 CLI 覆盖。"""
    if sample_rate and sample_rate <= 8000:
        return "phone"
    if bitrate_kbps and bitrate_kbps <= 40:
        return "phone"
    if sample_rate and sample_rate >= 44100:
        return "close"
    return "unknown"
