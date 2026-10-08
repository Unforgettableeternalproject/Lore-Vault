"""任務層 doctor 七項檢查：每項各有一個「通過」與一個「破壞後變紅」的情境。"""

from __future__ import annotations

import datetime as dt
import io
import json
import shutil

import yaml

from lore_vault.doctor.framework import DoctorContext
from lore_vault.tasks import checks, cli

from .conftest import NOW, SPEC_A, VAULT, TasksDir, delta, requirement

MOD_ROOT = requirement(
    "資料根目錄", "資料 SHALL 存放於 `~/.x/`。", scenarios=("讀取資料根",)
)


def _report(tasks_dir: TasksDir, client=None) -> dict[str, dict]:
    # 沒給假服務就 --offline：不可退回讀真實的 ~/.lore-vault/client.env
    flags = () if client else ("--offline",)
    code, out = tasks_dir.run("doctor", "--json", *flags, client=client)
    data = json.loads(out)
    assert data["exit_code"] == code
    return {c["name"]: c for c in data["checks"]}


def _status(tasks_dir: TasksDir, name: str, client=None) -> str:
    return _report(tasks_dir, client)[name]["status"]


def _archived(tasks_dir: TasksDir, vault, name="c1", block=MOD_ROOT, minutes=0) -> None:
    """`minutes`：封存時間偏移，讓先後順序由 archived_at 決定、不靠目錄名。"""
    if not (tasks_dir.root / "specs" / "demo" / "spec.md").exists():
        tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose(name, deltas={"demo": delta(modified=[block])})
    now = NOW + dt.timedelta(minutes=minutes)
    code, out = tasks_dir.run(
        "archive", name, "--vault", VAULT, client=vault.client, now=now
    )
    assert code == 0, out


def _edit_archived_meta(tasks_dir: TasksDir, name: str, **changes) -> None:
    path = tasks_dir.archived_dir(name) / ".openspec.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_no_tasks_root_all_skipped_exit_0(tmp_path):
    out = io.StringIO()
    code = cli.main(
        ["doctor", "--json"], stdout=out, environ={}, cwd=tmp_path, client_factory=None
    )
    data = json.loads(out.getvalue())
    assert code == 0
    assert data["summary"]["total"] == 7
    assert data["summary"]["skipped"] == 7


def test_registry_has_seven_checks_and_core_doctor_does_not():
    names = {c.name for c in checks.default_registry().checks}
    assert names == {
        "tasks.isolation",
        "tasks.archive_note_agreement",
        "tasks.requirement_overlap",
        "tasks.blocked_decision_resolvable",
        "tasks.supersedes_chain",
        "tasks.spec_delta_applied",
        "tasks.dependency_exists",
    }
    from lore_vault.doctor import default_registry

    assert not [c for c in default_registry().checks if c.name.startswith("tasks.")]


def test_all_pass_on_healthy_workspace(tasks_dir: TasksDir, vault):
    _archived(tasks_dir, vault)
    report = _report(tasks_dir, vault.client)
    assert {n: c["status"] for n, c in report.items()} == dict.fromkeys(report, "pass")


def test_isolation_fails_on_injected_import(tasks_dir: TasksDir, tmp_path):
    pkg = tmp_path / "pkg" / "lore_vault"
    (pkg / "notes").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "notes" / "x.py").write_text("import lore_vault.tasks\n", encoding="utf-8")
    ctx = DoctorContext({"tasks_root": str(tasks_dir.root), "package_root": str(pkg)})
    result = checks.isolation(ctx)
    assert result.status == "fail"


def test_archive_note_agreement(tasks_dir: TasksDir, vault):
    _archived(tasks_dir, vault)
    assert _status(tasks_dir, "tasks.archive_note_agreement", vault.client) == "pass"
    # note 被刪（服務端不存在）
    note_id = tasks_dir.meta("c1")["note_id"]
    saved = vault.notes.pop(note_id)
    assert _status(tasks_dir, "tasks.archive_note_agreement", vault.client) == "fail"
    # topics 缺 change:<name>
    vault.notes[note_id] = {**saved, "topics": []}
    assert _status(tasks_dir, "tasks.archive_note_agreement", vault.client) == "fail"
    vault.notes[note_id] = saved
    # 刪掉 archive 目錄的 note_id 欄位
    _edit_archived_meta(tasks_dir, "c1", note_id=None)
    assert _status(tasks_dir, "tasks.archive_note_agreement", vault.client) == "fail"
    # 明確標註的試用期封存 → 不算 fail
    _edit_archived_meta(tasks_dir, "c1", legacy_archive="OpenSpec 試用")
    assert _status(tasks_dir, "tasks.archive_note_agreement", vault.client) == "pass"


def test_archive_note_agreement_warns_half_written(tasks_dir: TasksDir, vault):
    tasks_dir.propose("half", "--skip-specs")
    tasks_dir.set_meta("half", notes={"summary": "n99"})
    assert _status(tasks_dir, "tasks.archive_note_agreement", vault.client) == "warn"


def test_service_checks_skipped_without_client(tasks_dir: TasksDir, vault):
    _archived(tasks_dir, vault)
    code, out = tasks_dir.run("doctor", "--json", "--offline")
    report = {c["name"]: c["status"] for c in json.loads(out)["checks"]}
    assert report["tasks.archive_note_agreement"] == "skipped"
    assert report["tasks.supersedes_chain"] == "skipped"


def test_requirement_overlap(tasks_dir: TasksDir):
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose("a", deltas={"demo": delta(modified=[MOD_ROOT])})
    assert _status(tasks_dir, "tasks.requirement_overlap") == "pass"
    shutil.copytree(tasks_dir.change_dir("a"), tasks_dir.change_dir("b"))
    assert _status(tasks_dir, "tasks.requirement_overlap") == "fail"


def test_blocked_decision_resolvable(tasks_dir: TasksDir):
    tasks_dir.propose("ok", "--skip-specs", "--blocked-by", "D6")
    assert _status(tasks_dir, "tasks.blocked_decision_resolvable") == "pass"
    tasks_dir.propose("bad", "--skip-specs", "--blocked-by", "D999")
    assert _status(tasks_dir, "tasks.blocked_decision_resolvable") == "fail"


def test_blocked_decision_file_missing_fails(tasks_dir: TasksDir):
    tasks_dir.propose("ok", "--skip-specs", "--blocked-by", "D6")
    tasks_dir.decisions.unlink()
    assert _status(tasks_dir, "tasks.blocked_decision_resolvable") == "fail"


def test_supersedes_chain(tasks_dir: TasksDir, vault):
    _archived(tasks_dir, vault, "z1")
    mod2 = requirement(
        "資料根目錄", "資料 SHALL 存放於 `~/.y/`。", scenarios=("讀取資料根",)
    )
    _archived(tasks_dir, vault, "a2", mod2, minutes=5)
    assert _status(tasks_dir, "tasks.supersedes_chain", vault.client) == "pass"
    first = tasks_dir.meta("z1")["notes"]["demo/資料根目錄"]
    second = tasks_dir.meta("a2")["notes"]["demo/資料根目錄"]
    # 鏈方向反了（仍只有一個鏈頭）：後封存的 note 沒有 supersedes 前一則
    vault.notes[second]["supersedes"] = None
    vault.notes[first]["supersedes"] = second
    assert _status(tasks_dir, "tasks.supersedes_chain", vault.client) == "fail"
    # 鏈本身正確，但同 topic 另有一則沒被取代的 note → 兩個鏈頭
    vault.notes[first]["supersedes"] = None
    vault.notes[second]["supersedes"] = first
    assert _status(tasks_dir, "tasks.supersedes_chain", vault.client) == "pass"
    vault.add(title="req:demo/資料根目錄", topics=["req:demo/資料根目錄"])
    assert _status(tasks_dir, "tasks.supersedes_chain", vault.client) == "fail"


def test_spec_delta_applied(tasks_dir: TasksDir, vault):
    _archived(tasks_dir, vault, "z1")
    mod2 = requirement(
        "資料根目錄", "資料 SHALL 存放於 `~/.y/`。", scenarios=("讀取資料根",)
    )
    _archived(tasks_dir, vault, "a2", mod2, minutes=5)
    # 同一 requirement 被兩次封存，只比最後一次：通過
    assert _status(tasks_dir, "tasks.spec_delta_applied") == "pass"
    tasks_dir.write_main("demo", SPEC_A)  # 主 spec 被改回 archive 前
    assert _status(tasks_dir, "tasks.spec_delta_applied") == "fail"


def test_dependency_exists(tasks_dir: TasksDir):
    tasks_dir.propose("base", "--skip-specs")
    tasks_dir.propose("ok", "--skip-specs", "--depends-on", "base")
    assert _status(tasks_dir, "tasks.dependency_exists") == "pass"
    tasks_dir.propose("bad", "--skip-specs", "--depends-on", "ghost")
    assert _status(tasks_dir, "tasks.dependency_exists") == "fail"
