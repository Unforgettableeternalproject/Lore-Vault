"""任務層測試共用：tmp 任務目錄建構器與記憶體內的假 Lore Vault（真的 HTTP）。

假服務走 `tests/fake_service.FakeService`（`http.server` 執行緒），任務層的
`VaultClient` 照常經 `hooks.service.request_json` 打過去——不連真實服務、不讀
`~/.lore-vault/client.env`、不看行程環境變數（`environ={}`）。

`propose`／`list`／`validate`／`archive` 結尾會推任務快照：`TasksDir.run` 沒給 client
時一律用未設定推送的 client（`offline_client`，不打網路、只在 stderr 警告），
絕不落到真實的 client.env。
"""

from __future__ import annotations

import datetime as dt
import io
import itertools
from pathlib import Path
from typing import Any

import pytest
import yaml

from lore_vault.hooks.client_env import load_client_settings
from lore_vault.tasks import cli
from lore_vault.tasks.vault_client import VaultClient

from ..fake_service import FakeService, closed_port_url

VAULT = "folder/demo"
NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.UTC)

DECISIONS_TEXT = """# 決策紀錄

## 待裁決

### D6 對 U.E.P 的接口

**不在本次範圍（A16）。**

### D12 對外自架發佈

**已裁決（艾斯維爾 2026-09-27）**：

1. MCP 端點

### D13 遠端機器收 episode 與服務設定頁

艾斯維爾 2026-09-27 裁決：

1. 遠端收 episode
"""

SPEC_A = """# demo Specification

## Purpose
示範用的 capability。

## Requirements

### Requirement: 資料根目錄
資料 SHALL 存放於 `~/.demo/`。

#### Scenario: 讀取資料根
- **WHEN** 解析路徑
- **THEN** 取得 `~/.demo/`

### Requirement: 封存
中間產物 MUST 封存。

#### Scenario: 搬遷
- **WHEN** 搬遷
- **THEN** 封存
"""


def requirement(name: str, text: str = "系統 SHALL 運作。", scenarios=("基本",)) -> str:
    lines = [f"### Requirement: {name}", text, ""]
    for s in scenarios:
        lines += [f"#### Scenario: {s}", "- **WHEN** 觸發", "- **THEN** 成立", ""]
    return "\n".join(lines)


def delta(added=(), modified=(), removed=()) -> str:
    parts = ["# Spec Delta", ""]
    if added:
        parts += ["## ADDED Requirements", "", *added]
    if modified:
        parts += ["## MODIFIED Requirements", "", *modified]
    if removed:
        parts += ["## REMOVED Requirements", ""] + [
            f"### Requirement: {n}" for n in removed
        ]
    return "\n".join(parts) + "\n"


class TasksDir:
    """tmp_path 底下的 `<project>/openspec`。"""

    def __init__(self, project: Path) -> None:
        self.project = project
        project.mkdir(parents=True, exist_ok=True)
        self.root = project / "openspec"
        self.decisions = project / "DECISIONS.md"
        self.decisions.write_text(DECISIONS_TEXT, encoding="utf-8")
        self.run("init")
        (self.root / "config.yaml").write_text(
            "schema: spec-driven\ndecisions_file: DECISIONS.md\n", encoding="utf-8"
        )

    def run(self, *argv: str, client=None, now=NOW) -> tuple[int, str]:
        """stdout 回傳；stderr（快照推送警告）存在 `self.err`。"""
        out = io.StringIO()
        err = io.StringIO()
        code = cli.main(
            ["--root", str(self.root), *argv],
            stdout=out,
            environ={},
            cwd=self.project,
            client_factory=client or offline_client,
            now=now,
            stderr=err,
        )
        self.err = err.getvalue()
        return code, out.getvalue()

    def change_dir(self, name: str) -> Path:
        return self.root / "changes" / name

    def meta(self, name: str) -> dict[str, Any]:
        path = self.change_dir(name) / ".openspec.yaml"
        if not path.exists():
            path = self.archived_dir(name) / ".openspec.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def set_meta(self, name: str, **fields: Any) -> None:
        path = self.change_dir(name) / ".openspec.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data.update(fields)
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def archived_dir(self, name: str) -> Path:
        hits = sorted((self.root / "changes" / "archive").glob(f"*-{name}"))
        assert hits, f"{name} 未封存"
        return hits[-1]

    def write_main(self, capability: str, text: str) -> Path:
        path = self.root / "specs" / capability / "spec.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def main_spec(self, capability: str) -> str:
        return (self.root / "specs" / capability / "spec.md").read_text(
            encoding="utf-8"
        )

    def propose(
        self,
        name: str,
        *flags: str,
        deltas: dict[str, str] | None = None,
        complete: bool = True,
    ):
        """`complete`：預設把 tasks.md 全部勾完（archive 會拒絕未完成的 tasks）。"""
        code, out = self.run("propose", name, *flags)
        assert code == 0, out
        if complete:
            self.check_all_tasks(name)
        for cap, text in (deltas or {}).items():
            path = self.change_dir(name) / "specs" / cap / "spec.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if deltas:
            code, out = self.run("validate", name, "--record-base")
            assert code == 0, out

    def check_all_tasks(self, name: str) -> None:
        path = self.change_dir(name) / "tasks.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
            encoding="utf-8",
        )


@pytest.fixture
def tasks_dir(tmp_path: Path) -> TasksDir:
    return TasksDir(tmp_path / "proj")


class FakeVault:
    """記憶體內的 `/v1/{vault_resolve,write,list,get}`。

    - `fail_write_at`：第 N 次 write（從 1 起算）回 503，模擬中途服務掛掉
    - `list_page`：list 每頁筆數（測翻頁）
    """

    def __init__(self, *, fail_write_at: int | None = None, list_page: int = 2) -> None:
        self.notes: dict[str, dict[str, Any]] = {}
        # 側載：{(vault, key): {"mime", "content_base64", "updated"}}
        self.blobs: dict[tuple[str, str], dict[str, Any]] = {}
        self.blob_puts = 0
        self.writes = 0
        self.fail_write_at = fail_write_at
        self.list_page = list_page
        self._ids = (f"n{i}" for i in itertools.count(1))
        self.service = FakeService(self.handle)

    def __enter__(self) -> FakeVault:
        self.service.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.service.__exit__(*exc)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.service.requests

    def client(self) -> VaultClient:
        return client_for(self.service.url)

    def superseded_by(self, note_id: str) -> str | None:
        for other in self.notes.values():
            if other.get("supersedes") == note_id:
                return other["id"]
        return None

    def add(self, **fields: Any) -> str:
        note_id = next(self._ids)
        self.notes[note_id] = {
            "id": note_id,
            "topics": [],
            "supersedes": None,
            **fields,
        }
        return note_id

    def handle(self, method: str, path: str, headers, body):
        if path == "/v1/vault_resolve":
            return 200, {"key": body["key"]}, {}
        if path == "/v1/blob_put":
            self.blob_puts += 1
            updated = f"2026-10-08T12:00:{self.blob_puts:02d}.000Z"
            self.blobs[(body["vault"], body["key"])] = {
                "mime": body.get("mime") or "application/octet-stream",
                "content_base64": body["content_base64"],
                "updated": updated,
            }
            return 200, {"updated": updated}, {}
        if path == "/v1/blob_get":
            hit = self.blobs.get((body["vault"], body["key"]))
            if hit is None:
                return 404, {"error": {"code": "not_found", "message": "x"}}, {}
            return 200, {"vault": body["vault"], "key": body["key"], **hit}, {}
        if path == "/v1/write":
            self.writes += 1
            if self.fail_write_at is not None and self.writes >= self.fail_write_at:
                return 503, {"error": {"code": "unavailable"}}, {}
            sup = body.get("supersedes")
            if sup is not None and sup not in self.notes:
                return 404, {"error": {"code": "not_found", "message": sup}}, {}
            note_id = self.add(
                title=body["title"],
                body=body["body"],
                topics=list(body.get("topics") or []),
                links=list(body.get("links") or []),
                supersedes=sup,
                vault=body["vault"],
            )
            return 201, {"id": note_id, "updated": "2026-10-08T12:00:00Z"}, {}
        if path == "/v1/list":
            wanted = set(body.get("topics") or [])
            hits = [
                {**n, "superseded_by": self.superseded_by(n["id"])}
                for n in self.notes.values()
                if not wanted or wanted & set(n["topics"])
            ]
            start = int(body.get("cursor") or 0)
            page = hits[start : start + self.list_page]
            nxt = start + self.list_page
            return (
                200,
                {"items": page, "next_cursor": str(nxt) if nxt < len(hits) else None},
                {},
            )
        if path == "/v1/get":
            items = [
                {**self.notes[i], "superseded_by": self.superseded_by(i)}
                for i in body["ids"]
                if i in self.notes
            ]
            missing = [i for i in body["ids"] if i not in self.notes]
            return 200, {"items": items, "missing": missing}, {}
        return 404, {"error": {"code": "unknown"}}, {}


def client_for(url: str) -> VaultClient:
    settings = load_client_settings(
        None, {"LORE_VAULT_URL": url, "LORE_VAULT_API_TOKEN": "test-token"}
    )
    return VaultClient(settings, timeout=5.0)


def offline_client() -> VaultClient:
    """未設定 URL／token：推送直接略過（不打網路、不讀任何 env 檔）。"""
    return VaultClient(load_client_settings(None, {}), timeout=5.0)


def unreachable_client() -> VaultClient:
    return client_for(closed_port_url())


@pytest.fixture
def vault():
    with FakeVault() as fake:
        yield fake
