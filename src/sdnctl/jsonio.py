"""JSON in and out for the API types: a generic dataclass codec plus message dispatch.

Data errors (wrong JSON shape, unknown or missing fields, bad enum values) raise ValueError
with a path such as ``$.lanes[3].module``. Frozensets are written as sorted lists, so the
same value always gives the same JSON text.
"""

import dataclasses
import functools
import json
import types
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from sdnctl.messages import MESSAGE_CLASSES, Message
from sdnctl.types import MsgType

T = TypeVar("T")
_NONE = type(None)


def to_jsonable(obj: Any) -> Any:
    """Convert an API value to plain JSON data (dict, list, str, int, float, bool, None)."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be str, got {type(key).__name__}")
            out[key] = to_jsonable(value)
        return out
    if isinstance(obj, frozenset):
        return sorted((to_jsonable(x) for x in obj), key=_canonical)
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    raise TypeError(f"cannot convert {type(obj).__name__} to JSON")


def from_jsonable(tp: type[T], data: Any) -> T:
    """Rebuild a value of type tp from JSON data written by to_jsonable."""
    return cast(T, _decode(tp, data, "$"))


def encode_message(msg: Message) -> dict[str, Any]:
    """JSON data of one protocol message."""
    return cast(dict[str, Any], to_jsonable(msg))


def decode_message(data: Any) -> Message:
    """Rebuild a protocol message, choosing its class from header.type."""
    obj = _expect(data, dict, "$")
    header = _expect(obj.get("header"), dict, "$.header")
    msg_type = _decode(MsgType, header.get("type"), "$.header.type")
    return cast(Message, _decode(MESSAGE_CLASSES[msg_type], obj, "$"))


def dump_json(obj: Any, path: str | Path, indent: int | None = None) -> None:
    """Write an API value to a JSON file ('\\n' line endings), creating parent folders."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_jsonable(obj), indent=indent) + "\n"
    p.write_text(text, encoding="utf-8", newline="\n")


def load_json(tp: type[T], path: str | Path) -> T:
    """Read a value of type tp from a JSON file written by dump_json."""
    return from_jsonable(tp, json.loads(Path(path).read_text(encoding="utf-8")))


def _canonical(x: Any) -> str:
    return json.dumps(x, sort_keys=True)


def _expect(data: Any, kind: type[T], path: str) -> T:
    if isinstance(data, bool) and kind is not bool:
        raise ValueError(f"{path}: expected {kind.__name__}, got bool")
    if not isinstance(data, kind):
        raise ValueError(f"{path}: expected {kind.__name__}, got {type(data).__name__}")
    return data


def _decode(tp: Any, data: Any, path: str) -> Any:
    if tp is Any:
        return data
    origin = get_origin(tp)
    args = get_args(tp)
    if origin is Union or origin is types.UnionType:
        if data is None and _NONE in args:
            return None
        options = [a for a in args if a is not _NONE]
        if len(options) != 1:
            raise TypeError(f"{path}: only X | None unions are supported, got {tp!r}")
        return _decode(options[0], data, path)
    if origin is tuple:
        items = _expect(data, list, path)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], x, f"{path}[{i}]") for i, x in enumerate(items))
        if len(items) != len(args):
            raise ValueError(f"{path}: expected {len(args)} items, got {len(items)}")
        pairs = enumerate(zip(args, items, strict=True))
        return tuple(_decode(a, x, f"{path}[{i}]") for i, (a, x) in pairs)
    if origin is list:
        items = _expect(data, list, path)
        return [_decode(args[0], x, f"{path}[{i}]") for i, x in enumerate(items)]
    if origin is frozenset:
        items = _expect(data, list, path)
        return frozenset(_decode(args[0], x, f"{path}[{i}]") for i, x in enumerate(items))
    if origin is dict or origin is Mapping:
        obj = _expect(data, dict, path)
        return {
            _decode(args[0], k, f"{path}.<key>"): _decode(args[1], v, f"{path}.{k}")
            for k, v in obj.items()
        }
    if isinstance(tp, type):
        if issubclass(tp, Enum):
            try:
                return tp(data)
            except ValueError:
                raise ValueError(f"{path}: {data!r} is not a {tp.__name__}") from None
        if dataclasses.is_dataclass(tp):
            return _decode_dataclass(tp, data, path)
        if tp is float:
            if isinstance(data, bool) or not isinstance(data, (int, float)):
                raise ValueError(f"{path}: expected float, got {type(data).__name__}")
            return float(data)
        if tp in (bool, int, str):
            return _expect(data, tp, path)
    raise TypeError(f"{path}: unsupported type {tp!r}")


@functools.cache
def _init_fields(cls: Any) -> tuple[tuple[str, Any, bool], ...]:
    """(name, type, required) of each constructor field of a dataclass."""
    hints = get_type_hints(cls)
    return tuple(
        (
            f.name,
            hints[f.name],
            f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING,
        )
        for f in dataclasses.fields(cls)
        if f.init
    )


def _decode_dataclass(cls: Any, data: Any, path: str) -> Any:
    obj = _expect(data, dict, path)
    specs = _init_fields(cls)
    unknown = sorted(set(obj) - {name for name, _, _ in specs})
    if unknown:
        raise ValueError(f"{path}: unknown fields {unknown} for {cls.__name__}")
    kwargs: dict[str, Any] = {}
    for name, tp, required in specs:
        if name in obj:
            kwargs[name] = _decode(tp, obj[name], f"{path}.{name}")
        elif required:
            raise ValueError(f"{path}: missing field {name!r} for {cls.__name__}")
    return cls(**kwargs)
