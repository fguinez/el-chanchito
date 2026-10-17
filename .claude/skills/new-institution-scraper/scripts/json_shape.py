#!/usr/bin/env python3
"""Print the structure of captured JSON or a HAR file, never its values.

For recon: point it at a response saved under `local/` or at a DevTools HAR
export, and it prints each JSON response as keys, types and format hints
(`str:digits`, `str:date-iso`, `str:clp-display`, ...). Only identifier-like
keys and URL path segments are printed as written. Anything that looks like
data (ids, dates, names, e-mails, account numbers) becomes `{id}` or
`{<format>}`. Query strings keep only parameter names, and URLs lose their
credentials and `;params`. So the output can go into notes, a decision record
or a fixture design without leaking data.

Usage:
    json_shape.py local/<slug>-recon/movements.json
    json_shape.py local/<slug>-recon/session.har [--filter movimientos] [--items 3]

Stdlib only. HAR files hold live cookies and often the password: keep them
under `local/` and delete them when recon is done.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union
from urllib.parse import parse_qsl, urlsplit

_FORMATS = [
    ("date-iso", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("datetime-iso", re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")),
    ("date-dmy", re.compile(r"^\d{2}[/-]\d{2}[/-]\d{4}$")),
    ("date-compact", re.compile(r"^(?:19|20)\d{6}$")),
    ("clp-display", re.compile(r"^-?\$\s?-?\d{1,3}(?:\.\d{3})*$")),
    ("usd-display", re.compile(r"^(?:USD|US\$)\s?-?\d{1,3}(?:\.\d{3})*,\d{2}$")),
    ("decimal-comma", re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d+$")),
    ("decimal", re.compile(r"^-?\d+\.\d+$")),
    ("digits", re.compile(r"^-?\d+$")),
    ("rut", re.compile(r"^\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]$")),
    ("account-number", re.compile(r"^\d{2,4}-\d{3,4}-\d{4,8}-\d{1,2}$")),
    ("digit-groups", re.compile(r"^\d+(?:[-\s]\d+)+$")),
    ("masked", re.compile(r"^[*Xx•\s-]{4,}\d{2,4}$")),
    ("uuid", re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")),
    ("hex", re.compile(r"^[0-9a-fA-F]{12,}$")),
    ("email", re.compile(r"^[^@\s]+@[^@\s]+$")),
    ("url", re.compile(r"^https?://")),
    ("empty", re.compile(r"^$")),
]
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$.\-]{0,60}$")
_XSSI = re.compile(r"^\s*(?:\)\]\}',?|while\s*\(1\);|for\s*\(;;\);)")


@dataclass(frozen=True)
class ListShape:
    shortest: int
    longest: int
    item: Any


Shape = Union[str, dict, ListShape]


def _format(text: str) -> str:
    """The format class of a string, without the value."""
    text = text.strip()
    for name, pattern in _FORMATS:
        if pattern.match(text):
            return name
    if len(text) <= 4 and text.isalpha() and text.isupper():
        return "code"
    if " " not in text and _looks_like_id(text):
        return "id-like"
    low = len(text) // 10 * 10
    return f"text({low}-{low + 9} chars)"


def _looks_like_id(text: str) -> bool:
    digits = sum(c.isdigit() for c in text)
    return (
        (digits >= 4 and len(text) >= 6)
        or re.fullmatch(r"(?=[a-fA-F]*\d)[0-9a-fA-F]{12,}", text) is not None
        or (len(text) >= 12 and digits >= 1)
    )


def _name(text: str) -> str:
    """A key or path segment as written when it is a plain identifier, else its class."""
    if _IDENTIFIER.match(text) and not _looks_like_id(text):
        return text
    if _looks_like_id(text) and "@" not in text:
        return "{id}"
    return "{" + _format(text) + "}"


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return f"str:{_format(value)}"
    return type(value).__name__


def _describe(shape: Shape) -> str:
    if isinstance(shape, dict):
        return "{...}"
    if isinstance(shape, ListShape):
        return "list"
    return shape


def _merge(shapes: list[Shape]) -> Shape:
    """One shape for several samples: dicts merge keys (`?` = not always present)."""
    if shapes and all(isinstance(s, dict) for s in shapes):
        keys: dict[str, list[Shape]] = {}
        for shape in shapes:
            for key, value in shape.items():  # type: ignore[union-attr]
                keys.setdefault(key, []).append(value)
        return {
            (key if len(values) == len(shapes) else f"{key}?"): _merge(values)
            for key, values in keys.items()
        }
    if shapes and all(isinstance(s, ListShape) for s in shapes):
        lists: list[ListShape] = shapes  # type: ignore[assignment]
        items = [s.item for s in lists if s.item is not None]
        return ListShape(
            min(s.shortest for s in lists),
            max(s.longest for s in lists),
            _merge(items) if items else None,
        )
    return " | ".join(sorted({_describe(s) for s in shapes}))


def _shape(value: Any, items: int) -> Shape:
    if isinstance(value, dict):
        if not value:
            return "{}"
        grouped: dict[str, list[Shape]] = {}
        for key, inner in value.items():
            grouped.setdefault(_name(str(key)), []).append(_shape(inner, items))
        return {key: _merge(shapes) for key, shapes in grouped.items()}
    if isinstance(value, list):
        sample = [_shape(v, items) for v in value[:items]]
        return ListShape(len(value), len(value), _merge(sample) if sample else None)
    return _scalar(value)


def _list_label(shape: ListShape) -> str:
    if shape.shortest == shape.longest:
        return f"list[{shape.shortest}]"
    return f"list[{shape.shortest}-{shape.longest}]"


def _render(shape: Shape, indent: int, label: str | None = None) -> list[str]:
    """Indented lines for a shape; `label` is the key it hangs under, if any."""
    pad = " " * indent
    head = f"{pad}{label}: " if label is not None else pad
    if isinstance(shape, dict):
        lines = [f"{pad}{label}:"] if label is not None else []
        child_indent = indent + 2 if label is not None else indent
        for key, value in shape.items():
            lines.extend(_render(value, child_indent, key))
        return lines
    if isinstance(shape, ListShape):
        lines = [f"{head}{_list_label(shape)}"]
        if isinstance(shape.item, (dict, ListShape)):
            lines.extend(_render(shape.item, indent + 2, "item"))
        elif shape.item is not None:
            lines.append(f"{pad}  of {shape.item}")
        return lines
    return [f"{head}{shape}"]


def _masked_url(url: str) -> str:
    parts = urlsplit(url)
    path = "/".join(_name(seg.split(";")[0]) if seg else "" for seg in parts.path.split("/"))
    params = sorted({_name(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True)})
    return f"{parts.hostname or ''}{path}" + (f"?{'&'.join(params)}" if params else "")


def _json_body(text: str | None, encoding: str | None = None) -> Any:
    if not text:
        return None
    try:
        if encoding == "base64":
            text = base64.b64decode(text).decode("utf-8", errors="replace")
        return json.loads(_XSSI.sub("", text, count=1))
    except (ValueError, binascii.Error):
        return None


def _print_shape(shape: Shape, indent: int) -> None:
    print("\n".join(_render(shape, indent)))


def _har(data: dict, needle: str | None, items: int) -> None:
    for entry in data.get("log", {}).get("entries", []):
        request, response = entry.get("request", {}), entry.get("response", {})
        content = response.get("content", {})
        body = _json_body(content.get("text"), content.get("encoding"))
        if body is None:
            continue
        url = _masked_url(request.get("url", ""))
        if needle and needle.lower() not in url.lower():
            continue
        print(f"{request.get('method', '?')} {url} -> {response.get('status', '?')}")
        sent = _json_body((request.get("postData") or {}).get("text"))
        if sent is not None:
            print("  request body:")
            _print_shape(_shape(sent, items), 4)
        print("  response:")
        _print_shape(_shape(body, items), 4)
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", help="a .json response or a .har export")
    parser.add_argument("--filter", help="HAR only: keep URLs containing this text")
    parser.add_argument("--items", type=int, default=5, help="list items sampled per list (default 5)")
    args = parser.parse_args()

    data = _json_body(Path(args.path).read_text(encoding="utf-8"))
    if data is None:
        raise SystemExit(f"{args.path} is not JSON")
    if isinstance(data, dict) and isinstance(data.get("log"), dict) and "entries" in data["log"]:
        _har(data, args.filter, args.items)
    else:
        _print_shape(_shape(data, args.items), 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
