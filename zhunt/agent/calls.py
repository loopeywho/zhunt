"""Make a free model's tool call usable without another model round trip.

Free models often send a call that is almost right: a channel marker glued to the tool
name, arguments as a JSON string inside a string, "5" for 5, an extra key. Repairing those
here costs nothing; anything that is genuinely wrong becomes one short, specific error the
model can fix on its next step. A repair only ever normalises the name or value the model
sent. It never picks a different tool, so approvals and permissions are unchanged.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from zhunt.agent.tools import Tool

_MARKER = re.compile(r"<\|.*$")  # Harmony channel markers some providers leak into names
_PREFIXES = ("functions.", "functions/", "tools.", "tool.", "default_api.")


class BadCall(ValueError):
    """The call cannot run; the message is written for the model."""


def resolve_name(raw: Any, tools: dict[str, Tool]) -> str | None:
    """The advertised tool the model meant by raw, or None. Exact after tidying, never fuzzy."""

    if not isinstance(raw, str):
        return None
    name = _MARKER.sub("", raw).strip()
    for prefix in _PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
    if name in tools:
        return name
    folded = name.lower().replace("-", "_").replace(" ", "_")
    matches = [n for n in tools if n.lower() == folded]
    return matches[0] if len(matches) == 1 else None


def parse_arguments(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    try:
        value = json.loads(raw, strict=False) if isinstance(raw, (str, bytes)) else raw
        if isinstance(value, str):  # arguments JSON-encoded twice
            value = json.loads(value, strict=False)
    except ValueError:
        raise BadCall("the arguments were not valid JSON; send one JSON object") from None
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BadCall("the arguments must be one JSON object, like {\"name\": \"value\"}")
    return value


def _signature(tool: Tool) -> str:
    props = tool.parameters.get("properties") or {}
    required = set(tool.parameters.get("required") or [])
    parts = [f"{key}{'' if key in required else '?'}: {spec.get('type', 'any')}" for key, spec in props.items()]
    return f"{tool.name}({', '.join(parts)})"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _coerce(value: Any, spec: dict[str, Any]) -> Any:
    kind = spec.get("type")
    if kind == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        raise ValueError
    if kind in ("integer", "number"):
        number = _number(value)
        if number is None:
            raise ValueError
        if kind == "integer":
            if number != int(number):
                raise ValueError
            number = int(number)
        low, high = spec.get("minimum"), spec.get("maximum")
        if low is not None:
            number = max(number, low)
        if high is not None:
            number = min(number, high)
        return number
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ValueError
    if kind == "array":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError from None
        if isinstance(value, list):
            return value
        raise ValueError
    return value


def fit_arguments(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    """Keep the keys the tool declares, coerce simple type slips, and name what is missing."""

    props = tool.parameters.get("properties")
    if not isinstance(props, dict):
        return args
    fitted: dict[str, Any] = {}
    wrong: list[str] = []
    for key, spec in props.items():
        if key not in args or args[key] is None:
            continue
        try:
            fitted[key] = _coerce(args[key], spec if isinstance(spec, dict) else {})
        except ValueError:
            wrong.append(f"{key} must be {spec.get('type', 'valid') if isinstance(spec, dict) else 'valid'}")
    missing = [key for key in tool.parameters.get("required") or [] if key not in fitted and key not in {w.split()[0] for w in wrong}]
    if wrong or missing:
        problems = wrong + ([f"missing {', '.join(missing)}"] if missing else [])
        raise BadCall(f"{'; '.join(problems)}. Usage: {_signature(tool)}")
    return fitted
