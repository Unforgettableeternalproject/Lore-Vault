"""T-07：ARCHITECTURE.md 分層的子套件都存在且可被 import。"""

from __future__ import annotations

import importlib

import pytest

SUBPACKAGES = [
    "mcp",
    "hooks",
    "cli",
    "api",
    "notes",
    "recall",
    "inject",
    "pipeline",
    "doctor",
    "schema",
    "binding",
    "storage",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_importable(name):
    module = importlib.import_module(f"lore_vault.{name}")
    # 確認載到的是本專案的子套件，而不是同名的第三方套件（如 PyPI 的 mcp）
    assert module.__name__ == f"lore_vault.{name}"
    assert module.__doc__
