"""JSON output that keeps FHIR decimal precision ("5.00" must not become 5.0)."""
from __future__ import annotations

import json
import re
from decimal import Decimal

_MARK = re.compile(r'"@@(-?\d+(?:\.\d+)?)@@"')


def _prep(o):
    if isinstance(o, Decimal):
        return f"@@{format(o, 'f')}@@"
    if isinstance(o, dict):
        return {k: _prep(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_prep(v) for v in o]
    return o


def dumps(obj, indent: int | None = 2) -> str:
    return _MARK.sub(r"\1", json.dumps(_prep(obj), indent=indent, ensure_ascii=False))
