from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

BRL_QUANTUM = Decimal("0.01")


def unique_strings(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value)).quantize(BRL_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    return result if result >= 0 else None


def json_money(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def objects(value: Any) -> list[Mapping[str, Any]]:
    """Flatten mapping/list containers without interpreting scalar values."""
    found: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        found.append(value)
        for child in value.values():
            found.extend(objects(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(objects(child))
    return found


def first(data: Any, *keys: str) -> Any:
    for item in objects(data):
        for key in keys:
            if key in item and item[key] is not None:
                return item[key]
    return None


def collect(data: Any, *keys: str) -> list[Any]:
    values: list[Any] = []
    for item in objects(data):
        for key in keys:
            value = item.get(key)
            if isinstance(value, (list, tuple)):
                values.extend(value)
            elif value is not None:
                values.append(value)
    return values
