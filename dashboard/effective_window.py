"""Degrade a detected context window by model family.

The length a provider advertises is a nominal window. Retrieval quality
fades before that limit, and flash / lite models fade earlier than pro
models. The desktop gauge colors against the smaller effective window.

The family patterns live in ``hermes-gauge/gauge_math.py``. They are
case-insensitive and the first match wins. Flash and lite are tested before
mini, so a name that contains both keeps the weaker factor. They are tested
before the pro patterns too, so a flash variant does not pick up a pro
family's full window.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CHARS_PER_TOKEN = 4

# The patterns live in hermes-gauge/gauge_math.py. This module asks that
# function so the chip and gauge_check cannot grow a second table.
UNKNOWN_FAMILY = "unknown"
UNKNOWN_FACTOR = 0.60


def _gauge_math():
    path = Path(__file__).resolve().parents[1] / "hermes-gauge" / "gauge_math.py"
    key = "hermes_gauge_math_families"
    cached = sys.modules.get(key)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def family_factor(model) -> tuple[str, float]:
    """Return ``(family, factor)`` for a model name. Unknown names use 0.60."""
    return _gauge_math().model_family(model)


def tokens_from_chars(chars, chars_per_token: int = CHARS_PER_TOKEN) -> int:
    """Estimate tokens as ``chars / chars_per_token``. Never negative."""
    try:
        count = int(chars or 0)
    except (TypeError, ValueError):
        count = 0
    if count < 0:
        count = 0
    try:
        per = int(chars_per_token or 0)
    except (TypeError, ValueError):
        per = CHARS_PER_TOKEN
    if per < 1:
        per = CHARS_PER_TOKEN
    return count // per


def effective_tokens(nominal, factor: float) -> int:
    """Apply a family factor to a nominal window. At least one token.

    Factors are multiples of 0.01. Scaling by 100 keeps the product on
    integers so 0.40 and 0.60 do not drift through binary floats.
    """
    try:
        size = int(nominal or 0)
    except (TypeError, ValueError):
        size = 0
    if size < 1:
        size = 1
    try:
        hundredths = int(round(float(factor) * 100))
    except (TypeError, ValueError):
        hundredths = int(round(UNKNOWN_FACTOR * 100))
    if hundredths < 1:
        hundredths = 1
    sized = (size * hundredths + 50) // 100
    return max(sized, 1)
