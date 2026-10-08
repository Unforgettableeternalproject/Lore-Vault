"""CLI 的服務端同步模式（MCP-T6）：`config.yaml` 標 `remote: true` 的專案。

CLI 的 `VaultClient` 經 `TestClient` 打真正的 `create_app`（同一個 sqlite），
MCP 的部分以 HTTP 殼另外接同一個資料庫，驗證兩條路徑看到同一份服務端內容：

- 本機修改以 `remote_version` 做 CAS 推送；衝突時拒絕並提示 pull，不覆寫服務端
- validate 先對齊工作副本（拉服務端修改），record_base 改到的 base 推回服務端
- archive 一次做完兩段；MCP 已做完段一的 change 只落地、不重寫 note
- 需授權的 change 只認 UI 核准紀錄（內容雜湊相符）；同步模式帶 `--authorized-by`
  在網路呼叫之前拒絕（只在 `--offline` 有效）；段二落地前再查一次紀錄
- `--offline` 是純本機模式；本機模式的 archive 拒絕已同步到服務端的 change
- 服務不可達：propose／validate 退回本機並警告
- 快照一律由服務端內容計算
"""

from __future__ import annotations

import base64
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml
from fastapi.testclient import TestClient

from lore_vault.api import routes as api_routes
from lore_vault.hooks.client_env import load_client_settings
from lore_vault.storage import sidecar as storage_sidecar
from lore_vault.storage.db import connect
from lore_vault.tasks import cli, snapshot
from lore_vault.tasks import remote_store as rs
from lore_vault.tasks.vault_client import (
    ServiceRejected,
    ServiceUnavailable,
    VaultClient,
)
from lore_vault.tasks.workspace import load_workspace, trial_merge

from .conftest import TOKEN, add_vault
from .test_tasks_archive_mcp import DONE_TASKS, _archive, notes, put_authorization
from .test_tasks_mcp import (
    DECISIONS,
    DELTA_ADDED,
    DELTA_MODIFIED,
    MAIN_SPEC,
    MODE_HTTP,
    VAULT,
    Tasks,
    _direct_post,
    blob,
    client_for,
    make_app,
    make_shell,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class AppClient(VaultClient):
    """同步 `VaultClient`，請求經 `TestClient` 送進 app（不開網路埠）。
    `down=True` 模擬服務不可達；`paths` 記錄每個請求。"""

    def __init__(self, http: TestClient) -> None:
        super().__init__(
            load_client_settings(
                None,
                {"LORE_VAULT_URL": "http://testserver", "LORE_VAULT_API_TOKEN": TOKEN},
            ),
            timeout=5.0,
        )
        self.http = http
        self.down = False
        self.paths: list[str] = []

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        self.paths.append(path)
        if self.down:
            raise ServiceUnavailable("連線失敗（模擬）")
        resp = self.http.post(
            path, json=body, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if resp.status_code >= 400:
            raise ServiceRejected(f"HTTP {resp.status_code}", resp.status_code, payload)
        return payload


class Cli:
    def __init__(self, project: Path, client: AppClient) -> None:
        self.project = project
        self.root = project / "openspec"
        self.client = client
        self.err = ""

    def run(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            ["--root", str(self.root), *argv],
            stdout=out,
            environ={},
            cwd=self.project,
            client_factory=lambda: self.client,
            now=NOW,
            stderr=err,
        )
        self.err = err.getvalue()
        return code, out.getvalue()

    def ok(self, *argv: str) -> str:
        code, out = self.run(*argv)
        assert code == 0, out + self.err
        return out

    def change(self, name: str) -> Path:
        return self.root / "changes" / name

    def meta(self, name: str) -> dict[str, Any]:
        return yaml.safe_load(
            (self.change(name) / ".openspec.yaml").read_text(encoding="utf-8")
        )

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def spec(self) -> str:
        return (self.root / "specs" / "demo" / "spec.md").read_bytes().decode("utf-8")


def http_call(db_path: Path, project: Path, **args: Any) -> tuple[bool, dict]:
    """以 HTTP 模式 MCP 殼呼叫一次 tasks（另一個 app 實例、同一個資料庫）。"""

    async def go() -> tuple[bool, dict]:
        async with client_for(make_shell(make_app(db_path), project, MODE_HTTP)) as hc:
            return await Tasks(hc).call(**args)

    return anyio.run(go)


def http_ok(db_path: Path, project: Path, **args: Any) -> dict:
    is_error, payload = http_call(db_path, project, vault=VAULT, **args)
    assert not is_error, payload
    return payload


def http_archive(db_path: Path, project: Path, name: str) -> dict:
    async def go() -> dict:
        async with client_for(make_shell(make_app(db_path), project, MODE_HTTP)) as hc:
            return await _archive(Tasks(hc), name)

    return anyio.run(go)


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    for var in ("LORE_VAULT_TASKS_ROOT", "LORE_VAULT_TASKS_DECISIONS"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "demo"
    (path / "openspec" / "specs" / "demo").mkdir(parents=True)
    (path / "openspec" / "specs" / "demo" / "spec.md").write_bytes(
        MAIN_SPEC.encode("utf-8")
    )
    (path / "openspec" / "config.yaml").write_bytes(
        b"schema: spec-driven\ndecisions_file: DECISIONS.md\n"
    )
    (path / "DECISIONS.md").write_bytes(DECISIONS.encode("utf-8"))
    return path


@pytest.fixture
def remote(db_path, project):
    add_vault(db_path, VAULT)
    with TestClient(make_app(db_path)) as http:
        c = Cli(project, AppClient(http))
        out = c.ok("init", "--remote")
        assert "服務端任務層" in out
        yield c


def _ready(c: Cli, name: str, delta: str = DELTA_MODIFIED, *flags: str) -> None:
    """CLI propose → 寫 delta、勾完 tasks → validate --record-base（推上服務端）。"""
    c.ok("propose", name, *flags)
    c.write(f"changes/{name}/specs/demo/spec.md", delta)
    c.write(f"changes/{name}/tasks.md", DONE_TASKS)
    out = c.ok("validate", name, "--record-base")
    assert "[ OK ]" in out, out


# ── init／propose ──────────────────────────────────────────────────


def test_init_remote_marks_config_and_pushes_mirrors(remote: Cli, db_path):
    config = (remote.root / "config.yaml").read_text(encoding="utf-8")
    assert "decisions_file: DECISIONS.md" in config and "remote: true" in config
    assert blob(db_path, "task-index")["changes"] == {}
    assert blob(db_path, "task-spec-mirror:demo")["text"] == MAIN_SPEC
    assert blob(db_path, "task-decisions")["decisions"] == {"D6": False, "D12": True}
    remote.ok("init")
    config = (remote.root / "config.yaml").read_text(encoding="utf-8")
    assert config.count("remote: true") == 1


def test_propose_creates_server_change_and_working_copy(remote: Cli, db_path):
    out = remote.ok("propose", "c1", "--goal", "g")
    assert "服務端 v1" in out
    meta = remote.meta("c1")
    assert meta["remote_version"] == 1 and meta["goal"] == "g"
    doc = blob(db_path, "task-change:c1")
    assert doc["_version"] == 1 and "remote_version" not in doc["meta"]
    assert blob(db_path, "task-index")["changes"]["c1"] == {"state": "active"}


# ── push（CAS）／pull ───────────────────────────────────────────────


def test_push_conflict_never_overwrites_server(remote: Cli, db_path, project):
    remote.ok("propose", "c1")
    http_ok(
        db_path,
        project,
        action="edit",
        name="c1",
        expected_version=1,
        tasks_md="- [x] 遠端\n",
    )
    remote.write("changes/c1/tasks.md", "- [ ] 本機\n")
    # 兩邊都改過：不推、不覆寫
    code, out = remote.run("push", "c1")
    assert code == 1 and "未覆寫任一邊" in out and "pull c1 --overwrite" in out
    assert blob(db_path, "task-change:c1")["tasks_md"] == "- [x] 遠端\n"
    # 本機看不出落後（沒有同步雜湊）時，仍由 remote_version 的 CAS 擋下
    meta = remote.meta("c1")
    meta.pop("remote_digest")
    remote.write("changes/c1/.openspec.yaml", yaml.safe_dump(meta, allow_unicode=True))
    code, out = remote.run("push", "c1")
    assert code == 1 and "版本衝突" in out and "v1" in out and "v2" in out
    doc = blob(db_path, "task-change:c1")
    assert doc["tasks_md"] == "- [x] 遠端\n" and doc["_version"] == 2

    # pull --overwrite 取回後再改、再推
    code, out = remote.run("pull", "c1")
    assert code == 1 and "--overwrite" in out
    remote.ok("pull", "c1", "--overwrite")
    remote.write("changes/c1/tasks.md", "- [x] 遠端\n- [ ] 本機合併\n")
    out = remote.ok("push", "c1")
    assert "推送到服務端 v3" in out
    assert blob(db_path, "task-change:c1")["tasks_md"].endswith("本機合併\n")
    assert remote.meta("c1")["remote_version"] == 3


def test_validate_pulls_remote_edits_and_pushes_base(remote: Cli, db_path, project):
    remote.ok("propose", "c1")
    http_ok(
        db_path,
        project,
        action="edit",
        name="c1",
        expected_version=1,
        deltas={"demo": DELTA_MODIFIED},
    )
    out = remote.ok("validate", "c1", "--record-base")
    assert "已從服務端取回 v2" in out and "已記錄 base" in out and "[ OK ] c1" in out
    assert remote.change("c1").joinpath("specs/demo/spec.md").is_file()
    doc = blob(db_path, "task-change:c1")
    assert "demo/資料根目錄" in doc["meta"]["base"] and doc["_version"] == 3
    assert remote.meta("c1")["remote_version"] == 3


# ── archive ────────────────────────────────────────────────────────


def test_cli_archive_runs_both_phases_once(remote: Cli, db_path, project):
    _ready(remote, "c1")
    out = remote.ok("archive", "c1")
    assert "已封存 c1" in out and "本次寫入" in out
    assert len(notes(db_path)) == 2
    assert "~/.demo2/" in remote.spec()
    assert not remote.change("c1").exists()
    assert (remote.root / "changes" / "archive" / "2026-10-08-c1").is_dir()
    doc = blob(db_path, "task-change:c1")
    assert doc["state"] == "archived" and doc["meta"]["note_id"]
    # MCP 不能再對它封存，note 不重寫
    is_error, err = http_call(
        db_path, project, action="archive", vault=VAULT, name="c1"
    )
    assert is_error and err["error"]["code"] == "change_not_active"
    assert len(notes(db_path)) == 2


def test_cli_lands_mcp_phase_one_without_rewriting_notes(remote: Cli, db_path, project):
    _ready(remote, "c1")
    http_archive(db_path, project, "c1")
    assert blob(db_path, "task-change:c1")["state"] == "pending_apply"
    written = notes(db_path)
    out = remote.ok("archive", "c1")
    assert "段一先前已完成" in out
    assert notes(db_path) == written
    assert blob(db_path, "task-change:c1")["state"] == "archived"
    assert "~/.demo2/" in remote.spec()


def test_cli_sync_mode_requires_ui_authorization(remote: Cli, db_path):
    remote.ok("propose", "c1", "--skip-specs", "--requires-authorization")
    remote.write("changes/c1/tasks.md", DONE_TASKS)
    remote.ok("push", "c1")
    # 同步模式帶 --authorized-by：在任何網路呼叫之前拒絕，提示到 UI 核准
    remote.client.paths.clear()
    code, out = remote.run("archive", "c1", "--authorized-by", "艾斯維爾")
    assert code == 1 and "--authorized-by" in out and "UI" in out
    assert "--offline" in out
    assert remote.client.paths == []
    # 沒有 UI 核准紀錄：authorization_required，不寫任何 note
    code, out = remote.run("archive", "c1")
    assert code == 1 and "UI 核准" in out
    assert notes(db_path) == []
    assert blob(db_path, "task-change:c1")["state"] == "active"
    # UI 核准目前內容後才能封存；meta 抄下的是 UI 紀錄（沒有 source: cli）
    put_authorization(db_path, "c1", blob(db_path, "task-change:c1")["_version"])
    out = remote.ok("archive", "c1")
    assert "UI 核准：艾斯維爾" in out
    doc = blob(db_path, "task-change:c1")
    assert doc["state"] == "archived"
    auth = doc["meta"]["authorization"]
    assert "source" not in auth and auth["authorized_by"] == "艾斯維爾"
    assert auth["content_digest"] == rs.authorization_digest(doc)
    assert doc["meta"]["authorized_by"] == "艾斯維爾"
    assert all("授權：艾斯維爾" in n["body"] for n in notes(db_path))


def _forge_pending_apply(db_path: Path, name: str, merged: str) -> None:
    """繞過 `/v1/blob_put` 守衛（直接寫儲存層）把需授權 change 改成 pending_apply，
    模擬服務端守衛失效時的防禦縱深：段二落地必須自己重查授權紀錄。"""
    doc = {
        k: v for k, v in blob(db_path, rs.change_key(name)).items() if k != "_version"
    }
    doc["state"] = "pending_apply"
    doc["meta"]["note_id"] = "01FAKE"
    doc["apply"] = {
        "archived_at": "2026-10-08T12:00:00Z",
        "merged_specs": {"demo": merged},
        "mirror_versions": {},
    }
    conn = connect(db_path)
    try:
        storage_sidecar.put(
            conn,
            VAULT,
            rs.change_key(name),
            rs.encode(doc),
            space="dev",
            mime="application/json",
        )
    finally:
        conn.close()


@pytest.mark.parametrize("approved", [False, True])
def test_sync_specs_rechecks_authorization_before_landing(
    remote: Cli, db_path, approved, monkeypatch
):
    _ready(remote, "c1", DELTA_ADDED, "--requires-authorization")
    # 假設服務端守衛失效（段二自己的寫入也不擋）：只剩落地前的授權檢查
    monkeypatch.setattr(
        api_routes,
        "guard_change_write",
        lambda *a, expected_version, **kw: expected_version,
    )
    before = remote.spec()
    if approved:
        # 核准的是另一份內容（之後被改過）：雜湊不符，同樣不落地
        put_authorization(db_path, "c1", 1, digest="0" * 64)
    # 偽造的 merged_specs 與 delta 真正併入的結果相同：
    # 拿掉落地前的授權檢查就會寫進主 spec
    ws = load_workspace(remote.root, environ={})
    merged, errors = trial_merge(ws.find_active("c1"), ws)
    assert not errors and merged["demo"] != before
    _forge_pending_apply(db_path, "c1", merged["demo"])
    code, out = remote.run("sync-specs")
    assert code == 1 and "核准" in out, out
    assert remote.spec() == before
    assert blob(db_path, "task-change:c1")["state"] == "pending_apply"


def _tamper_decisions(remote: Cli, decisions: dict[str, bool]) -> None:
    """持 bearer 者直接改服務端的 DECISIONS 鏡像（純 HTTP，不經任何工具）。"""
    doc = {"schema": 1, "decisions": decisions, "source_digest": "0" * 64}
    remote.client._post(
        "/v1/blob_put",
        {
            "space": "dev",
            "vault": VAULT,
            "key": rs.DECISIONS_KEY,
            "mime": "application/json",
            "content_base64": base64.b64encode(rs.encode(doc)).decode("ascii"),
        },
    )


def _blocked_and_archived_over_http(remote: Cli, db_path, project) -> str:
    """blocked_by D6（本機未裁決）→ 竄改鏡像把 D6 標成已裁決
    → HTTP archive 段一成功。"""
    _ready(remote, "c1", DELTA_ADDED, "--blocked-by", "D6")
    before = remote.spec()
    _tamper_decisions(remote, {"D6": True, "D12": True})
    done = http_archive(db_path, project, "c1")
    assert done["executed"] is True
    assert blob(db_path, "task-change:c1")["state"] == "pending_apply"
    return before


def test_land_rechecks_blocked_by_with_local_decisions(remote: Cli, db_path, project):
    before = _blocked_and_archived_over_http(remote, db_path, project)
    remote.client.paths.clear()
    code, out = remote.run("sync-specs")
    assert code == 1 and "D6" in out and "擋住" in out, out
    # 零寫入：主 spec、工作副本、服務端狀態都不動
    assert remote.spec() == before
    assert remote.change("c1").is_dir()
    assert blob(db_path, "task-change:c1")["state"] == "pending_apply"
    assert "/v1/blob_put" not in remote.client.paths
    # doctor 以本機 DECISIONS 重查：fail
    code, out = remote.run("doctor", "--json", "--vault", VAULT)
    report = {c["name"]: c for c in json.loads(out)["checks"]}
    blocked = report["tasks.blocked_archive"]
    assert blocked["status"] == "fail" and any("D6" in d for d in blocked["details"])


def test_land_without_local_decisions_warns(remote: Cli, db_path, project):
    _blocked_and_archived_over_http(remote, db_path, project)
    (project / "DECISIONS.md").unlink()
    out = remote.ok("sync-specs")
    assert "已落地 c1" in out
    assert "沒有 DECISIONS.md" in remote.err and "D6" in remote.err


def test_offline_archive_refuses_server_tracked_change(remote: Cli, db_path):
    remote.ok("propose", "c1", "--skip-specs")
    remote.write("changes/c1/tasks.md", DONE_TASKS)
    remote.ok("push", "c1")
    remote.client.paths.clear()
    code, out = remote.run("archive", "c1", "--offline")
    assert code == 1 and "已同步到服務端" in out
    assert remote.client.paths == []
    assert notes(db_path) == []
    assert remote.change("c1").is_dir()


# ── --offline／服務不可達 ──────────────────────────────────────────


def test_offline_flag_is_pure_local(remote: Cli, db_path):
    remote.client.paths.clear()
    out = remote.ok("propose", "c2", "--skip-specs", "--offline")
    assert "已建立" in out and "服務端" not in out
    assert "remote_version" not in remote.meta("c2")
    code, out = remote.run("list", "--json", "--offline")
    assert code == 0 and [r["change"] for r in json.loads(out)] == ["c2"]
    assert remote.ok("validate", "c2", "--offline").strip() == "[ OK ] c2"
    assert remote.client.paths == []
    assert blob(db_path, "task-change:c2") is None


def test_unreachable_service_falls_back_to_local(remote: Cli, db_path):
    remote.client.down = True
    out = remote.ok("propose", "c3", "--skip-specs")
    assert "已建立" in out and "只建在本機" in remote.err
    assert "remote_version" not in remote.meta("c3")
    code, out = remote.run("validate", "c3")
    assert code == 0 and "[ OK ] c3" in out and "服務不可達" in remote.err
    code, out = remote.run("push", "c3")
    assert code == 1 and "服務不可達" in out
    remote.client.down = False
    assert "已在服務端建立 v1" in remote.ok("push", "c3")
    assert remote.meta("c3")["remote_version"] == 1


# ── sync-specs ─────────────────────────────────────────────────────


def test_cli_sync_specs_checks_local_base(remote: Cli, db_path, project):
    _ready(remote, "c1", DELTA_ADDED)
    http_archive(db_path, project, "c1")
    edited = MAIN_SPEC.replace("示範。", "示範（別人改過）。")
    remote.write("specs/demo/spec.md", edited)
    code, out = remote.run("sync-specs")
    assert code == 1 and "不一致" in out and "demo" in out
    assert remote.spec() == edited
    remote.write("specs/demo/spec.md", MAIN_SPEC)
    out = remote.ok("sync-specs")
    assert "已落地 c1" in out and "### Requirement: 匯出" in remote.spec()


def test_remote_only_commands_need_marker(tmp_path):
    root = tmp_path / "openspec"
    out = io.StringIO()
    assert cli.main(["--root", str(root), "init"], stdout=out, environ={}) == 0
    code = cli.main(["--root", str(root), "sync-specs"], stdout=out, environ={})
    assert code == 1 and "init --remote" in out.getvalue()


# ── 快照：服務端內容計算 ───────────────────────────────────────────


def test_cli_snapshot_is_computed_from_server(remote: Cli, db_path, project):
    remote.ok("propose", "c1")
    http_ok(db_path, project, action="propose", name="server-only")
    remote.ok("propose", "local-only", "--offline")
    remote.ok("list")
    names = [c["name"] for c in blob(db_path, "tasks-snapshot")["changes"]]
    assert names == ["c1", "server-only"]

    async def expected() -> bytes:
        store = rs.RemoteStore(_direct_post(make_app(db_path)), VAULT)
        return await snapshot.remote_snapshot_bytes(store)

    conn = connect(db_path)
    try:
        pushed = storage_sidecar.get(conn, VAULT, "tasks-snapshot", space="dev")
    finally:
        conn.close()
    assert pushed.content == anyio.run(expected)
    rows = {r["change"]: r for r in json.loads(remote.ok("list", "--json"))}
    assert rows["server-only"]["state"] == "active" and rows["c1"]["version"] == 1
    assert "local-only" not in rows
