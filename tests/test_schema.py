"""落盘结构契约（schema/）自检：能拦住坏数据、不误伤好数据、且真的接上了。

为什么值得单独一个套件
----------------------
「加了校验」和「校验生效了」是两件事。最常见的失败形态是：schema 文件写得很漂亮，
但写入点根本没调用它；或者调用了，却因为异常被外层 `except Exception` 吞掉，
于是数据坏了半年都没人知道。两种情况下测试如果只断言「能过」，全是绿的。

所以这里的重点是两组断言：
  · 反向：删掉 meeting_id / 写错 kind / 状态值乱填 —— 必须抛，且消息里出现字段名
  · 接线：pipeline.py 与 costs.py 的源码里必须真的出现校验调用（改回不校验就红）

测试方式遵照本仓铁律：直接 import 被测模块（schema_guard 本身就是真源），
不抄一份校验逻辑到测试里——抄的那份永远是对的，改坏了源文件也不报。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import schema_guard as G  # noqa: E402

PASS = FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  ✗    {name}\n      得到 {got!r}\n      期望 {want!r}")


def check_true(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  ✗    {name}  {detail}")


def check_raises(name: str, func, must_contain: tuple[str, ...] = ()) -> None:
    """反向断言：必须抛出，且消息里必须出现指定字串（证明报的是那个字段）。"""
    global PASS, FAIL
    try:
        func()
    except Exception as exc:  # noqa: BLE001 - 就是要抓全部
        text = str(exc)
        missing = [m for m in must_contain if m not in text]
        if missing:
            FAIL += 1
            print(f"  ✗    {name}  抛了但没提到 {missing}：{text[:200]}")
        else:
            PASS += 1
            print(f"  ok   {name}")
        return
    FAIL += 1
    print(f"  ✗    {name}  本该抛异常，却通过了（这就是假绿）")


# --------------------------------------------------------------------------- #
# 与生产代码完全一致的样本
# --------------------------------------------------------------------------- #
def real_meta(**over) -> dict:
    """照抄 pipeline.archive() 里 meta 的构造（含 extra 的全部字段）。"""
    meta = {
        "meeting_id": "M20260101-090000-aaaa",
        "audio": "周会_622e.m4a",
        "sha256": "622e" + "0" * 60,
        "size": 12345678,
        "title": "周会",
        "owner": "me",
        "archived_at": "2026-10-19T14:32:33",
        "duration": 1832.5,
        "attempts": 1,
        "dual": True,
        "assistant_events": [{"at_ms": 1200, "kind": "answer", "text": "已记录"}],
        "mic": "mic",
        "elapsed_sec": 412.7,
    }
    meta.update(over)
    return meta


def real_state(**entry_over) -> dict:
    entry = {
        "status": "done",
        "file": "周会.m4a",
        "out_dir": "/home/alice/data/tenants/me/processed/2026-10-19_周会",
        "attempts": 1,
        "duration": 1832.5,
        "last_error": "",
        "next_retry": 0,
        "updated": "2026-10-19T14:32:33",
    }
    entry.update(entry_over)
    return {"622e" + "0" * 60: entry}


def real_usage(kind: str = "asr") -> dict:
    """照抄 costs.log() 的公共字段 + 各 kind 的计费字段（见 costs.asr/tts/llm_cost）。"""
    base = {
        "ts": "2026-10-19T14:32:33",
        "kind": kind,
        "ref": "M20260101-090000-aaaa",
        "scene": "archive",
        "owner": "me",
    }
    if kind == "asr":
        base.update({
            "vendor": "local", "model": "paraformer-zh", "audio_sec": 1832.5,
            "compute_sec": 400.2, "cost_yuan": 0.0, "fair_yuan": 1.83,
            "electricity_yuan": 0.05,
        })
    elif kind == "tts":
        base.update({
            "vendor": "local", "model": "cosyvoice2", "chars": 124,
            "text_chars": 62, "cached": False, "cost_yuan": 0.0,
            "fair_yuan": 0.12, "electricity_yuan": 0.01,
        })
    else:
        base.update({
            "vendor": "deepseek", "model": "deepseek-chat", "prompt_tokens": 3200,
            "completion_tokens": 800, "cost_yuan": 0.004, "fair_yuan": 0.004,
        })
    return base


# --------------------------------------------------------------------------- #
def main() -> int:
    print("1) schema 文件本身")
    for name, fname in G.SCHEMAS.items():
        path = ROOT / "schema" / fname
        check_true(f"{fname} 存在", path.is_file(), str(path))
        if path.is_file():
            spec = json.loads(path.read_text(encoding="utf-8"))
            check_true(f"{fname} 有 $schema", "$schema" in spec)
            check_true(f"{fname} 顶层是 object", spec.get("type") == "object",
                       str(spec.get("type")))
    check("SCHEMAS 登记了 3 类", len(G.SCHEMAS), 3)

    print("\n2) 生产形态必须通过（不能误伤）")
    check("完整 meta（archive 新建）", G.violations("meta", real_meta()), [])
    check("meta 允许额外字段（预算/来源等后续扩展）",
          G.violations("meta", real_meta(extra_future_field=1)), [])
    check("meta 允许空 owner（早期归档没这个字段）",
          G.violations("meta", real_meta(owner="")), [])
    check("meta 允许 meeting_id 尾段为空（refresh_archive 的既存不一致）",
          G.violations("meta", real_meta(meeting_id="M20260101-090000-")), [])
    for st in ("pending", "done", "failed"):
        check(f"state entry status={st}", G.violations("state", real_state(status=st)), [])
    check("state 空字典（还没处理过任何文件）", G.violations("state", {}), [])
    for kind in ("llm", "asr", "tts"):
        check(f"usage kind={kind}", G.violations("usage", real_usage(kind)), [])

    print("\n3) 反向：坏数据必须被拦住，且报出字段名")
    bad = real_meta()
    del bad["meeting_id"]
    check_raises("meta 删掉 meeting_id → fail-fast 且报字段名",
                 lambda: G.validate("meta", bad), ("meeting_id",))
    check_raises("meta meeting_id 格式乱写",
                 lambda: G.validate("meta", real_meta(meeting_id="abc")),
                 ("meeting_id", "不匹配"))
    check_raises("meta sha256 不是 64 位",
                 lambda: G.validate("meta", real_meta(sha256="deadbeef")), ("sha256",))
    check_raises("meta audio 空串",
                 lambda: G.validate("meta", real_meta(audio="")), ("audio", "长度"))
    check_raises("meta size 写成字符串",
                 lambda: G.validate("meta", real_meta(size="123")), ("size", "integer"))
    check_raises("meta size 为负",
                 lambda: G.validate("meta", real_meta(size=-1)), ("size", "不能小于"))
    check_raises("meta 顶层写成 list",
                 lambda: G.validate("meta", [real_meta()]), ("$", "object"))
    check_raises("state status 乱填",
                 lambda: G.validate("state", real_state(status="finished")),
                 ("status", "取值必须是"))
    check_raises("state entry 缺 status",
                 lambda: G.validate("state", {"622e" + "0" * 60: {"file": "x"}}),
                 ("status",))
    check_raises("usage kind 乱填",
                 lambda: G.validate("usage", real_usage("llm2")), ("kind", "取值必须是"))
    del_ts = real_usage()
    del del_ts["ts"]
    check_raises("usage 删掉 ts → 报字段名", lambda: G.validate("usage", del_ts), ("ts",))
    check_raises("usage ts 不是 ISO",
                 lambda: G.validate("usage", real_usage() | {"ts": "2026/10/19"}),
                 ("ts", "不匹配"))
    check_raises("usage chars 为负",
                 lambda: G.validate("usage", real_usage("tts") | {"chars": -1}),
                 ("chars", "不能小于"))

    print("\n4) 校验器自身的防线（不能悄悄放行）")
    check_raises("未知 schema 名 → 报错而不是当作通过",
                 lambda: G.validate("metaa", {}), ("未登记",))
    check("bool 不算 integer", G._type_ok(True, "integer"), False)
    check("int 不算 string", G._type_ok(1, "string"), False)
    check("float 算 number", G._type_ok(1.5, "number"), True)
    with tempfile.TemporaryDirectory() as td:
        # 把一个「本校验器不支持」的关键字塞进 schema，必须报错而不是静默忽略
        fake_dir = Path(td) / "schema"
        fake_dir.mkdir()
        (fake_dir / "meta.schema.json").write_text(
            json.dumps({"type": "object", "$ref": "#/x"}), encoding="utf-8"
        )
        old_dir, old_cache = G.SCHEMA_DIR, dict(G._cache)
        G.SCHEMA_DIR, G._cache = fake_dir, {}
        try:
            check_raises("schema 用了 $ref → 报「不支持」",
                         lambda: G.validate("meta", {}), ("不支持",))
        finally:
            G.SCHEMA_DIR, G._cache = old_dir, old_cache

    print("\n5) 接线断言：写入点真的调了校验（改回不校验这里就红）")
    pipe_src = (ROOT / "pipeline.py").read_text(encoding="utf-8")
    costs_src = (ROOT / "costs.py").read_text(encoding="utf-8")
    check_true("archive() 新建 meta 走严格校验",
               'write_json(target / "meta.json", meta, schema="meta")' in pipe_src)
    check_true("state 走宽松校验（strict=False，写失败比脏数据更糟）",
               'schema="state", strict=False' in pipe_src)
    check_true("costs.log() 校验每条用量记录",
               'validate("usage", record)' in costs_src)

    # 顺序也是契约的一部分：先校验、后动盘。反过来的话校验失败会留下
    # 「有音频没 meta」的半成品目录，下次重跑被判成重复归档。
    archive_body = pipe_src[pipe_src.index("def archive("):]
    archive_body = archive_body[: archive_body.index("\ndef ", 1)]
    i_check = archive_body.find('validate_schema("meta", meta)')
    i_mkdir = archive_body.find("target.mkdir(")
    i_move = archive_body.find("shutil.move(")
    check_true("archive() 里能找到校验调用", i_check > 0)
    check_true("校验在 mkdir 之前", 0 < i_check < i_mkdir, f"check={i_check} mkdir={i_mkdir}")
    check_true("校验在搬音频之前", 0 < i_check < i_move, f"check={i_check} move={i_move}")

    print("\n6) write_json 的严格/宽松两种行为")
    from utils import read_json, write_json

    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "good.json"
        write_json(good, real_meta(), schema="meta")
        check("严格模式好数据写成功", read_json(good, None) is not None, True)

        bad_path = Path(td) / "bad.json"
        bad_meta = real_meta()
        del bad_meta["meeting_id"]
        check_raises("严格模式坏数据：抛异常且盘点未落盘",
                     lambda: write_json(bad_path, bad_meta, schema="meta"), ("meeting_id",))
        check("抛了就不该留下文件（原子写没执行）", bad_path.exists(), False)

        lax = Path(td) / "lax.json"
        write_json(lax, {"digest1": {"status": "坏值"}}, schema="state", strict=False)
        check("宽松模式坏数据仍然落盘（不阻断）", read_json(lax, None) is not None, True)

        plain = Path(td) / "plain.json"
        write_json(plain, {"任意": ["结构", 1]})
        check("不传 schema 时行为与以前一致（不做任何校验）",
              read_json(plain, None), {"任意": ["结构", 1]})

    print("\n7) 体检工具端到端：造坏数据，看它能不能报出来")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "tenants"
        ok_dir = root / "me" / "processed" / "2020-01-01_archive-a"
        ok_dir.mkdir(parents=True)
        (ok_dir / "meta.json").write_text(
            json.dumps(real_meta(), ensure_ascii=False), encoding="utf-8"
        )
        bad_dir = root / "me" / "processed" / "2020-01-02_archive-b"
        bad_dir.mkdir(parents=True)
        broken = real_meta()
        broken["meeting_id"] = "not-an-id"
        (bad_dir / "meta.json").write_text(
            json.dumps(broken, ensure_ascii=False), encoding="utf-8"
        )
        (root / "me" / "work").mkdir(parents=True)
        (root / "me" / "work" / "state.json").write_text(
            json.dumps(real_state(), ensure_ascii=False), encoding="utf-8"
        )
        (Path(td) / "usage.jsonl").write_text(
            json.dumps(real_usage("asr"), ensure_ascii=False) + "\n", encoding="utf-8"
        )

        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "check-schemas.py"),
             "--tenants", str(root), "--json"],
            capture_output=True, text=True, encoding="utf-8",
            # Windows 控制台默认 cp936，子进程会把中文按 GBK 吐出来，这里强制 UTF-8
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        check("有坏数据时退出码为 1", proc.returncode, 1)
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = {}
        check("ok=false", payload.get("ok"), False)
        check("meta 检查了 2 个", payload.get("meta", {}).get("checked"), 2)
        joined = " ".join(payload.get("meta", {}).get("problems") or [])
        check_true("问题清单点名了 archive-b", "2020-01-02_archive-b" in joined, joined[:200])
        check_true("问题清单点名了 meeting_id", "meeting_id" in joined, joined[:200])
        check_true("archive-a 没被误报", "2020-01-01_archive-a" not in joined)
        check("state 检查了 1 个", payload.get("state", {}).get("checked"), 1)
        check("state 全部合规", payload.get("state", {}).get("problems"), [])
        check("usage 检查了 1 行", payload.get("usage_rows", {}).get("checked"), 1)
        check("usage 全部合规", payload.get("usage_rows", {}).get("problems"), [])

    print("\n" + "=" * 40)
    total = PASS + FAIL
    print(f"通过 {PASS} 项，失败 {FAIL} 项（共 {total} 项）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
