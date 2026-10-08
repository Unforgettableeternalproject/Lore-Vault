"""任務層 doctor 的服務端對帳（TASK_LAYER_MCP §6、MCP-T7）：每項各有通過與
破壞後變紅的情境，服務端一律是 `VersionedVault`（版本化側載的假服務）。"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from lore_vault.doctor.framework import CheckSkipped, DoctorContext
from lore_vault.tasks import checks, specs
from lore_vault.tasks import remote_store as rs

from .conftest import NOW, SPEC_A, VAULT, TasksDir, delta, requirement
from .versioned_fake import VersionedVault

REMOTE_CHECKS = (
    "tasks.pending_apply_stale",
    "tasks.authorization_record_integrity",
    "tasks.version_sync_agreement",
    "tasks.specs_mirror_agreement",
    "tasks.decisions_mirror_agreement",
)
MOD_ROOT = requirement(
    "資料根目錄", "資料 SHALL 存放於 `~/.x/`。", scenarios=("讀取資料根",)
)
ADD_EXPORT = requirement("匯出", "系統 MUST 能匯出。", scenarios=("匯出",))
LOCAL_DECISIONS = {"D6": False, "D12": True, "D13": True}


@pytest.fixture
def vv():
    with VersionedVault() as fake:
        yield fake


def _report(tasks_dir: TasksDir, fake: VersionedVault) -> dict[str, dict]:
    code, out = tasks_dir.run("doctor", "--json", "--vault", VAULT, client=fake.client)
    data = json.loads(out)
    assert data["exit_code"] == code
    return {c["name"]: c for c in data["checks"]}


def _ctx(tasks_dir: TasksDir, fake: VersionedVault, **settings) -> DoctorContext:
    return DoctorContext(
        {
            "tasks_root": str(tasks_dir.root),
            "decisions_path": str(tasks_dir.decisions),
            "vault": VAULT,
            "now": NOW,
            **settings,
        },
        {"client": fake.client()},
    )


def _migrate(tasks_dir: TasksDir, fake: VersionedVault) -> None:
    code, out = tasks_dir.run("migrate", "--vault", VAULT, client=fake.client)
    assert code == 0, out


def _with_change(tasks_dir: TasksDir, fake: VersionedVault) -> None:
    """主 spec demo ＋ 一個 active change c1（MODIFIED 資料根目錄），已遷移。"""
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose("c1", deltas={"demo": delta(modified=[MOD_ROOT])})
    _migrate(tasks_dir, fake)


def _place(fake: VersionedVault, doc: dict, state: str | None = None) -> int:
    """直接擺一份 change 文件到服務端，並登記進索引。"""
    version = fake.put_json(rs.change_key(doc["name"]), doc)
    index = fake.get_json(rs.INDEX_KEY) or {"schema": 1, "changes": {}}
    index["changes"][doc["name"]] = {"state": state or doc["state"]}
    fake.put_json(rs.INDEX_KEY, index)
    return version


def _landed(name: str, state: str, stamp: str, deltas: dict, merged: dict) -> dict:
    doc = rs.new_doc(name, {"vault": VAULT}, "# P\n", "- [x] t\n")
    doc["state"] = state
    doc["deltas"] = deltas
    doc["meta"]["archived_at"] = stamp
    doc["apply"] = {"archived_at": stamp, "merged_specs": merged, "mirror_versions": {}}
    return doc


def _merged(text: str, delta_text: str, name: str) -> str:
    return specs.apply_delta(text, specs.parse_delta(delta_text), "demo", name)


def _decisions_blob(fake: VersionedVault, decisions: dict, **extra) -> None:
    fake.put_json(checks.DECISIONS_KEY, {"schema": 1, "decisions": decisions, **extra})


# ── 整體 ──


def test_registry_includes_remote_checks():
    names = {c.name for c in checks.default_registry().checks}
    assert set(REMOTE_CHECKS) <= names


def test_remote_checks_skipped_before_migration(tasks_dir: TasksDir, vv):
    """服務端沒有任務索引（純本機工作區）：不判成異常。"""
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose("c1", deltas={"demo": delta(modified=[MOD_ROOT])})
    report = _report(tasks_dir, vv)
    assert {n: report[n]["status"] for n in REMOTE_CHECKS} == dict.fromkeys(
        REMOTE_CHECKS, "skipped"
    )
    assert "tasks migrate" in report["tasks.version_sync_agreement"]["summary"]


def test_all_pass_after_migration(tasks_dir: TasksDir, vv):
    """封存過一個 change、另有 active change：遷移＋sync＋DECISIONS 鏡像後全線 ok。"""
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose("old", deltas={"demo": delta(modified=[MOD_ROOT])})
    code, out = tasks_dir.run("archive", "old", "--vault", VAULT, client=vv.client)
    assert code == 0, out
    tasks_dir.propose("c1", deltas={"demo": delta(added=[ADD_EXPORT])})
    _migrate(tasks_dir, vv)
    code, out = tasks_dir.run("sync", "--vault", VAULT, client=vv.client)
    assert code == 0, out
    _decisions_blob(vv, LOCAL_DECISIONS, source_digest="x")
    report = _report(tasks_dir, vv)
    assert {n: c["status"] for n, c in report.items()} == dict.fromkeys(report, "pass")


def test_service_unreachable_warns(tasks_dir: TasksDir):
    from .conftest import unreachable_client

    tasks_dir.write_main("demo", SPEC_A)
    code, out = tasks_dir.run(
        "doctor", "--json", "--vault", VAULT, client=unreachable_client
    )
    report = {c["name"]: c["status"] for c in json.loads(out)["checks"]}
    assert {n: report[n] for n in REMOTE_CHECKS} == dict.fromkeys(REMOTE_CHECKS, "warn")


# ── tasks.pending_apply_stale ──


def test_pending_apply_stale(tasks_dir: TasksDir, vv):
    _with_change(tasks_dir, vv)
    fresh = (NOW - dt.timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _place(vv, _landed("p1", rs.STATE_PENDING_APPLY, fresh, {}, {}))
    assert checks.pending_apply_stale(_ctx(tasks_dir, vv)).status == "pass"
    # 超過 72 小時未落地
    old = (NOW - dt.timedelta(hours=73)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _place(vv, _landed("p1", rs.STATE_PENDING_APPLY, old, {}, {}))
    result = checks.pending_apply_stale(_ctx(tasks_dir, vv))
    assert result.status == "warn"
    assert any("p1" in d and "73 小時" in d for d in result.details)
    # 門檻可調
    ctx = _ctx(tasks_dir, vv, pending_apply_stale_hours=100)
    assert checks.pending_apply_stale(ctx).status == "pass"


def test_pending_apply_without_archived_at_warns(tasks_dir: TasksDir, vv):
    _with_change(tasks_dir, vv)
    doc = _landed("p1", rs.STATE_PENDING_APPLY, "", {}, {})
    doc["apply"]["archived_at"] = None
    _place(vv, doc)
    assert checks.pending_apply_stale(_ctx(tasks_dir, vv)).status == "warn"


def test_pending_apply_stale_ignores_landed(tasks_dir: TasksDir, vv):
    _with_change(tasks_dir, vv)
    old = (NOW - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _place(vv, _landed("p1", rs.STATE_ARCHIVED, old, {}, {}))
    assert checks.pending_apply_stale(_ctx(tasks_dir, vv)).status == "pass"


# ── tasks.authorization_record_integrity ──


def _auth_doc(**meta) -> dict:
    doc = rs.new_doc(
        "auth1",
        {"vault": VAULT, "requires_authorization": True, **meta},
        "# P\n",
        "- [x] t\n",
    )
    return doc


def _record(**changes) -> dict:
    return {
        "schema": 1,
        "vault": VAULT,
        "change": "auth1",
        "change_version": 1,
        "authorized_by": "艾斯維爾",
        "authorized_at": "2026-10-08T11:00:00Z",
        "principal": {"kind": "ui_session", "name": "bernie"},
        **changes,
    }


COPIED = {
    "authorized_by": "艾斯維爾",
    "authorized_at": "2026-10-08T11:00:00Z",
    "change_version": 1,
}


def test_authorization_record_integrity(tasks_dir: TasksDir, vv):
    _with_change(tasks_dir, vv)
    check = checks.authorization_record_integrity
    # 還沒寫 note：閘門未通過，不要求紀錄
    _place(vv, _auth_doc())
    assert check(_ctx(tasks_dir, vv)).status == "pass"
    # 已寫 note 卻沒有 UI 核准紀錄（閘門被繞過）
    _place(vv, _auth_doc(notes={"summary": "n1"}, authorization=COPIED))
    result = check(_ctx(tasks_dir, vv))
    assert result.status == "fail"
    assert any("task-authorization:auth1" in d for d in result.details)
    # 有 UI session 的紀錄 → 通過
    vv.put_json(rs.authorization_key("auth1"), _record())
    assert check(_ctx(tasks_dir, vv)).status == "pass"
    # principal 不是 UI session（bearer／MCP 自己寫的）
    vv.put_json(rs.authorization_key("auth1"), _record(principal={"kind": "bearer"}))
    assert check(_ctx(tasks_dir, vv)).status == "fail"
    # 紀錄屬於別的 vault
    vv.put_json(rs.authorization_key("auth1"), _record(vault="folder/other"))
    assert check(_ctx(tasks_dir, vv)).status == "fail"


def test_authorization_record_mismatch_with_copy_warns(tasks_dir: TasksDir, vv):
    _with_change(tasks_dir, vv)
    _place(
        vv,
        _auth_doc(
            notes={"summary": "n1"}, authorization={**COPIED, "change_version": 3}
        ),
    )
    vv.put_json(rs.authorization_key("auth1"), _record())
    result = checks.authorization_record_integrity(_ctx(tasks_dir, vv))
    assert result.status == "warn"


def test_authorization_applies_to_landed_docs(tasks_dir: TasksDir, vv):
    """pending_apply（段一已寫 note）沒有紀錄同樣 fail。"""
    _with_change(tasks_dir, vv)
    doc = _auth_doc(note_id="n9")
    doc["state"] = rs.STATE_PENDING_APPLY
    _place(vv, doc)
    result = checks.authorization_record_integrity(_ctx(tasks_dir, vv))
    assert result.status == "fail"


# ── tasks.version_sync_agreement ──


def _version_status(tasks_dir: TasksDir, vv) -> str:
    return checks.version_sync_agreement(_ctx(tasks_dir, vv)).status


def test_version_sync_behind(tasks_dir: TasksDir, vv):
    """(a) 本機 remote_version 調回舊值、服務端不動 → warn。"""
    _with_change(tasks_dir, vv)
    assert _version_status(tasks_dir, vv) == "pass"
    tasks_dir.set_meta("c1", remote_version=0)
    result = checks.version_sync_agreement(_ctx(tasks_dir, vv))
    assert result.status == "warn"
    assert any("落後" in d for d in result.details)


def test_version_sync_remote_advanced(tasks_dir: TasksDir, vv):
    """別台機器 edit 過（服務端版本前進、本機沒 pull）→ warn。"""
    _with_change(tasks_dir, vv)
    vv.put_json(rs.change_key("c1"), vv.get_json(rs.change_key("c1")))
    assert _version_status(tasks_dir, vv) == "warn"


def test_version_sync_local_modified(tasks_dir: TasksDir, vv):
    """(b) 版本一致但本機 tasks.md 改過、沒重推 → warn。"""
    _with_change(tasks_dir, vv)
    path = tasks_dir.change_dir("c1") / "tasks.md"
    path.write_text(path.read_text(encoding="utf-8") + "- [ ] 新項目\n", "utf-8")
    result = checks.version_sync_agreement(_ctx(tasks_dir, vv))
    assert result.status == "warn"
    assert any("未成功推送" in d for d in result.details)


def test_version_sync_local_ahead_and_missing(tasks_dir: TasksDir, vv):
    _with_change(tasks_dir, vv)
    tasks_dir.set_meta("c1", remote_version=5)
    assert _version_status(tasks_dir, vv) == "warn"
    # 本機新 change 還沒推
    tasks_dir.set_meta("c1", remote_version=1)
    tasks_dir.propose("c2", "--skip-specs")
    result = checks.version_sync_agreement(_ctx(tasks_dir, vv))
    assert result.status == "warn"
    assert any("c2" in d and "migrate" in d for d in result.details)


def test_version_sync_ignores_pending_apply(tasks_dir: TasksDir, vv):
    """服務端已段一封存：本機還在 changes/ 是預期的（內容與版本都不同）。"""
    _with_change(tasks_dir, vv)
    doc = vv.get_json(rs.change_key("c1"))
    doc["state"] = rs.STATE_PENDING_APPLY
    doc["meta"]["notes"] = {"summary": "n1"}
    _place(vv, doc)
    assert _version_status(tasks_dir, vv) == "pass"


# ── tasks.specs_mirror_agreement ──


def _mirror_status(tasks_dir: TasksDir, vv) -> str:
    return checks.specs_mirror_agreement(_ctx(tasks_dir, vv)).status


def _put_mirror(vv, text: str | None, source="stdio") -> None:
    vv.put_json(
        rs.mirror_key("demo"),
        {
            "schema": 1,
            "capability": "demo",
            "exists": text is not None,
            "text": text,
            "source": source,
        },
    )


def test_specs_mirror_lag(tasks_dir: TasksDir, vv):
    """本機改一行主 spec、沒重推鏡像 → warn。"""
    _with_change(tasks_dir, vv)
    assert _mirror_status(tasks_dir, vv) == "pass"
    tasks_dir.write_main("demo", SPEC_A.replace("中間產物", "所有中間產物"))
    result = checks.specs_mirror_agreement(_ctx(tasks_dir, vv))
    assert result.status == "warn"
    assert any(d.startswith("demo：") for d in result.details)


def test_specs_mirror_missing_warns(tasks_dir: TasksDir, vv):
    _with_change(tasks_dir, vv)
    del vv.blobs[(VAULT, rs.mirror_key("demo"))]
    assert _mirror_status(tasks_dir, vv) == "warn"


def test_specs_mirror_pending_apply_ahead_is_ok(tasks_dir: TasksDir, vv):
    """pending_apply 期間鏡像本來就領先 git：不算落後。"""
    _with_change(tasks_dir, vv)
    mod = delta(modified=[MOD_ROOT])
    merged = _merged(SPEC_A, mod, "p1")
    _place(
        vv,
        _landed(
            "p1",
            rs.STATE_PENDING_APPLY,
            "2026-10-08T11:00:00Z",
            {"demo": mod},
            {"demo": merged},
        ),
    )
    _put_mirror(vv, merged, source="archive:p1")
    assert _mirror_status(tasks_dir, vv) == "pass"


def test_specs_mirror_regression_fails(tasks_dir: TasksDir, vv):
    """沒 git pull 的機器跑 stdio validate、把舊內容推回鏡像：鏡像缺少已封存
    change 併入的內容 → fail（即使鏡像與該機器的本機 specs/ 一致）。"""
    _with_change(tasks_dir, vv)
    mod = delta(modified=[MOD_ROOT])
    merged = _merged(SPEC_A, mod, "landed1")
    _place(
        vv,
        _landed(
            "landed1",
            rs.STATE_ARCHIVED,
            "2026-10-08T11:00:00Z",
            {"demo": mod},
            {"demo": merged},
        ),
    )
    # 鏡像＝本機舊內容（SPEC_A），缺 landed1 的 MODIFIED
    result = checks.specs_mirror_agreement(_ctx(tasks_dir, vv))
    assert result.status == "fail"
    assert any("landed1" in d and "倒退" in d for d in result.details)
    # 鏡像回到併入後內容：不再倒退，但本機尚未 git pull → 落後 warn
    _put_mirror(vv, merged)
    assert _mirror_status(tasks_dir, vv) == "warn"
    # 本機也 pull 到併入後內容 → 一致
    tasks_dir.write_main("demo", merged)
    assert _mirror_status(tasks_dir, vv) == "pass"


def test_specs_mirror_regression_uses_latest_change(tasks_dir: TasksDir, vv):
    """同一 requirement 後來又被另一個 change 改過：只比最後一個。"""
    _with_change(tasks_dir, vv)
    first = delta(modified=[MOD_ROOT])
    second_req = requirement(
        "資料根目錄", "資料 SHALL 存放於 `~/.y/`。", scenarios=("讀取資料根",)
    )
    second = delta(modified=[second_req])
    after_first = _merged(SPEC_A, first, "a1")
    after_second = _merged(after_first, second, "a2")
    _place(
        vv,
        _landed(
            "a1",
            rs.STATE_ARCHIVED,
            "2026-10-08T10:00:00Z",
            {"demo": first},
            {"demo": after_first},
        ),
    )
    _place(
        vv,
        _landed(
            "a2",
            rs.STATE_ARCHIVED,
            "2026-10-08T11:00:00Z",
            {"demo": second},
            {"demo": after_second},
        ),
    )
    _put_mirror(vv, after_second)
    tasks_dir.write_main("demo", after_second)
    assert _mirror_status(tasks_dir, vv) == "pass"


# ── tasks.decisions_mirror_agreement ──


def _decisions_status(tasks_dir: TasksDir, vv) -> str:
    try:
        return checks.decisions_mirror_agreement(_ctx(tasks_dir, vv)).status
    except CheckSkipped:
        return "skipped"


def test_decisions_mirror_agreement(tasks_dir: TasksDir, vv):
    assert _decisions_status(tasks_dir, vv) == "skipped"  # 尚未推送
    _decisions_blob(vv, LOCAL_DECISIONS)
    assert _decisions_status(tasks_dir, vv) == "pass"
    # 本機 D6 已裁決、鏡像還是舊的
    _decisions_blob(vv, {**LOCAL_DECISIONS, "D6": True})
    result = checks.decisions_mirror_agreement(_ctx(tasks_dir, vv))
    assert result.status == "warn"
    assert result.details[0].startswith("D6：")
    # 鏡像少一項
    _decisions_blob(vv, {"D6": False, "D12": True})
    assert _decisions_status(tasks_dir, vv) == "warn"


def test_decisions_mirror_tolerates_format(tasks_dir: TasksDir, vv):
    # 缺 schema／source_digest 仍可比對
    vv.put_json(checks.DECISIONS_KEY, {"decisions": LOCAL_DECISIONS})
    assert _decisions_status(tasks_dir, vv) == "pass"
    # 看不懂的格式 → warn（不是崩潰）
    vv.put_json(checks.DECISIONS_KEY, ["D6"])
    assert _decisions_status(tasks_dir, vv) == "warn"
    vv.put_json(checks.DECISIONS_KEY, {"decisions": {"D6": "no"}})
    assert _decisions_status(tasks_dir, vv) == "warn"


def test_decisions_mirror_skipped_without_local_file(tasks_dir: TasksDir, vv):
    _decisions_blob(vv, LOCAL_DECISIONS)
    tasks_dir.decisions.unlink()
    assert _decisions_status(tasks_dir, vv) == "skipped"
