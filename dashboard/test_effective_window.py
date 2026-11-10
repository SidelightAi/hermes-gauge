"""Unit tests for the model-family effective window. One case per table entry."""

import importlib.util
from pathlib import Path

import pytest


def _load():
    path = Path(__file__).resolve().with_name("effective_window.py")
    spec = importlib.util.spec_from_file_location(
        "hermes_gauge_effective_window_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ew = _load()


@pytest.mark.parametrize(
    ("model", "family", "factor"),
    [
        ("flash", "flash", 0.25),
        ("lite", "flash", 0.25),
        ("flashx", "flash", 0.25),
        ("air", "flash", 0.25),
        ("nano", "flash", 0.25),
        ("gemini-2.0-lite", "flash", 0.25),
        ("GLM-5.3-FLASH", "flash", 0.25),
        ("mini", "mini", 0.40),
        ("4o-mini", "mini", 0.40),
        ("o4-mini", "mini", 0.40),
        ("glm-5.2", "mini", 0.40),
        ("haiku", "haiku", 0.50),
        ("HAIKU", "haiku", 0.50),
        ("claude-3-5-haiku", "haiku", 0.50),
        ("sonnet", "pro", 1.0),
        ("opus", "pro", 1.0),
        ("gpt-5", "pro", 1.0),
        ("gpt-6", "pro", 1.0),
        ("gemini-3", "pro", 1.0),
        ("GEMINI-3", "pro", 1.0),
        ("gemini-2.5-pro", "pro", 1.0),
        ("Gemini-2.5-Pro", "pro", 1.0),
        ("glm-5.3-prime", "pro", 1.0),
        ("deepseek-r1", "pro", 1.0),
        ("qwen2.5-max", "pro", 1.0),
        ("Qwen-Max", "pro", 1.0),
        ("hermes-3", "unknown", 0.60),
        ("gpt-4.1", "unknown", 0.60),
        ("", "unknown", 0.60),
        (None, "unknown", 0.60),
        # Weaker row wins when a name contains two families.
        ("flash-mini", "flash", 0.25),
        ("gpt-5-mini", "mini", 0.40),
        ("gpt-5-nano", "flash", 0.25),
        ("gemini-3-flash", "flash", 0.25),
        ("glm-5.3-flash", "flash", 0.25),
    ],
)
def test_family_factor_matches_each_table_entry(model, family, factor):
    assert ew.family_factor(model) == (family, factor)


def test_tokens_from_chars_uses_four_characters_per_token():
    assert ew.CHARS_PER_TOKEN == 4
    assert ew.tokens_from_chars(231_289) == 57_822
    assert ew.tokens_from_chars(0) == 0
    assert ew.tokens_from_chars(-8) == 0
    assert ew.tokens_from_chars(None) == 0
    assert ew.tokens_from_chars("nope") == 0


def test_effective_window_scales_the_nominal_length():
    assert ew.effective_tokens(1_000_000, 0.25) == 250_000
    assert ew.effective_tokens(1_048_576, 0.25) == 262_144
    assert ew.effective_tokens(200_000, 0.40) == 80_000
    assert ew.effective_tokens(200_000, 0.50) == 100_000
    assert ew.effective_tokens(200_000, 0.60) == 120_000
    assert ew.effective_tokens(200_000, 1.0) == 200_000
    assert ew.effective_tokens(0, 0.25) == 1
