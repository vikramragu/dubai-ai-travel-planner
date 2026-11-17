"""Phase 0: the repository layout from docs/architecture.md §3 is importable."""

from __future__ import annotations

import importlib

import pytest

PACKAGES = [
    "app",
    "app.components",
    "core",
    "core.config",
    "schemas",
    "agents",
    "agents.skills",
    "tools",
    "data",
    "data.providers",
    "graph",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name: str) -> None:
    assert importlib.import_module(name) is not None
