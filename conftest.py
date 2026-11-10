"""Collect the plugin directory without importing it.

``hermes-gauge/`` is a Hermes plugin: the directory name is not a Python
identifier, and ``__init__.py`` uses relative imports. Pytest would otherwise
treat it as a package and fail while importing that file. Collect it as a
plain directory so ``hermes-gauge/tests`` can load ``tools.py`` by path.
"""

import time
from pathlib import Path

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_collect_directory(path: Path, parent):
    if path.name == "hermes-gauge" and (path / "__init__.py").is_file():
        return pytest.Dir.from_parent(parent, path=path)
    return None


# Anything that decides what a test reads or prints is pinned here, so a run is
# the same on every machine and never touches the real ``~/.hermes``:
# the Hermes home (preferences, databases), the language hints, the clock zone.
_LOCALE_VARS = (
    "LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES",
    "HERMES_LANG", "HERMES_LANGUAGE", "HERMES_LOCALE", "HERMES_PROFILE",
)


@pytest.fixture(autouse=True)
def _pinned_environment(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path_factory.mktemp("hermes-home")))
    for name in _LOCALE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()
