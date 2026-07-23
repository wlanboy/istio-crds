"""Shared fixtures for the test suite.

A few modules in this project have hyphens in their filename
(``istio-graph.py``, ``sync-job.py``, ``istio-objekt-liste.py``,
``arangodb/datenimport-arangodb.py``) because they are meant to be run as
standalone CLI scripts, not imported. ``import istio-graph`` is not valid
Python syntax, so those modules are loaded via ``importlib`` instead, through
the ``load_module`` fixture below.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module(module_name: str, relative_path: str) -> object:
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def load_module() -> Callable[[str, str], object]:
    return _load_module
