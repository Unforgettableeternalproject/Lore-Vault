"""T-12：git remote → vault key，以及別名解析。

- 正規化結果對照現行 ON notebook description 的 `[bind: ...]`
- 若本機有 `~/.claude/pm/pm-bind.py`，逐案與它的 `normalize` 做差異比對
- 真實 git：在 tmp_path 建 repo 設 remote（不連網路）
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from lore_vault.binding import (
    SOURCE_FOLDER,
    SOURCE_GIT_REMOTE,
    AliasConflictError,
    VaultIndex,
    display_from_remote,
    git_remote,
    normalize_remote,
    resolve_binding,
    resolve_vault,
)
from lore_vault.binding import remote as remote_mod
from lore_vault.schema import Vault

# 現行 ON notebook description 內的 `[bind: ...]`（2026-09-25 查得）
ON_BIND_REMOTE_KEYS = [
    "github.com/unforgettableeternalproject/chatroom",
    "github.com/ji-shine/jsai-main-website",
    "github.com/u-e-production/seven-divines",
]


def _variants(host: str, owner: str, repo: str) -> list[str]:
    """同一 repo 的常見 remote 寫法（含大小寫、.git、尾斜線、port、帳密）。"""
    return [
        f"https://{host}/{owner}/{repo}",
        f"https://{host}/{owner}/{repo}.git",
        f"https://{host}/{owner}/{repo}/",
        f"https://user:token@{host}/{owner}/{repo}.git",
        f"http://{host}/{owner}/{repo}.git",
        f"git@{host}:{owner}/{repo}.git",
        f"git@{host}:{owner}/{repo}",
        f"{host}:{owner}/{repo}.git",
        f"ssh://git@{host}/{owner}/{repo}.git",
        f"ssh://git@{host}:22/{owner}/{repo}",
        f"git://{host}/{owner}/{repo}.git",
        f"  https://{host}/{owner}/{repo}.git  ",
        f"https://{host}/{owner}/{repo}.git?ref=main#readme",
    ]


REMOTE_CASES = [
    *[
        (url, "github.com/unforgettableeternalproject/chatroom")
        for url in _variants("github.com", "Unforgettableeternalproject", "Chatroom")
    ],
    *[
        (url, "github.com/ji-shine/jsai-main-website")
        for url in _variants("github.com", "Ji-Shine", "JSAI-Main-Website")
    ],
    *[
        (url, "github.com/u-e-production/seven-divines")
        for url in _variants("GitHub.com", "U-E-Production", "Seven-Divines")
    ],
    # 自架／巢狀群組
    ("git@gitlab.example.com:group/sub/Proj.git", "gitlab.example.com/group/sub/proj"),
    (
        "https://gitlab.example.com:8443/group/sub/proj",
        "gitlab.example.com/group/sub/proj",
    ),
]


@pytest.mark.parametrize("url, expected", REMOTE_CASES)
def test_normalize_remote(url, expected):
    assert normalize_remote(url) == expected


def test_known_on_bind_keys_are_fixed_points():
    # 已經是 key 的字串再正規化一次不變
    for key in ON_BIND_REMOTE_KEYS:
        assert normalize_remote(key) == key


@pytest.mark.parametrize("url", [None, "", "   "])
def test_normalize_empty(url):
    assert normalize_remote(url) == ""


def test_display_keeps_case():
    assert display_from_remote("git@github.com:Ji-Shine/JSAI-Main-Website.git") == (
        "JSAI-Main-Website"
    )
    assert display_from_remote("https://github.com/U/Repo/") == "Repo"


# ── 與 pm-bind 的差異比對 ─────────────────────────────────────────

_PM_BIND = Path.home() / ".claude" / "pm" / "pm-bind.py"

# 已知且刻意保留的 pm-bind 行為（照抄，不修）：本機路徑 remote 會被 SCP_LIKE 誤判
QUIRK_CASES = [
    ("C:/repos/demo.git", "c//repos/demo"),
    (r"D:\repos\Demo", r"d/\repos\demo"),
    ("/srv/git/Demo.git", "/srv/git/demo"),
]


def _load_pm_bind():
    spec = importlib.util.spec_from_file_location("pm_bind_reference", _PM_BIND)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not _PM_BIND.is_file(), reason="本機沒有 pm-bind.py 可比對")
@pytest.mark.parametrize("url", [u for u, _ in REMOTE_CASES + QUIRK_CASES])
def test_matches_pm_bind_normalize(url):
    reference = _load_pm_bind()
    assert normalize_remote(url) == reference.normalize(url)


@pytest.mark.parametrize("url, expected", QUIRK_CASES)
def test_pm_bind_quirks_preserved(url, expected):
    assert normalize_remote(url) == expected


# ── 真實 git（本機設定，不連網路） ─────────────────────────────────

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="沒有 git")


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    # 避免 tmp_path 往上找到外層 repo
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return tmp_path


@needs_git
def test_resolve_from_origin(isolated):
    repo = isolated / "Chatroom"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "git@github.com:Unforgettableeternalproject/Chatroom.git",
    )
    b = resolve_binding(repo)
    assert b.key == "github.com/unforgettableeternalproject/chatroom"
    assert b.display == "Chatroom"
    assert b.source == SOURCE_GIT_REMOTE
    # 子目錄也解析到同一個 key
    sub = repo / "src"
    sub.mkdir()
    assert resolve_binding(sub).key == b.key


@needs_git
def test_falls_back_to_first_remote_without_origin(isolated):
    repo = isolated / "x"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(
        repo,
        "remote",
        "add",
        "upstream",
        "https://github.com/Ji-Shine/JSAI-Main-Website",
    )
    assert git_remote(repo) == "https://github.com/Ji-Shine/JSAI-Main-Website"
    assert resolve_binding(repo).key == "github.com/ji-shine/jsai-main-website"


@needs_git
def test_git_repo_without_remote_uses_folder(isolated):
    repo = isolated / "Lore-Vault"
    repo.mkdir()
    _git(repo, "init", "-q")
    b = resolve_binding(repo)
    assert (b.key, b.display, b.source) == (
        "folder/lore-vault",
        "Lore-Vault",
        SOURCE_FOLDER,
    )


def test_non_git_directory_uses_folder(isolated):
    plain = isolated / "Lore-Vault"
    plain.mkdir()
    assert git_remote(plain) is None
    assert resolve_binding(plain).key == "folder/lore-vault"


def test_on_folder_mcsf_differs_only_by_case(isolated):
    """ON 上是手寫的 `folder/MCSF`；pm-bind／本實作產生 `folder/mcsf`。

    別名索引不分大小寫，兩者接得起來。
    """
    plain = isolated / "MCSF"
    plain.mkdir()
    b = resolve_binding(plain)
    assert b.key == "folder/mcsf"
    assert b.key != "folder/MCSF"
    index = VaultIndex([Vault(key="folder/MCSF", display="MCSF")])
    assert index.resolve(b.key) is not None


def test_missing_git_executable_uses_folder(isolated, monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(remote_mod.subprocess, "run", boom)
    plain = isolated / "Demo"
    plain.mkdir()
    assert resolve_binding(plain).source == SOURCE_FOLDER


def test_timeout_uses_folder(isolated, monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(remote_mod.subprocess, "run", slow)
    plain = isolated / "Demo"
    plain.mkdir()
    assert resolve_binding(plain).source == SOURCE_FOLDER


def test_nonexistent_directory_raises(isolated):
    with pytest.raises(NotADirectoryError):
        resolve_binding(isolated / "nope")


def test_git_is_only_called_with_local_subcommands(isolated, monkeypatch):
    """binding 不得呼叫會連網的 git 子指令（fetch／ls-remote 等）。"""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(remote_mod.subprocess, "run", spy)
    plain = isolated / "Demo"
    plain.mkdir()
    resolve_binding(plain)
    assert calls
    for cmd in calls:
        sub = cmd[3:]
        assert sub[:2] == ["remote", "get-url"] or sub == ["remote"], cmd


# ── 別名 ──────────────────────────────────────────────────────────

# 改名案例：spike REPO_ALIASES 的 AI-Website-API → JSAI-API（owner 為測試虛構）
RENAMED = Vault(
    key="github.com/ji-shine/jsai-api",
    display="JSAI-API",
    aliases=("github.com/ji-shine/ai-website-api", "AI-Website-API"),
)
CHATROOM = Vault(
    key="github.com/unforgettableeternalproject/chatroom", display="Chatroom"
)


@pytest.mark.parametrize(
    "lookup",
    [
        "github.com/ji-shine/jsai-api",
        "github.com/ji-shine/ai-website-api",
        "AI-Website-API",
        "ai-website-api",
        "  GitHub.com/Ji-Shine/AI-Website-API  ",
    ],
)
def test_old_key_resolves_to_same_vault(lookup):
    index = VaultIndex([RENAMED, CHATROOM])
    assert index.resolve(lookup) is RENAMED


def test_old_remote_url_resolves_after_normalization():
    old_url = "git@github.com:Ji-Shine/AI-Website-API.git"
    assert resolve_vault(normalize_remote(old_url), [RENAMED, CHATROOM]) is RENAMED


@pytest.mark.parametrize("lookup", ["", "  ", "github.com/ji-shine/other"])
def test_unknown_key_returns_none(lookup):
    assert VaultIndex([RENAMED, CHATROOM]).resolve(lookup) is None


def test_alias_claimed_by_two_vaults_raises():
    other = Vault(key="github.com/x/y", display="Y", aliases=("AI-Website-API",))
    with pytest.raises(AliasConflictError, match="ai-website-api"):
        VaultIndex([RENAMED, other])


def test_alias_colliding_with_other_key_raises():
    other = Vault(
        key="github.com/x/y", display="Y", aliases=("GitHub.com/Ji-Shine/JSAI-API",)
    )
    with pytest.raises(AliasConflictError):
        VaultIndex([RENAMED, other])


def test_duplicate_vault_key_raises():
    dup = Vault(key="github.com/ji-shine/jsai-api", display="dup")
    with pytest.raises(AliasConflictError):
        VaultIndex([RENAMED, dup])


def test_alias_resolution_is_load_bearing(monkeypatch):
    """拿掉別名註冊後，舊 key 查不到——證明上面的測試確實依賴別名。"""
    bare = Vault(key=RENAMED.key, display=RENAMED.display)
    assert VaultIndex([bare]).resolve("AI-Website-API") is None


def test_mcsf_case_variants_map_to_single_vault():
    """ON 手寫的 `folder/MCSF` 與 pm-bind 產生的 `folder/mcsf` 只會是同一個 vault。"""
    upper = Vault(key="folder/MCSF", display="MCSF")
    lower = Vault(key="folder/mcsf", display="MCSF")
    assert upper.key == lower.key == "folder/mcsf"
    assert upper.display == "MCSF"
    # 兩者同時出現即視為重複 vault，不會分裂成兩個
    with pytest.raises(AliasConflictError):
        VaultIndex([upper, lower])
    index = VaultIndex([upper])
    assert index.resolve("folder/mcsf") is upper
    assert index.resolve("folder/MCSF") is upper


def test_key_normalization_is_load_bearing(monkeypatch):
    """拿掉 Vault 建構時的小寫正規化，folder/MCSF 就分裂成兩個 vault、且查不到。"""
    monkeypatch.setattr("lore_vault.schema.models.canonical_key", lambda k: k)
    upper = Vault(key="folder/MCSF", display="MCSF")
    lower = Vault(key="folder/mcsf", display="MCSF")
    assert upper.key != lower.key
    index = VaultIndex([upper, lower])  # 不再報衝突
    assert len(index) == 2
    assert VaultIndex([upper]).resolve("folder/mcsf") is None
