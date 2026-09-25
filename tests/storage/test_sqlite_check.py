"""T-26：容器啟動前的 SQLite 能力斷言（版本 ≥ 3.37、FTS5、STRICT）。"""

from __future__ import annotations

import sqlite3

from lore_vault.storage import sqlite_check
from lore_vault.storage.sqlite_check import main, sqlite_problems


def test_current_runtime_passes(capsys):
    assert sqlite_problems() == []
    assert main() == 0
    assert "FTS5" in capsys.readouterr().out


def test_old_version_is_rejected():
    problems = sqlite_problems(version_info=(3, 36, 0))
    assert len(problems) == 1 and "3.36.0" in problems[0]


def test_missing_fts5_is_rejected(monkeypatch, capsys):
    class NoFts:
        def __init__(self):
            self._conn = sqlite3.connect(":memory:")

        def execute(self, sql):
            if "fts5" in sql:
                raise sqlite3.OperationalError("no such module: fts5")
            return self._conn.execute(sql)

        def close(self):
            self._conn.close()

    problems = sqlite_problems(connect=lambda _: NoFts())
    assert len(problems) == 1 and "FTS5" in problems[0]

    monkeypatch.setattr(sqlite_check, "sqlite_problems", lambda: problems)
    assert main() == 1
    assert "FTS5" in capsys.readouterr().err
