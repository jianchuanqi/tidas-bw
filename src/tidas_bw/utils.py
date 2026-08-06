"""Shape-tolerant helpers for XML-to-JSON style TIDAS documents."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def pick_text(value: Any, preferred: tuple[str, ...] = ("en", "zh")) -> str | None:
    """Return one useful string from a TIDAS multilingual value."""
    candidates = as_list(value)
    for language in preferred:
        for item in candidates:
            if isinstance(item, Mapping) and item.get("@xml:lang") == language:
                text = item.get("#text")
                if text not in (None, ""):
                    return str(text)
    for item in candidates:
        if isinstance(item, str) and item:
            return item
        if isinstance(item, Mapping):
            text = item.get("#text")
            if text not in (None, ""):
                return str(text)
    return None


def multilingual(text: str, *, language: str = "en") -> list[dict[str, str]]:
    return [{"@xml:lang": language, "#text": text}]


def parse_number(value: Any, *, default: float | None = None) -> float:
    if value in (None, ""):
        if default is None:
            raise ValueError("missing numeric value")
        return default
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid numeric value: {value!r}") from exc


def number_string(value: Any) -> str:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError(f"non-finite numeric value: {value!r}")
    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def deep_get(value: Mapping[str, Any], *path: str, default: Any = None) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return default
        current = current[key]
    return current


def reference_uuid(reference: Any) -> str | None:
    if isinstance(reference, Mapping):
        value = reference.get("@refObjectId")
        return str(value) if value else None
    return None


def slug(value: str, fallback: str = "dataset") -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._").lower()
    return result or fallback


def canonicalize(value: Any) -> Any:
    """Normalise harmless TIDAS JSON representation differences."""
    if isinstance(value, Mapping):
        if not value:
            return None
        result: dict[str, Any] = {}
        for key in sorted(value):
            item = canonicalize(value[key])
            if key == "@uri" and isinstance(item, str):
                item = re.sub(r"\.(?:xml|json)(?=\?|$)", "", item)
            result[str(key)] = item
        return result
    if isinstance(value, list):
        items = [canonicalize(item) for item in value]
        return items[0] if len(items) == 1 else items
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | Decimal):
        return number_string(value)
    return value


def semantic_hash(value: Any) -> str:
    encoded = json.dumps(
        canonicalize(deepcopy(value)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
