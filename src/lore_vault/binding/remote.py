"""git remote → 穩定 vault key。

邏輯沿用 `~/.claude/pm/pm-bind.py`（與 `~/.claude/pm-kit/bin/pm-bind.py` 內容相同）：
同一個 repo 在不同機器路徑不同，git remote 是唯一跨裝置穩定的識別；
同一 remote 有多種寫法，要正規化成 `host/owner/repo` 小寫。

    git@github.com:U/Repo.git       ┐
    https://github.com/U/Repo.git   ├─→  github.com/u/repo
    ssh://git@github.com:22/U/Repo  ┘

無 remote（含非 git 目錄）時退回 `folder/<資料夾名小寫>`。

只用標準庫、不連網路：`git remote get-url` 只讀本機設定。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SOURCE_GIT_REMOTE = "git-remote"
SOURCE_FOLDER = "folder"
FOLDER_PREFIX = "folder/"

# 與 pm-bind 相同的正規表達式，改動前先確認兩邊行為一致（見 tests/test_binding.py）
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_SCP_LIKE = re.compile(r"^([\w.+\-]+@)?([^:/]+):(?!\d+/)(.+)$")
_USERINFO = re.compile(r"^[^/@]+@")
_PORT = re.compile(r"^([^/]+):\d+/")
_DOT_GIT = re.compile(r"\.git$")

_GIT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Binding:
    """目錄解析出的綁定：`key` 用於比對（小寫），`display` 保留原始大小寫。"""

    key: str
    display: str
    source: str  # SOURCE_GIT_REMOTE / SOURCE_FOLDER


def normalize_remote(url: str | None) -> str:
    """把任意 git remote 寫法壓成 `host/path` 小寫形式；空輸入回傳空字串。"""
    u = (url or "").strip()
    if not u:
        return ""

    # scp-like（git@host:path）要先判，否則會被誤認為帶 scheme
    m = _SCP_LIKE.match(u)
    if m and not _SCHEME.match(u):
        u = f"{m.group(2)}/{m.group(3)}"
    else:
        u = _SCHEME.sub("", u)
        u = _USERINFO.sub("", u)
        u = _PORT.sub(r"\1/", u, count=1)

    u = u.split("?")[0].split("#")[0]
    u = _DOT_GIT.sub("", u)
    u = u.rstrip("/")
    return u.lower()


def display_from_remote(url: str) -> str:
    """remote 的最後一段（repo 名），保留原始大小寫。"""
    raw = _DOT_GIT.sub("", url.strip().rstrip("/"))
    return re.split(r"[/:]", raw)[-1]


def folder_key(name: str) -> str:
    return f"{FOLDER_PREFIX}{name.lower()}"


def _run_git(path: Path, args: list[str]) -> str | None:
    env = dict(os.environ)
    # 保險：即使未來誤加會連網的子指令，也不要卡在帳密提示
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        out = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        # git 不存在或逾時：與非 git 目錄同樣處理
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def git_remote(path: str | os.PathLike[str]) -> str | None:
    """取 origin 的 URL；沒有 origin 就取第一個 remote。

    都沒有、非 git 目錄、git 不存在或逾時皆回傳 None。
    """
    target = Path(path)
    url = _run_git(target, ["remote", "get-url", "origin"])
    if url:
        return url
    names = _run_git(target, ["remote"])
    if names:
        first = names.splitlines()[0].strip()
        if first:
            return _run_git(target, ["remote", "get-url", first])
    return None


def resolve_binding(path: str | os.PathLike[str]) -> Binding:
    """把目錄解析成綁定。與 pm-bind 的輸出一致（不含 ON 專用的 `notebook` 欄）。

    目錄不存在時拋 `NotADirectoryError`（pm-bind 會靜默退回 folder key）。
    """
    target = Path(os.path.abspath(path))
    if not target.is_dir():
        raise NotADirectoryError(f"不是目錄：{target}")

    url = git_remote(target)
    key = normalize_remote(url) if url else ""
    if key and url:
        display = display_from_remote(url) or target.name
        return Binding(key=key, display=display, source=SOURCE_GIT_REMOTE)
    return Binding(
        key=folder_key(target.name), display=target.name, source=SOURCE_FOLDER
    )
