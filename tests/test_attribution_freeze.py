"""T-13：歷史歸屬凍結（防 A7 repo 改名事故重演）。

情境：episode 在 repo 叫 `OldName`、機器叫 `desktop-a` 時寫入；之後 repo 改名
（資料夾搬移 + remote 改名）、在另一台機器、另一個 cwd 讀回這筆舊 episode。

這裡保護的是「讀取流程」＝ 儲存形式（JSON）→ `Episode.from_dict` → 使用：
- `repo`、`repo_root`、`machine` 必須維持寫入值，不受 cwd／主機名稱／
  binding 解析結果影響；`to_dict` 還原後與儲存內容逐字相同。
- 改名靠 `Vault.aliases` 在讀取端接起來（`VaultIndex.resolve(ep.repo)`
  找到現行 vault），而不是把 episode 的 `repo` 改寫成新名字。

目前 schema 沒有任何讀取時重算的程式路徑；`test_freeze_test_is_load_bearing`
把重算邏輯塞進 `Episode.from_dict` 的轉換鉤子，證明一旦有人加了重算，本測試會紅。
讀取流程參數化為兩條：直接 JSON，以及經儲存層寫入 SQLite 再讀回
（`insert_episode` → `list_episodes`），兩條都必須保持凍結。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from lore_vault.binding import VaultIndex, resolve_binding
from lore_vault.schema import Episode, Vault

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="沒有 git")

WRITE_MACHINE = "desktop-a"
READ_MACHINE = "laptop-b"
FROZEN_FIELDS = ("repo", "repo_root", "machine")


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _episode_dict(repo: str, repo_root: str, machine: str) -> dict:
    return {
        "prompt_id": "p-1",
        "turn_index": 0,
        "session_id": "s-1",
        "agent": "claude-code",
        "origin": "human",
        "machine": machine,
        # 與 spike 實際格式及儲存層正規化格式相同（毫秒、Z），逐字比對才有意義
        "started_at": "2026-08-20T02:00:00.000Z",
        "ended_at": "2026-08-20T02:05:00.000Z",
        "cwd": [repo_root],
        "repo": repo,
        "repo_root": repo_root,
        "git_branch": ["main"],
        "cc_version": "2.0.0",
        "user_text": "修 bug",
        "assistant_text": "好",
        "injected": [],
        "tool_sequence": [{"name": "Edit", "count": 1}],
        "tool_calls_total": 1,
        "mcp_tools": [],
        "skills": [],
        "files_edited": ["src/a.py"],
        "files_read": [],
        "symbols_edited": [],
        "thinking_blocks": 0,
    }


def _read_json(stored: str) -> Episode:
    """讀取流程：儲存形式 → Episode。"""
    return Episode.from_dict(json.loads(stored))


@pytest.fixture(params=["json", "storage"])
def read_stored(request, tmp_path):
    """讀取流程的兩種實作。storage：每次讀都用新的資料庫檔，寫入後經儲存層讀回。"""
    if request.param == "json":
        return _read_json

    from lore_vault.storage.db import connect
    from lore_vault.storage.records import insert_episode, list_episodes
    from lore_vault.storage.vaults import upsert_vault

    counter = iter(range(1_000_000))

    def read(stored: str) -> Episode:
        conn = connect(tmp_path / f"attr-{next(counter)}.db")
        try:
            upsert_vault(conn, Vault(key="folder/attr", display="attr"))
            insert_episode(conn, "folder/attr", Episode.from_dict(json.loads(stored)))
            [ep], _ = list_episodes(conn, "folder/attr")
        finally:
            conn.close()
        return ep

    return read


def _assert_frozen(ep: Episode, stored: str) -> None:
    written = json.loads(stored)
    for name in FROZEN_FIELDS:
        assert getattr(ep, name) == written[name], name
    assert ep.to_dict() == written


@pytest.fixture
def renamed_repo(tmp_path, monkeypatch):
    """寫入一筆舊 episode，接著把 repo 改名、換機器、換 cwd。回傳 (stored, old_key)。"""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    # ── 寫入當下 ──
    old_dir = tmp_path / "OldName"
    old_dir.mkdir()
    _git(old_dir, "init", "-q")
    _git(old_dir, "remote", "add", "origin", "https://github.com/me/OldName.git")
    old_binding = resolve_binding(old_dir)
    assert old_binding.display == "OldName"
    ep = Episode.from_dict(
        _episode_dict(old_binding.display, old_dir.as_posix(), WRITE_MACHINE)
    )
    stored = json.dumps(ep.to_dict(), ensure_ascii=False)

    # ── repo 改名、換機器、換 cwd ──
    new_dir = tmp_path / "NewName"
    old_dir.rename(new_dir)
    _git(new_dir, "remote", "set-url", "origin", "https://github.com/me/NewName.git")
    monkeypatch.chdir(new_dir)
    monkeypatch.setattr(socket, "gethostname", lambda: READ_MACHINE)
    monkeypatch.setattr(platform, "node", lambda: READ_MACHINE)
    for var in ("COMPUTERNAME", "HOSTNAME"):
        monkeypatch.setenv(var, READ_MACHINE)

    # 確認環境真的變了（否則下面的斷言是空轉）
    now = resolve_binding(os.getcwd())
    assert now.display == "NewName" and now.key != old_binding.key
    assert not old_dir.exists()
    return stored, old_binding.key


@needs_git
def test_old_episode_keeps_frozen_attribution_after_rename(renamed_repo, read_stored):
    stored, _ = renamed_repo
    ep = read_stored(stored)
    _assert_frozen(ep, stored)
    assert (ep.repo, ep.machine) == ("OldName", WRITE_MACHINE)
    assert ep.repo_root is not None and ep.repo_root.endswith("/OldName")


@needs_git
def test_rename_is_joined_by_aliases_not_by_rewriting(renamed_repo, read_stored):
    stored, old_key = renamed_repo
    current = resolve_binding(os.getcwd())
    vault = Vault(
        key=current.key, display=current.display, aliases=(old_key, "OldName")
    )
    index = VaultIndex([vault])
    ep = read_stored(stored)
    # 舊名字透過別名找到現行 vault
    assert index.resolve(ep.repo) is vault
    # 但 episode 本身不被改寫
    _assert_frozen(ep, stored)


@needs_git
def test_freeze_test_is_load_bearing(renamed_repo, read_stored):
    """把「讀取時依環境重算」塞進讀取流程，上面的凍結斷言必須失守。"""
    stored, _ = renamed_repo
    original = Episode._convert.__func__  # type: ignore[attr-defined]

    def recompute_on_read(cls, data):
        data = original(cls, data)
        binding = resolve_binding(os.getcwd())
        data["repo"] = binding.display
        data["repo_root"] = Path(os.getcwd()).as_posix()
        data["machine"] = platform.node()
        return data

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Episode, "_convert", classmethod(recompute_on_read))
        ep = read_stored(stored)
        assert (ep.repo, ep.machine) == ("NewName", READ_MACHINE)
        with pytest.raises(AssertionError):
            _assert_frozen(ep, stored)

    # 只還原重算鉤子（環境仍是改名後）：恢復凍結
    _assert_frozen(read_stored(stored), stored)
