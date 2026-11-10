"""Repo-root plugin entry.

``hermes plugins install`` expects ``plugin.yaml`` and ``register`` beside
each other. The implementation lives in ``hermes-gauge/``. ``webapp-demo/``
is not imported.
"""

import importlib.util
import sys
from pathlib import Path

_PACKAGE = "hermes_gauge_plugin"


def _implementation():
    if _PACKAGE in sys.modules:
        return sys.modules[_PACKAGE]
    pkg = Path(__file__).resolve().parent / "hermes-gauge"
    init = pkg / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        _PACKAGE,
        init,
        submodule_search_locations=[str(pkg)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _PACKAGE
    sys.modules[_PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def register(ctx):
    return _implementation().register(ctx)
