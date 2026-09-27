"""階段 8 客戶端：hook 端設定、episode spool 與推送（T-38）、
machine／vault 凍結（T-39）、spool 與 concept 快照的 doctor 對帳。

服務端用 `fake_service.FakeService`（真的 HTTP、`http.server` 執行緒），
因為 hook 客戶端是 `urllib`，httpx 的 MockTransport 攔不到。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.hooks import concept_snapshot, spool
from lore_vault.hooks.client_env import (
    CLIENT_ENV_VAR,
    ClientSettings,
    Secret,
    load_client_settings,
)

from .fake_service import BlackHole, FakeService, closed_port_url

TOKEN = "tok-" + "s" * 32
CF_ID = "cf-id-value-123"
CF_SECRET = "cf-secret-value-456"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="沒有 git")


def _episode(n: int = 0, **extra) -> dict:
    data = {
        "prompt_id": f"p-{n}",
        "turn_index": n,
        "session_id": "s-1",
        "agent": "claude-code",
        "origin": "human",
        "started_at": "2026-09-20T02:00:00.000Z",
        "ended_at": "2026-09-20T02:05:00.000Z",
        "cwd": ["C:/src/demo"],
        "repo": "demo",
        "repo_root": "C:/src/demo",
        "git_branch": ["main"],
        "cc_version": "2.0.0",
        "user_text": "u",
        "assistant_text": "a",
        "injected": [],
        "tool_sequence": [{"name": "Edit", "count": 1}],
        "tool_calls_total": 1,
        "mcp_tools": [],
        "skills": [],
        "files_edited": ["a.py"],
        "files_read": [],
        "symbols_edited": [],
        "thinking_blocks": 0,
    }
    data.update(extra)
    return data


def _settings(url: str, **kw) -> ClientSettings:
    return ClientSettings(env_file=None, url=url, token=Secret(TOKEN), **kw)


def _spool_n(spool_dir: Path, n: int, *, machine="desk-a", vault="folder/demo"):
    for i in range(n):
        spool.write_pending(
            spool_dir, spool.wire_episode(_episode(i), machine=machine, vault=vault)
        )


def _files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


def _all_text(root: Path) -> str:
    return "".join(
        p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file()
    )


# ── 設定 ─────────────────────────────────────────────────────────────


def test_client_env_file_and_environ_precedence(tmp_path):
    env_file = tmp_path / "client.env"
    env_file.write_text(
        "# 註解\n"
        "LORE_VAULT_URL=http://127.0.0.1:5056/\n"
        f'export LORE_VAULT_API_TOKEN="{TOKEN}"\n'
        "LORE_VAULT_PUSH_TIMEOUT=1.5\n"
        "LORE_VAULT_CONCEPT_SNAPSHOT=~/snap/concepts.json\n",
        encoding="utf-8",
    )
    s = load_client_settings(env_file, environ={})
    assert s.push_configured
    assert s.url == "http://127.0.0.1:5056"
    assert s.token is not None and s.token.reveal() == TOKEN
    assert s.push_timeout == 1.5
    assert s.concept_snapshot == Path("~/snap/concepts.json").expanduser()

    override = load_client_settings(
        env_file, environ={"LORE_VAULT_URL": "https://pm-api.example.com"}
    )
    assert override.url == "https://pm-api.example.com"

    moved = tmp_path / "other.env"
    moved.write_text("LORE_VAULT_URL=http://x:1\n", encoding="utf-8")
    via_var = load_client_settings(env_file, environ={CLIENT_ENV_VAR: str(moved)})
    assert via_var.env_file == moved and not via_var.push_configured


def test_client_env_problems_never_leak_secrets(tmp_path):
    env_file = tmp_path / "client.env"
    env_file.write_text(
        f"LORE_VAULT_URL=ftp://x\nLORE_VAULT_API_TOKEN={TOKEN}\n"
        f"CF_ACCESS_CLIENT_ID={CF_ID}\n",
        encoding="utf-8",
    )
    s = load_client_settings(env_file, environ={})
    assert not s.push_configured
    text = s.describe() + repr(s)
    assert "CF_ACCESS_CLIENT_SECRET" in text and "http://" in text
    for secret in (TOKEN, CF_ID):
        assert secret not in text


def test_missing_env_file_is_unconfigured(tmp_path):
    s = load_client_settings(tmp_path / "nope.env", environ={})
    assert not s.push_configured
    assert "LORE_VAULT_URL" in s.describe()


# ── spool 寫入 ───────────────────────────────────────────────────────


def test_spool_is_one_atomic_file_per_turn_and_idempotent(tmp_path):
    _spool_n(tmp_path, 3)
    _spool_n(tmp_path, 3)  # 同一輪重寫覆蓋同一檔
    names = _files(tmp_path / spool.PENDING)
    assert len(names) == 3 and all(n.endswith(".json") for n in names)
    record = json.loads((tmp_path / spool.PENDING / names[0]).read_text("utf-8"))
    assert record["format"] == spool.FORMAT_VERSION
    assert record["episode"]["machine"] == "desk-a"
    assert record["episode"]["vault"] == "folder/demo"
    stats = spool.spool_stats(tmp_path)
    assert (stats.pending, stats.rejected) == (3, 0)


def test_spool_sanitizes_control_characters_before_writing(tmp_path):
    """NUL 等控制字元在寫 spool 前就換成可見形式並計數；推送送出的是清理後的內容。"""
    dirty = _episode(0, user_text="問" + chr(0) + "題", files_read=["a" + chr(1)])
    path = spool.write_pending(
        tmp_path, spool.wire_episode(dirty, machine="desk-a", vault="folder/demo")
    )
    raw = path.read_bytes()
    assert b"u0000" not in raw and b"u0001" not in raw  # 沒有 JSON 跳脫的控制字元
    record = json.loads(raw)
    assert record["sanitized"] == 2
    assert record["episode"]["user_text"] == "問" + chr(92) + "0題"
    assert record["episode"]["files_read"] == ["a" + chr(92) + "x01"]
    assert dirty["user_text"] == "問" + chr(0) + "題"  # 原物件不動
    clean = spool.write_pending(
        tmp_path, spool.wire_episode(_episode(1), machine="desk-a", vault="folder/demo")
    )
    assert "sanitized" not in json.loads(clean.read_text("utf-8"))


def test_derive_vault_falls_back_to_folder(tmp_path):
    cache: dict[str, str] = {}
    gone = str(tmp_path / "Deleted-Repo")
    assert spool.derive_vault(gone, "Deleted-Repo", cache) == "folder/deleted-repo"
    assert cache == {gone: "folder/deleted-repo"}
    assert spool.derive_vault(None, "Demo") == "folder/demo"
    assert spool.derive_vault(None, None) == "folder/unknown"


# ── 推送 ─────────────────────────────────────────────────────────────


def test_push_removes_accepted_and_duplicate_rejects_conflict_invalid(tmp_path):
    statuses = ["accepted", "duplicate", "conflict", "invalid", "weird"]

    def handler(method, path, headers, body):
        results = [
            {"index": i, "status": statuses[i], "error": f"e{i}"}
            for i in range(len(body["episodes"]))
        ]
        return 200, {"results": results}, {}

    _spool_n(tmp_path, 5)
    with FakeService(handler) as svc:
        settings = _settings(svc.url, cf_access=(Secret(CF_ID), Secret(CF_SECRET)))
        result = spool.push_pending(tmp_path, settings)
    assert (result.accepted, result.duplicate, result.rejected, result.kept) == (
        1,
        1,
        2,
        1,
    )
    [req] = svc.requests
    assert (req["method"], req["path"]) == ("POST", "/v1/episodes")
    assert req["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert req["headers"]["cf-access-client-id"] == CF_ID
    assert req["headers"]["cf-access-client-secret"] == CF_SECRET
    assert all("machine" in e and "vault" in e for e in req["body"]["episodes"])

    stats = spool.spool_stats(tmp_path)
    assert stats.pending == 1
    assert stats.rejected_by_status == {"conflict": 1, "invalid": 1}
    assert TOKEN not in _all_text(tmp_path) and CF_SECRET not in _all_text(tmp_path)


def test_push_respects_batch_limit(tmp_path):
    _spool_n(tmp_path, 5)
    with FakeService() as svc:
        result = spool.push_pending(tmp_path, _settings(svc.url, push_batch=2))
        assert result.sent == 2 and spool.spool_stats(tmp_path).pending == 3
        total = spool.push_all(tmp_path, _settings(svc.url, push_batch=2))
    assert total.accepted == 3 and spool.spool_stats(tmp_path).pending == 0
    assert [len(r["body"]["episodes"]) for r in svc.requests] == [2, 2, 1]


@pytest.mark.parametrize(
    "status, payload",
    [
        (500, {"error": {"code": "storage_error"}}),
        (401, {"error": {"code": "unauthorized"}}),
        (302, b""),
        (503, b"down"),
        (200, {"results": []}),  # 筆數對不上
        (200, b"<html>login</html>"),  # 被中間層攔截
    ],
)
def test_push_failures_keep_everything_and_back_off(tmp_path, status, payload):
    _spool_n(tmp_path, 3)
    with FakeService(lambda *a: (status, payload, {})) as svc:
        settings = _settings(svc.url)
        result = spool.push_pending(tmp_path, settings)
        assert result.kept == 3 and result.error
        assert TOKEN not in result.error
        # 退避期間 Stop hook 不再打服務
        again = spool.push_pending(tmp_path, settings)
        assert again.skipped_reason and len(svc.requests) == 1
    assert spool.spool_stats(tmp_path).pending == 3
    state = spool.load_push_state(tmp_path)
    assert state["last_error"] and "backoff_until_ts" in state
    assert TOKEN not in _all_text(tmp_path)


def test_unreachable_service_keeps_spool(tmp_path):
    _spool_n(tmp_path, 2)
    result = spool.push_pending(tmp_path, _settings(closed_port_url()), timeout=1.0)
    assert result.kept == 2 and "連線失敗" in (result.error or "")
    with BlackHole() as hole:
        result = spool.push_pending(
            tmp_path, _settings(hole.url), timeout=0.3, respect_backoff=False
        )
    assert result.kept == 2 and spool.spool_stats(tmp_path).pending == 2


def test_unconfigured_push_only_spools(tmp_path):
    _spool_n(tmp_path, 1)
    unconfigured = ClientSettings(env_file=None)
    result = spool.push_pending(tmp_path, unconfigured)
    assert not result.attempted and "推送未設定" in result.summary()
    assert spool.spool_stats(tmp_path).pending == 1


def test_corrupt_spool_file_is_quarantined(tmp_path):
    _spool_n(tmp_path, 1)
    (tmp_path / spool.PENDING / "broken.json").write_text("{nope", encoding="utf-8")
    with FakeService() as svc:
        result = spool.push_all(tmp_path, _settings(svc.url))
    assert result.accepted == 1 and result.rejected == 1
    assert spool.spool_stats(tmp_path).rejected_by_status == {"corrupt": 1}


# ── T-39：machine／vault 寫入時凍結 ─────────────────────────────────


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@needs_git
def test_machine_and_vault_are_frozen_at_spool_time(tmp_path, monkeypatch):
    """寫入 spool 後 repo 改名、remote 改名、換機器；重播送出的仍是寫入當下的值。"""
    import platform

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "OldName"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "https://github.com/me/OldName.git")
    monkeypatch.setattr(platform, "node", lambda: "desk-a")

    spool_dir = tmp_path / "spool"
    ep = _episode(0, repo="OldName", repo_root=str(repo))
    spool.spool_episodes(
        spool_dir,
        [ep],
        machine=platform.node(),
        vault_for=lambda e: spool.derive_vault(e["repo_root"], e["repo"]),
    )

    # 之後：改名、換 remote、換機器
    new = tmp_path / "NewName"
    repo.rename(new)
    _git(new, "remote", "set-url", "origin", "https://github.com/me/NewName.git")
    monkeypatch.setattr(platform, "node", lambda: "laptop-b")
    assert spool.derive_vault(str(new), "NewName") == "github.com/me/newname"

    with FakeService() as svc:
        spool.push_all(spool_dir, _settings(svc.url))
    [sent] = svc.requests[0]["body"]["episodes"]
    assert sent["machine"] == "desk-a"
    assert sent["vault"] == "github.com/me/oldname"
    assert sent["repo_root"] == str(repo)


# ── doctor：spool ────────────────────────────────────────────────────


def _doctor(settings: dict, categories: list[str]):
    return default_registry().run(
        DoctorContext(settings=settings), categories=categories
    )


def _outcome(report, name: str):
    return next(o.result for o in report.outcomes if o.name == name)


def _write_client_env(path: Path, url: str) -> None:
    path.write_text(f"LORE_VAULT_URL={url}\nLORE_VAULT_API_TOKEN={TOKEN}\n", "utf-8")


def test_doctor_spool_turns_red_when_push_is_broken(tmp_path):
    """推送故障（服務回 500）→ pending 留著 → 超過門檻時 spool.pending 為 fail；
    修好後推送清空 → pass。"""
    spool_dir = tmp_path / "spool"
    _write_client_env(tmp_path / "client.env", "http://127.0.0.1:1")
    _spool_n(spool_dir, 2)
    settings = {"spool_dir": str(spool_dir), "now": datetime.now(UTC)}

    with FakeService(lambda *a: (500, {"error": {}}, {})) as broken:
        spool.push_pending(spool_dir, _settings(broken.url))
    later = dict(settings, now=datetime.now(UTC) + timedelta(hours=25))
    result = _outcome(_doctor(later, ["spool"]), "spool.pending")
    assert result.status is Status.FAIL
    assert result.counts["pending"] == 2
    assert any("上次推送失敗" in d for d in result.details)
    warn = _outcome(
        _doctor(dict(settings, now=datetime.now(UTC) + timedelta(hours=2)), ["spool"]),
        "spool.pending",
    )
    assert warn.status is Status.WARN

    with FakeService() as fixed:
        spool.push_all(spool_dir, _settings(fixed.url))
    result = _outcome(_doctor(later, ["spool"]), "spool.pending")
    assert result.status is Status.PASS and result.counts["pending"] == 0


def test_doctor_spool_reports_unconfigured_push(tmp_path):
    spool_dir = tmp_path / "spool"
    _spool_n(spool_dir, 1)
    report = _doctor({"spool_dir": str(spool_dir), "now": datetime.now(UTC)}, ["spool"])
    result = _outcome(report, "spool.pending")
    assert result.status is Status.WARN and "推送未設定" in result.summary


def test_doctor_spool_conflicts_is_red(tmp_path):
    spool_dir = tmp_path / "spool"
    _spool_n(spool_dir, 2)
    ok = _outcome(_doctor({"spool_dir": str(spool_dir)}, ["spool"]), "spool.conflicts")
    assert ok.status is Status.PASS

    def handler(method, path, headers, body):
        return 200, {"results": [{"status": "conflict"}, {"status": "accepted"}]}, {}

    with FakeService(handler) as svc:
        spool.push_pending(spool_dir, _settings(svc.url))
    bad = _outcome(_doctor({"spool_dir": str(spool_dir)}, ["spool"]), "spool.conflicts")
    assert bad.status is Status.FAIL and bad.counts["conflict"] == 1


def test_doctor_spool_skipped_without_dir():
    report = _doctor({}, ["spool", "concept_snapshot"])
    assert {o.result.status for o in report.outcomes} == {Status.SKIPPED}


# ── doctor：concept 快照 ─────────────────────────────────────────────


CONCEPTS = [{"id": "c1", "surprisal": 0.9, "anchors": ["a.py", "foo"], "scope": None}]


def test_doctor_concept_snapshot_age(tmp_path):
    path = tmp_path / "concepts.json"
    settings = {"concept_snapshot": str(path), "now": NOW}
    missing = _outcome(_doctor(settings, ["concept_snapshot"]), "concept_snapshot.age")
    assert missing.status is Status.FAIL and "從未拉取" in missing.summary

    concept_snapshot.install(path, json.dumps(CONCEPTS).encode(), now=NOW)
    fresh = _outcome(_doctor(settings, ["concept_snapshot"]), "concept_snapshot.age")
    assert fresh.status is Status.PASS and fresh.counts["concepts"] == 1

    old = dict(settings, now=NOW + timedelta(hours=25))
    stale = _outcome(_doctor(old, ["concept_snapshot"]), "concept_snapshot.age")
    assert stale.status is Status.FAIL

    concept_snapshot.mark_checked(path, now=NOW + timedelta(hours=24, minutes=30))
    rechecked = _outcome(_doctor(old, ["concept_snapshot"]), "concept_snapshot.age")
    assert rechecked.status is Status.PASS  # 304 也算確認過

    path.write_text("[]", encoding="utf-8")  # 被改動：與 manifest 不一致
    tampered = _outcome(_doctor(settings, ["concept_snapshot"]), "concept_snapshot.age")
    assert tampered.status is Status.FAIL and "sha256" in tampered.summary


def test_concept_install_rejects_bad_payload_and_keeps_old(tmp_path):
    path = tmp_path / "concepts.json"
    good = json.dumps(CONCEPTS).encode()
    concept_snapshot.install(path, good, now=NOW)
    for bad in (b"{nope", b'{"a": 1}', b"[1, 2]"):
        with pytest.raises(concept_snapshot.ConceptSnapshotError):
            concept_snapshot.install(path, bad)
    with pytest.raises(concept_snapshot.ConceptSnapshotError):
        concept_snapshot.install(path, good, expected_sha256="0" * 64)
    assert path.read_bytes() == good
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_load_for_injection_degrades_without_raising(tmp_path):
    path = tmp_path / "concepts.json"
    assert concept_snapshot.load_for_injection(path) == ([], "concept 快照不存在")
    path.write_text("{broken", encoding="utf-8")
    concepts, reason = concept_snapshot.load_for_injection(path)
    assert concepts == [] and reason and "損毀" in reason
    path.write_text(json.dumps(CONCEPTS), encoding="utf-8")
    assert concept_snapshot.load_for_injection(path) == (CONCEPTS, None)


def test_doctor_cli_flags(tmp_path, capsys):
    from lore_vault.doctor.command import main

    spool_dir = tmp_path / "spool"
    _spool_n(spool_dir, 1)
    code = main(
        [
            "--json",
            "--category",
            "spool",
            "--spool-dir",
            str(spool_dir),
            "--client-env",
            str(tmp_path / "none.env"),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0  # 推送未設定是 warn，不影響 exit code
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["spool.pending"]["status"] == "warn"
    assert by_name["spool.pending"]["counts"]["pending"] == 1


# ── doctor：hook 與 MCP 的 concept 快照路徑一致 ─────────────────────


def _snapshot_env(path: Path, snapshot: Path | None) -> Path:
    lines = ["LORE_VAULT_URL=http://127.0.0.1:1", f"LORE_VAULT_API_TOKEN={TOKEN}"]
    if snapshot is not None:
        lines.append(f"LORE_VAULT_CONCEPT_SNAPSHOT={snapshot}")
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path


def _agreement(settings: dict):
    report = _doctor(settings, ["concept_snapshot"])
    return _outcome(report, "concept_snapshot.path_agreement")


def test_doctor_concept_snapshot_paths_must_agree(tmp_path):
    shared = tmp_path / "snap" / "concepts.json"
    env = _snapshot_env(tmp_path / "client.env", shared)

    same = _agreement(
        {"client_env": str(env), "mcp_concept_snapshot_path": str(shared)}
    )
    assert same.status is Status.PASS

    # 同一檔的不同寫法（相對段、大小寫在 Windows 上）仍視為同檔
    alias = tmp_path / "snap" / ".." / "snap" / "concepts.json"
    assert (
        _agreement(
            {"client_env": str(env), "mcp_concept_snapshot_path": str(alias)}
        ).status
        is Status.PASS
    )

    other = tmp_path / "mcp" / "concepts.json"
    diff = _agreement({"client_env": str(env), "mcp_concept_snapshot_path": str(other)})
    assert diff.status is Status.FAIL
    assert any(str(other) in d for d in diff.details)


def test_doctor_concept_snapshot_paths_derive_from_config(tmp_path):
    """未直接給 MCP 路徑時由設定推導（與殼相同）：
    未設 concept_snapshot_path 就用 snapshot_dir。"""
    snap_dir = tmp_path / "snap"
    env = _snapshot_env(tmp_path / "client.env", tmp_path / "elsewhere.json")
    config = tmp_path / "lore-vault.toml"
    config.write_text(f"[mcp]\nsnapshot_dir = '{snap_dir.as_posix()}'\n", "utf-8")
    settings = {"client_env": str(env), "config": str(config), "environ": {}}
    assert _agreement(settings).status is Status.FAIL

    _snapshot_env(env, snap_dir / "concepts.json")
    assert _agreement(settings).status is Status.PASS


def test_doctor_concept_snapshot_paths_skipped_when_either_side_unset(tmp_path):
    env_unset = _snapshot_env(tmp_path / "a.env", None)
    assert (
        _agreement(
            {"client_env": str(env_unset), "mcp_concept_snapshot_path": "x.json"}
        ).status
        is Status.SKIPPED
    )
    env = _snapshot_env(tmp_path / "b.env", tmp_path / "s.json")
    no_mcp = {"client_env": str(env), "config": None, "environ": {}}
    assert _agreement(no_mcp).status is Status.SKIPPED
    assert _agreement({}).status is Status.SKIPPED
