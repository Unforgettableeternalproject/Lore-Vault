"""archive：授權閘門、dry-run 先驗、不可達不搬目錄、可續跑、鏈頭與 supersedes。"""

from __future__ import annotations

from .conftest import (
    SPEC_A,
    VAULT,
    FakeVault,
    TasksDir,
    delta,
    requirement,
    unreachable_client,
)

MOD_ROOT = requirement(
    "資料根目錄", "資料 SHALL 存放於 `~/.x/`。", scenarios=("讀取資料根",)
)


def _archive(tasks_dir: TasksDir, name: str, client, *extra: str):
    return tasks_dir.run("archive", name, "--vault", VAULT, *extra, client=client)


def _setup_two_reqs(tasks_dir: TasksDir, name: str = "c1") -> None:
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose(
        name, deltas={"demo": delta(added=[requirement("新功能")], modified=[MOD_ROOT])}
    )


def test_archive_success_writes_notes_merges_and_moves(tasks_dir: TasksDir, vault):
    _setup_two_reqs(tasks_dir)
    code, out = _archive(tasks_dir, "c1", vault.client)
    assert code == 0, out
    assert not tasks_dir.change_dir("c1").exists()
    meta = tasks_dir.meta("c1")
    assert set(meta["notes"]) == {"demo/新功能", "demo/資料根目錄", "summary"}
    assert meta["note_id"] == meta["notes"]["summary"]
    assert meta["spec_applied"] is True and meta["vault"] == VAULT
    assert tasks_dir.archived_dir("c1").name == "2026-10-08-c1"
    req = vault.notes[meta["notes"]["demo/新功能"]]
    assert req["title"] == "req:demo/新功能"
    assert set(req["topics"]) == {"change:c1", "req:demo/新功能"}
    summary = vault.notes[meta["note_id"]]
    assert summary["topics"] == ["change:c1"]
    assert set(summary["links"]) == {
        meta["notes"]["demo/新功能"],
        meta["notes"]["demo/資料根目錄"],
    }
    assert "~/.x/" in tasks_dir.main_spec("demo")
    assert "### Requirement: 新功能" in tasks_dir.main_spec("demo")


def test_archive_preserves_crlf(tasks_dir: TasksDir, vault):
    tasks_dir.write_main("demo", SPEC_A.replace("\n", "\r\n"))
    tasks_dir.propose("c1", deltas={"demo": delta(added=[requirement("新功能")])})
    assert _archive(tasks_dir, "c1", vault.client)[0] == 0
    raw = (tasks_dir.root / "specs" / "demo" / "spec.md").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_authorization_gate_before_any_request(tasks_dir: TasksDir, vault):
    tasks_dir.propose("guarded", "--skip-specs", "--requires-authorization")
    code, out = _archive(tasks_dir, "guarded", vault.client)
    assert code == 1 and "--authorized-by" in out
    assert vault.requests == []
    assert tasks_dir.change_dir("guarded").is_dir()
    code, out = _archive(
        tasks_dir, "guarded", vault.client, "--authorized-by", "艾斯維爾"
    )
    assert code == 0, out
    meta = tasks_dir.meta("guarded")
    assert meta["authorized_by"] == "艾斯維爾"
    assert "授權：艾斯維爾" in vault.notes[meta["note_id"]]["body"]


def test_unreachable_service_keeps_change_and_spec(tasks_dir: TasksDir):
    _setup_two_reqs(tasks_dir)
    spec_before = (tasks_dir.root / "specs" / "demo" / "spec.md").read_bytes()
    code, out = _archive(tasks_dir, "c1", unreachable_client)
    assert code == 1 and "change 留在原處" in out
    assert tasks_dir.change_dir("c1").is_dir()
    assert not list((tasks_dir.root / "changes" / "archive").glob("*-c1"))
    assert (tasks_dir.root / "specs" / "demo" / "spec.md").read_bytes() == spec_before
    meta = tasks_dir.meta("c1")
    assert meta["note_id"] is None and meta["notes"] == {}
    assert "spec_applied" not in meta


def test_partial_failure_resumes_without_rewriting(tasks_dir: TasksDir):
    _setup_two_reqs(tasks_dir)
    spec_before = tasks_dir.main_spec("demo")
    with FakeVault(fail_write_at=2) as flaky:
        code, out = _archive(tasks_dir, "c1", flaky.client)
        assert code == 1
        meta = tasks_dir.meta("c1")
        assert len(meta["notes"]) == 1 and meta["note_id"] is None
        first_key, first_id = next(iter(meta["notes"].items()))
        assert tasks_dir.change_dir("c1").is_dir()
        assert tasks_dir.main_spec("demo") == spec_before
        flaky.fail_write_at = None
        code, out = _archive(tasks_dir, "c1", flaky.client)
        assert code == 0, out
        meta = tasks_dir.meta("c1")
        assert meta["notes"][first_key] == first_id
        titles = [n["title"] for n in flaky.notes.values()]
        assert titles.count(f"req:{first_key}") == 1
        assert len(flaky.notes) == 3
        assert "先前已寫（跳過）" in out


def test_resume_after_spec_applied_only_moves(tasks_dir: TasksDir, vault):
    _setup_two_reqs(tasks_dir)
    assert _archive(tasks_dir, "c1", vault.client)[0] == 0
    # 模擬「主 spec 已併、搬目錄前中斷」：把目錄搬回 active
    archived = tasks_dir.archived_dir("c1")
    archived.rename(tasks_dir.change_dir("c1"))
    spec_after = tasks_dir.main_spec("demo")
    count = len(vault.notes)
    code, out = _archive(tasks_dir, "c1", vault.client)
    assert code == 0, out
    assert len(vault.notes) == count
    assert tasks_dir.main_spec("demo") == spec_after


def test_supersedes_points_to_chain_head(tasks_dir: TasksDir, vault):
    _setup_two_reqs(tasks_dir, "c1")
    assert _archive(tasks_dir, "c1", vault.client)[0] == 0
    first = tasks_dir.meta("c1")["notes"]["demo/資料根目錄"]
    mod2 = requirement(
        "資料根目錄", "資料 SHALL 存放於 `~/.y/`。", scenarios=("讀取資料根",)
    )
    tasks_dir.propose("c2", deltas={"demo": delta(modified=[mod2])})
    code, out = _archive(tasks_dir, "c2", vault.client)
    assert code == 0, out
    second = tasks_dir.meta("c2")["notes"]["demo/資料根目錄"]
    assert vault.notes[second]["supersedes"] == first
    assert vault.notes[tasks_dir.meta("c2")["notes"]["summary"]]["supersedes"] is None


def test_multiple_chain_heads_abort_before_writing(tasks_dir: TasksDir, vault):
    _setup_two_reqs(tasks_dir)
    for _ in range(3):  # 3 則 > list_page=2，順便驗翻頁
        vault.add(title="req:demo/資料根目錄", topics=["req:demo/資料根目錄"])
    code, out = _archive(tasks_dir, "c1", vault.client)
    assert code == 1 and "鏈頭不只一則" in out
    assert vault.writes == 0
    assert tasks_dir.change_dir("c1").is_dir()


def test_archive_runs_full_validation_first(tasks_dir: TasksDir, vault):
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose("c1", deltas={"demo": delta(modified=[MOD_ROOT])})
    tasks_dir.write_main("demo", SPEC_A.replace("`~/.demo/`。", "`~/.z/`。", 1))
    code, out = _archive(tasks_dir, "c1", vault.client)
    assert code == 1 and "base 過時" in out
    assert vault.requests == []


def test_archive_refuses_blocked(tasks_dir: TasksDir, vault):
    tasks_dir.propose("c1", "--skip-specs", "--blocked-by", "D6")
    code, out = _archive(tasks_dir, "c1", vault.client)
    assert code == 1 and "被擋住" in out
    assert vault.requests == []
