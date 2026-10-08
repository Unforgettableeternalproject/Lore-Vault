"""init／propose／list／validate。"""

from __future__ import annotations

import io
import json

from lore_vault.tasks import cli

from .conftest import SPEC_A, TasksDir, delta, requirement


def test_init_idempotent_and_keeps_content(tmp_path):
    root = tmp_path / "openspec"
    out = io.StringIO()
    assert cli.main(["--root", str(root), "init"], stdout=out, environ={}) == 0
    for rel in ("config.yaml", "specs/.gitkeep", "changes/archive/.gitkeep"):
        assert (root / rel).is_file()
    (root / "config.yaml").write_text("schema: custom\n", encoding="utf-8")
    assert cli.main(["--root", str(root), "init"], stdout=out, environ={}) == 0
    assert (root / "config.yaml").read_text(encoding="utf-8") == "schema: custom\n"


def test_init_default_location_is_cwd_openspec(tmp_path):
    out = io.StringIO()
    assert cli.main(["init"], stdout=out, environ={}, cwd=tmp_path) == 0
    assert (tmp_path / "openspec" / "config.yaml").is_file()


def test_propose_writes_all_fields(tasks_dir: TasksDir):
    tasks_dir.propose("add-x", "--source", "T-32", "--blocked-by", "D6")
    meta = tasks_dir.meta("add-x")
    assert meta["source"] == "T-32"
    assert meta["blocked_by"] == ["D6"]
    for key, value in {
        "schema": "spec-driven",
        "space": "dev",
        "depends_on": [],
        "requires_authorization": False,
        "note_id": None,
        "skip_specs": False,
        "base": {},
        "notes": {},
    }.items():
        assert meta[key] == value, key
    assert str(meta["created"]) == "2026-10-08"
    for name in ("proposal.md", "tasks.md"):
        assert (tasks_dir.change_dir("add-x") / name).is_file()


def test_propose_rejects_bad_input(tasks_dir: TasksDir):
    assert tasks_dir.run("propose", "Bad_Name")[0] == 1
    assert tasks_dir.run("propose", "ok", "--blocked-by", "X1")[0] == 1
    tasks_dir.propose("dup")
    assert tasks_dir.run("propose", "dup")[0] == 1


def _status(tasks_dir: TasksDir) -> dict[str, str]:
    code, out = tasks_dir.run("list", "--json")
    assert code == 0
    return {r["change"]: r["status"] for r in json.loads(out)}


def test_list_derives_every_status(tasks_dir: TasksDir):
    tasks_dir.propose("ready", "--skip-specs")
    tasks_dir.propose("blocked-d", "--skip-specs", "--blocked-by", "D6")
    tasks_dir.propose("resolved-d", "--skip-specs", "--blocked-by", "D12,D13")
    tasks_dir.propose("blocked-dep", "--skip-specs", "--depends-on", "ready")
    tasks_dir.propose("auth", "--skip-specs", "--requires-authorization")
    tasks_dir.propose("unknown-d", "--skip-specs", "--blocked-by", "D999")
    done = tasks_dir.change_dir("ready").parent / "archive" / "2026-10-01-old"
    done.mkdir(parents=True)
    (done / ".openspec.yaml").write_text("schema: spec-driven\n", encoding="utf-8")
    assert _status(tasks_dir) == {
        "ready": "可開工",
        "blocked-d": "被擋住",
        "resolved-d": "可開工",
        "blocked-dep": "被擋住",
        "auth": "待授權",
        "unknown-d": "無法判定",
        "old": "已完成",
    }
    code, out = tasks_dir.run("list")
    assert code == 0 and "D6 未裁決" in out


def test_list_decisions_file_missing_is_unknown(tasks_dir: TasksDir):
    tasks_dir.propose("blocked-d", "--skip-specs", "--blocked-by", "D12")
    tasks_dir.decisions.unlink()
    assert _status(tasks_dir) == {"blocked-d": "無法判定"}


def test_validate_skip_specs_ok_and_missing_fields_fail(tasks_dir: TasksDir):
    tasks_dir.propose("plain", "--skip-specs")
    assert tasks_dir.run("validate", "plain")[0] == 0
    path = tasks_dir.change_dir("plain") / ".openspec.yaml"
    path.write_text("schema: spec-driven\ncreated: 2026-10-08\n", encoding="utf-8")
    code, out = tasks_dir.run("validate", "plain")
    assert code == 1 and "缺少欄位 blocked_by" in out


def test_validate_requires_delta_unless_skip_specs(tasks_dir: TasksDir):
    tasks_dir.propose("nodelta")
    code, out = tasks_dir.run("validate", "nodelta")
    assert code == 1 and "沒有 spec delta" in out


def test_validate_bad_delta_exits_nonzero(tasks_dir: TasksDir):
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose("bad")
    path = tasks_dir.change_dir("bad") / "specs" / "demo" / "spec.md"
    path.parent.mkdir(parents=True)
    path.write_text(delta(modified=[requirement("不存在")]), encoding="utf-8")
    code, out = tasks_dir.run("validate", "bad", "--record-base")
    assert code == 1 and "不在主 spec" in out


def test_validate_overlap_is_error(tasks_dir: TasksDir):
    tasks_dir.write_main("demo", SPEC_A)
    mod = delta(modified=[requirement("資料根目錄", scenarios=("讀取資料根",))])
    tasks_dir.propose("a", deltas={"demo": mod})
    tasks_dir.propose("b")
    path = tasks_dir.change_dir("b") / "specs" / "demo" / "spec.md"
    path.parent.mkdir(parents=True)
    path.write_text(mod, encoding="utf-8")
    code, out = tasks_dir.run("validate", "--record-base")
    assert code == 1
    assert "demo/資料根目錄：同時被 active change" in out
    # 非 0 exit，不是只印警告：兩個 change 都 FAIL
    assert out.count("[FAIL]") == 2


def test_validate_missing_base_is_error_not_autofilled(tasks_dir: TasksDir):
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose("a")
    path = tasks_dir.change_dir("a") / "specs" / "demo" / "spec.md"
    path.parent.mkdir(parents=True)
    path.write_text(delta(added=[requirement("新功能")]), encoding="utf-8")
    code, out = tasks_dir.run("validate", "a")
    assert code == 1 and "base 未記錄" in out
    assert tasks_dir.meta("a")["base"] == {}
    assert tasks_dir.run("validate", "a", "--record-base")[0] == 0
    assert tasks_dir.meta("a")["base"] == {"demo/新功能": None}


def test_validate_stale_base_after_main_spec_changes(tasks_dir: TasksDir):
    """修正 1：B 在 A active 時建立；A 改了主 spec 後，B 的 validate 必須失敗。"""
    tasks_dir.write_main("demo", SPEC_A)
    mod = delta(modified=[requirement("資料根目錄", scenarios=("讀取資料根",))])
    tasks_dir.propose("b", deltas={"demo": mod})
    # 模擬 A 封存後主 spec 的該 requirement 已變
    tasks_dir.write_main("demo", SPEC_A.replace("`~/.demo/`。", "`~/.other/`。", 1))
    code, out = tasks_dir.run("validate", "b")
    assert code == 1 and "base 過時" in out
    # --record-base 不覆寫既有記錄，仍失敗
    assert tasks_dir.run("validate", "b", "--record-base")[0] == 1
    # 作者確認 delta 已 rebase 後才用 --rebase 重記
    assert tasks_dir.run("validate", "b", "--rebase")[0] == 0


def test_validate_added_base_detects_concurrent_add(tasks_dir: TasksDir):
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose("b", deltas={"demo": delta(added=[requirement("新功能")])})
    tasks_dir.write_main("demo", SPEC_A + "\n" + requirement("新功能", "別的 SHALL。"))
    code, out = tasks_dir.run("validate", "b")
    assert code == 1 and "base 過時" in out
