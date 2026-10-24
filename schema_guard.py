"""落盘结构的机器可读契约：加载 `schema/*.schema.json` 并校验数据。

## 为什么自己写而不是 `import jsonschema`

本仓 requirements.txt 的原则是「少一层坑」（音频解码全交给系统 ffmpeg，不装
ffmpeg-python）。jsonschema 会带一条 rpds 的 C 扩展依赖，服务器换 Python 版本
时要重新编译；而我们只用到它能力的一小部分（type / required / pattern / enum /
minimum）。这里 200 行 stdlib 代码把这个子集覆盖掉，零安装、零版本风险。

## 只实现子集，且明确列出

已支持：type / required / properties / additionalProperties(object 或 bool) /
enum / pattern / minimum / maximum / minLength / minItems / items / description(忽略)。

不支持（用到就该报错而不是静默忽略）：$ref / oneOf / anyOf / allOf / patternProperties /
dependentRequired / if-then-else。**遇到不认识的 schema 关键字一律抛错**——宁可当场
发现契约没生效，也不要以为在保护其实在放空。

## 校验时机：只在「新建」处严格，修补处不校验

归档里的 meta.json 有三条写入路径：
  · pipeline.archive()            新建归档   → 严格（strict=True，失败即抛）
  · pipeline._stamp_meeting_id()  给新 meta 补字段 → 不校验（刚写出的东西必然合规）
  · refresh_archive / backfill    修历史数据 → 不校验（老归档本来就缺字段，
                                                卡在这里等于让修补工具彻底不能用）

历史数据的合规性用 `tools/check-schemas.py` 做只读体检，不阻断任何流程。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = BASE_DIR / "schema"

# schema 名 → 文件名。新增一个落盘结构就在这里登记一行，测试会检查文件真实存在。
SCHEMAS = {
    "meta": "meta.schema.json",
    "state": "state.schema.json",
    "usage": "usage.schema.json",
}

# 本校验器不认识的 JSON Schema 关键字。写进 schema 文件就会报错，避免「以为校验了」。
_UNSUPPORTED = (
    "$ref", "oneOf", "anyOf", "allOf", "not", "patternProperties",
    "dependentRequired", "if", "then", "else", "propertyNames",
)

_cache: dict[str, tuple[float, dict]] = {}


class SchemaError(ValueError):
    """落盘结构不合契约。消息里带字段名与 JSON 路径，方便直接定位。"""

    def __init__(self, name: str, problems: list[str]):
        self.name = name
        self.problems = problems
        head = f"{name} 结构校验失败（{len(problems)} 处）："
        super().__init__(head + "\n  " + "\n  ".join(problems))


def schema_path(name: str) -> Path:
    try:
        fname = SCHEMAS[name]
    except KeyError:
        raise SchemaError(name, [f"未登记的 schema：{name!r}（可选：{sorted(SCHEMAS)}）"]) from None
    return SCHEMA_DIR / fname


def load_schema(name: str) -> dict:
    """读 schema 文件。按 mtime 缓存——改了 json 立刻生效，不用重启进程。"""
    path = schema_path(name)
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise SchemaError(name, [f"读不到 schema 文件 {path}：{exc}"]) from None

    hit = _cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]

    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError(name, [f"{path.name} 不是合法 JSON：{exc}"]) from None
    if not isinstance(spec, dict):
        raise SchemaError(name, [f"{path.name} 顶层必须是 object"])

    _cache[name] = (mtime, spec)
    return spec


def clear_cache() -> None:
    _cache.clear()


def _type_ok(value, want: str) -> bool:
    # bool 是 int 的子类，True 不该被当成 integer/number 放行
    if want == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if want == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want == "string":
        return isinstance(value, str)
    if want == "boolean":
        return isinstance(value, bool)
    if want == "array":
        return isinstance(value, list)
    if want == "object":
        return isinstance(value, dict)
    if want == "null":
        return value is None
    return False


def _type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _short(value, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _walk(value, spec: dict, path: str, problems: list[str]) -> None:
    """递归检查。path 用 $ 开头的 JSON 路径，报错时用户能直接找到位置。"""
    for key in _UNSUPPORTED:
        if key in spec:
            problems.append(f"schema 用了本校验器不支持的 {key}（于 {path}）——契约没生效")
            return

    want = spec.get("type")
    if want:
        wants = want if isinstance(want, list) else [want]
        if not any(_type_ok(value, w) for w in wants):
            problems.append(
                f"字段 {path} 应为 {'/'.join(wants)}，实际 {_type_name(value)}：{_short(value)}"
            )
            return  # 类型都不对，后面 length/pattern 再报只会刷屏

    if "enum" in spec and value not in spec["enum"]:
        problems.append(f"字段 {path} 取值必须是 {spec['enum']} 之一，实际 {_short(value)}")

    if "pattern" in spec and isinstance(value, str):
        if not re.search(spec["pattern"], value):
            problems.append(
                f"字段 {path} 不匹配 {spec['pattern']}，实际 {_short(value)}"
            )

    if "minLength" in spec and isinstance(value, str) and len(value) < spec["minLength"]:
        problems.append(
            f"字段 {path} 长度至少 {spec['minLength']}，实际 {len(value)}：{_short(value)}"
        )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in spec and value < spec["minimum"]:
            problems.append(f"字段 {path} 不能小于 {spec['minimum']}，实际 {value}")
        if "maximum" in spec and value > spec["maximum"]:
            problems.append(f"字段 {path} 不能大于 {spec['maximum']}，实际 {value}")

    if isinstance(value, list):
        if "minItems" in spec and len(value) < spec["minItems"]:
            problems.append(f"字段 {path} 至少 {spec['minItems']} 项，实际 {len(value)}")
        item_spec = spec.get("items")
        if isinstance(item_spec, dict):
            for index, item in enumerate(value):
                _walk(item, item_spec, f"{path}[{index}]", problems)

    if isinstance(value, dict):
        for key in spec.get("required") or []:
            if key not in value:
                problems.append(f"缺少必填字段 {key!r}（于 {path}）")

        props = spec.get("properties") or {}
        for key, sub in props.items():
            if key in value:
                _walk(value[key], sub, f"{path}.{key}", problems)

        extra_spec = spec.get("additionalProperties")
        if extra_spec is False:
            for key in value:
                if key not in props:
                    problems.append(f"不允许的字段 {key!r}（于 {path}）")
        elif isinstance(extra_spec, dict):
            for key, item in value.items():
                if key not in props:
                    _walk(item, extra_spec, f"{path}.{key}", problems)


def violations(name: str, data) -> list[str]:
    """只取问题清单，不抛异常。给体检工具与测试用。"""
    problems: list[str] = []
    _walk(data, load_schema(name), "$", problems)
    return problems


def validate(name: str, data) -> None:
    """不合契约就抛 SchemaError（fail-fast，消息里带字段名与路径）。"""
    problems = violations(name, data)
    if problems:
        raise SchemaError(name, problems)
