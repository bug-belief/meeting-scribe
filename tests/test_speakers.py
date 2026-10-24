"""声纹库离线自检：不加载任何模型，纯向量运算。

    python tests/test_speakers.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from speakers import CHANNEL_THRESHOLDS, SpeakerStore  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def unit(vec) -> np.ndarray:
    vec = np.asarray(vec, dtype="float32")
    return vec / np.linalg.norm(vec)


def similar(base, cos: float, seed: int) -> np.ndarray:
    """构造一个与 base 余弦相似度约为 cos 的向量。"""
    rng = np.random.default_rng(seed)
    noise = rng.normal(size=len(base)).astype("float32")
    noise -= noise @ base * base  # 去掉平行分量
    noise /= np.linalg.norm(noise)
    return unit(np.cos(np.arccos(cos)) * base + np.sin(np.arccos(cos)) * noise)


def main() -> int:
    rng = np.random.default_rng(42)
    alice = unit(rng.normal(size=192))
    bob = unit(rng.normal(size=192))
    stranger = unit(rng.normal(size=192))

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "speakers.json"
        store = SpeakerStore(path, max_templates=3)

        print("\n[1] 登记与持久化")
        store.add("阿甲", alice, channel="close", duration=12.0)
        check("文件已写出", path.is_file())
        check("模板数 1", store.template_count("阿甲") == 1)
        reloaded = SpeakerStore(path)
        check("重新加载后仍在", reloaded.template_count("阿甲") == 1)

        print("\n[2] 重复登记不膨胀")
        store.add("阿甲", alice, channel="close", duration=12.0)
        check("同信道同向量 → 替换而非追加", store.template_count("阿甲") == 1)
        store.add("阿甲", alice, channel="phone", duration=20.0)
        check("不同信道 → 新增一条", store.template_count("阿甲") == 2)

        print("\n[3] 识别三档")
        near = similar(alice, 0.85, 1)
        mid = similar(alice, 0.40, 2)
        far = similar(alice, 0.20, 3)

        auto = store.identify(0, near, channel="close", duration=5)
        check("近场 0.85 → auto 且认出阿甲", auto.status == "auto" and auto.name == "阿甲",
              f"实际 {auto.status}/{auto.name}/{auto.score:.2f}")

        cand = store.identify(0, mid, channel="close", duration=5)
        check("近场 0.40 → candidate 且不给名字", cand.status == "candidate" and cand.name is None,
              f"实际 {cand.status}/{cand.score:.2f}")

        unknown = store.identify(0, far, channel="close", duration=5)
        check("近场 0.20 → unknown", unknown.status == "unknown", f"实际 {unknown.status}/{unknown.score:.2f}")

        # 跨语域（念稿 vs 对话）实测只有 0.30：必须给「候选」而不是「未知」，
        # 否则用户每次换场景都要从零认领（2026-10-19 实测）
        cross = store.identify(0, similar(alice, 0.30, 5), channel="close", duration=5)
        check("跨语域 0.30 → candidate（给候选，别判未知）",
              cross.status == "candidate", f"实际 {cross.status}/{cross.score:.2f}")

        # 0.52：高于近场自动线 0.50，低于窄带自动线 0.56 → 同一分数两种信道结论不同
        edge = similar(alice, 0.52, 4)
        close_edge = store.identify(0, edge, channel="close", duration=5)
        phone_edge = store.identify(0, edge, channel="phone", duration=5)
        check("0.52 在近场 → auto", close_edge.status == "auto", f"实际 {close_edge.status}/{close_edge.score:.2f}")
        check("0.52 在窄带 → 只给候选（阈值更高，宁可不认）",
              phone_edge.status == "candidate", f"实际 {phone_edge.status}/{phone_edge.score:.2f}")

        print("\n[4] 陌生人不会被误认")
        other = store.identify(0, stranger, channel="close", duration=5)
        check("随机向量 → unknown", other.status == "unknown", f"实际 {other.score:.2f}")

        print("\n[5] 模板上限")
        for i in range(5):
            store.add("阿乙", similar(bob, 0.5 + i * 0.05, 10 + i), channel="far", duration=5)
        check("超出上限只保留 3 条", store.template_count("阿乙") == 3, f"实际 {store.template_count('阿乙')}")

        print("\n[6] 改名与合并")
        store.add("阿丙", similar(bob, 0.9, 99), channel="far", duration=5)
        before = store.template_count("阿乙") + store.template_count("阿丙")
        store.rename("阿丙", "阿乙")
        after = store.template_count("阿乙")
        check("同名合并后模板合并且不超上限", after == min(before, 3) and "阿丙" not in store.people,
              f"{before} → {after}")

        print("\n[7] 删除")
        check("删单人成功", store.remove_person("阿丙") is False)
        check("删存在的人成功", store.remove_person("阿乙") is True)
        check("删不存在返回 False", store.remove_person("不存在") is False)

        print("\n[8] 损坏文件不拖垮主流程")
        path.write_text("{ 坏掉的 json", encoding="utf-8")
        broken = SpeakerStore(path)
        check("坏文件被隔离并返回空库", broken.names() == [] and any(path.parent.glob("*.broken-*.json")))

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    print("信道阈值：" + "，".join(f"{k}={v['auto']:.2f}/{v['cand']:.2f}" for k, v in CHANNEL_THRESHOLDS.items()))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
