"""任務快照（UI-T2）：內容與語料邊界、`task_groups`（階段＋逐條任務）與其對帳、
確定性、大小上限與降級順序，以及 `sync` 與四個子指令的推送。

推送失敗只在 stderr 警告、exit code 不變；stdout（含 `list --json`）不受影響。
"""

from __future__ import annotations

import base64
import json
import re

import pytest

from lore_vault.binding import resolve_binding
from lore_vault.tasks import remote_store as rs
from lore_vault.tasks import snapshot
from lore_vault.tasks.workspace import Change, load_workspace, parse_task_groups

from .conftest import (
    SPEC_A,
    VAULT,
    TasksDir,
    delta,
    requirement,
    unreachable_client,
)

# 只出現在 delta 內文／scenario、design.md 的字串（不可進快照）；tasks.md 的階段標題與
# 逐項文字自 2026-10-11 起可進快照，但只限進行中的 change
DELTA_SECRET = "機密規格內文zq1"
SCENARIO_SECRET = "機密情境zq2"
TASK_SECRET = "機密子任務zq3"
DESIGN_SECRET = "機密設計zq4"
ARCHIVED_TASK = "已封存子任務zq5"


def _ws(tasks_dir: TasksDir):
    return load_workspace(tasks_dir.root, None, {})


def _by_name(tasks_dir: TasksDir) -> dict:
    return {c["name"]: c for c in snapshot.build_snapshot(_ws(tasks_dir))["changes"]}


def _write_why(tasks_dir: TasksDir, name: str, why: str) -> None:
    path = tasks_dir.change_dir(name) / "proposal.md"
    text = path.read_text(encoding="utf-8").replace("（為什麼要做）", why)
    path.write_text(text, encoding="utf-8")


def _remote(vault, key: str = VAULT) -> bytes:
    hit = vault.blobs[(key, snapshot.SNAPSHOT_KEY)]
    assert hit["mime"] == "application/json"
    return base64.b64decode(hit["content_base64"])


def test_snapshot_fields_and_statuses(tasks_dir: TasksDir):
    tasks_dir.propose("ready", "--skip-specs", "--source", "T-90")
    _write_why(tasks_dir, "ready", "讓 UI 看得到進行中的 change。")
    tasks_dir.propose("blocked", "--skip-specs", "--blocked-by", "D6,D12,D999")
    tasks_dir.propose("dep", "--skip-specs", "--depends-on", "ready,old")
    tasks_dir.propose("auth", "--skip-specs", "--requires-authorization")
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose(
        "spec",
        deltas={
            "demo": delta(added=[requirement("新功能")], removed=["封存"]),
        },
        complete=False,
    )
    old = tasks_dir.root / "changes" / "archive" / "2026-10-01-old"
    old.mkdir(parents=True)
    (old / ".openspec.yaml").write_text(
        "schema: spec-driven\nnote_id: note:abc\narchived_at: '2026-10-01'\n",
        encoding="utf-8",
    )
    snap = snapshot.build_snapshot(_ws(tasks_dir))
    assert snap["schema"] == 1 and set(snap) == {"schema", "changes"}
    changes = {c["name"]: c for c in snap["changes"]}
    for entry in changes.values():
        assert set(entry) - {snapshot.TASK_GROUPS} == snapshot.CHANGE_FIELDS
        # 只有進行中的 change 帶逐條明細；已封存的省略欄位（不是 null）
        assert (snapshot.TASK_GROUPS in entry) == (entry["archived_at"] is None)
    ready = changes["ready"]
    assert ready["status"] == "可開工" and ready["reasons"] == []
    assert ready["source"] == "T-90"
    assert ready["why"] == "讓 UI 看得到進行中的 change。"
    assert ready["tasks"] == {"done": 2, "total": 2}
    assert ready["note_id"] is None and ready["archived_at"] is None
    blocked = changes["blocked"]
    assert blocked["status"] == "被擋住"
    assert blocked["blocked_by"] == [
        {"id": "D6", "resolved": False},
        {"id": "D12", "resolved": True},
        {"id": "D999", "resolved": None},
    ]
    assert changes["dep"]["depends_on"] == [
        {"name": "ready", "archived": False},
        {"name": "old", "archived": True},
    ]
    assert changes["auth"]["status"] == "待授權"
    # 純本機（離線）模式：核准走 archive --authorized-by，與同步模式的 UI 文字分開
    assert changes["auth"]["reasons"] == [
        "需艾斯維爾授權（archive 須帶 --authorized-by）"
    ]
    assert "approved" not in changes["auth"]
    assert changes["auth"]["requires_authorization"] is True
    spec = changes["spec"]
    assert spec["specs"] == [
        {"capability": "demo", "requirement": "新功能", "op": "ADDED"},
        {"capability": "demo", "requirement": "封存", "op": "REMOVED"},
    ]
    assert spec["tasks"] == {"done": 0, "total": 2}
    done = changes["old"]
    assert done["status"] == "已完成"
    assert done["note_id"] == "note:abc" and done["archived_at"] == "2026-10-01"
    # 沒有 proposal.md（或 Why 段為空）為 None
    assert done["why"] is None


def test_unknown_status_when_decisions_missing(tasks_dir: TasksDir):
    tasks_dir.propose("c", "--skip-specs", "--blocked-by", "D12")
    tasks_dir.decisions.unlink()
    entry = _by_name(tasks_dir)["c"]
    assert entry["status"] == "無法判定"
    assert entry["blocked_by"] == [{"id": "D12", "resolved": None}]


def test_snapshot_excludes_corpus_text(tasks_dir: TasksDir):
    """語料邊界：spec delta 全文、scenario、design.md 不進快照；tasks.md 的階段標題與
    逐項文字只有進行中的 change 會進（已封存的不帶）。"""
    tasks_dir.write_main("demo", "# demo\n\n## Requirements\n")
    tasks_dir.propose(
        "c1",
        deltas={
            "demo": delta(
                added=[
                    requirement(
                        "公開標題", f"系統 SHALL {DELTA_SECRET}。", (SCENARIO_SECRET,)
                    )
                ]
            )
        },
        complete=False,
    )
    tasks = tasks_dir.change_dir("c1") / "tasks.md"
    tasks.write_text(f"# Tasks\n\n- [x] {TASK_SECRET}\n- [ ] 2\n", encoding="utf-8")
    (tasks_dir.change_dir("c1") / "design.md").write_text(
        f"# Design\n\n{DESIGN_SECRET}\n", encoding="utf-8"
    )
    old = tasks_dir.root / "changes" / "archive" / "2026-10-01-old"
    old.mkdir(parents=True)
    (old / ".openspec.yaml").write_text("schema: spec-driven\n", encoding="utf-8")
    (old / "tasks.md").write_text(f"- [x] {ARCHIVED_TASK}\n", encoding="utf-8")
    data = snapshot.snapshot_bytes(_ws(tasks_dir)).decode("utf-8")
    for secret in (DELTA_SECRET, SCENARIO_SECRET, DESIGN_SECRET, ARCHIVED_TASK):
        assert secret not in data
    entry, archived = json.loads(data)["changes"]
    assert entry["specs"] == [
        {"capability": "demo", "requirement": "公開標題", "op": "ADDED"}
    ]
    assert entry["tasks"] == {"done": 1, "total": 2}
    # `# Tasks`（H1）不算階段：兩項都歸 title=None
    assert entry["task_groups"] == [
        {
            "title": None,
            "items": [
                {"text": TASK_SECRET, "done": True},
                {"text": "2", "done": False},
            ],
        }
    ]
    assert archived["tasks"] == {"done": 1, "total": 1}
    assert snapshot.TASK_GROUPS not in archived


# ── task_groups：解析 ──

TASKS_MD = """# Tasks

- [ ] 開場項目

## 1. 後端
- [x] 解析
  - [X] 巢狀子項
* [ ] 星號項目

## 空階段

### 2.1 前端 
-   [ ]   多餘空白  
- 不是 checkbox
- [x]
#### 更深的標題不算階段
- [ ] 仍屬 2.1
"""


def test_parse_task_groups():
    assert parse_task_groups(TASKS_MD) == [
        {"title": None, "items": [{"text": "開場項目", "done": False}]},
        {
            "title": "1. 後端",
            "items": [
                {"text": "解析", "done": True},
                {"text": "巢狀子項", "done": True},
                {"text": "星號項目", "done": False},
            ],
        },
        {
            "title": "2.1 前端",
            "items": [
                {"text": "多餘空白", "done": False},
                {"text": "", "done": True},
                {"text": "仍屬 2.1", "done": False},
            ],
        },
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("# Tasks\n\n## 只有標題\n\n### 也沒有項目\n", []),
        # 沒有任何標題
        ("- [ ] a\n- [x] b\n", [(None, [("a", False), ("b", True)])]),
        # 只有 H1
        ("# Tasks\n- [x] a\n", [(None, [("a", True)])]),
        # CRLF 行尾
        ("## 階段\r\n- [x] a\r\n", [("階段", [("a", True)])]),
        # 同名階段各自成組，順序照檔案
        (
            "## A\n- [ ] 1\n## A\n- [ ] 2\n",
            [("A", [("1", False)]), ("A", [("2", False)])],
        ),
        # ATX 閉合的 `#` 去掉；沒有前置空白的 `#` 是標題文字的一部分
        (
            "## A ##\n- [ ] 1\n### B #  \n- [x] 2\n## C#\n- [ ] 3\n## D # E\n- [ ] 4\n",
            [
                ("A", [("1", False)]),
                ("B", [("2", True)]),
                ("C#", [("3", False)]),
                ("D # E", [("4", False)]),
            ],
        ),
        # 標題沒有文字（含只剩閉合 `#`）：另起無標題群組，項目不併入前一階段
        (
            "## A\n- [ ] 1\n## \n- [x] 2\n## ##\n- [ ] 3\n",
            [("A", [("1", False)]), (None, [("2", True)]), (None, [("3", False)])],
        ),
    ],
)
def test_parse_task_groups_edges(text, expected):
    want = [
        {"title": title, "items": [{"text": t, "done": d} for t, d in items]}
        for title, items in expected
    ]
    assert parse_task_groups(text) == want


def test_tasks_progress_counts_the_same_items(tmp_path):
    """`tasks_progress` 由 `task_groups` 攤平：被略過的空階段、H1 不影響計數。"""
    (tmp_path / "tasks.md").write_text(TASKS_MD, encoding="utf-8")
    change = Change("c", tmp_path, False, {})
    groups = change.task_groups()
    assert change.tasks_progress() == (3, 7)
    assert sum(len(g["items"]) for g in groups) == 7
    assert Change("none", tmp_path / "missing", False, {}).tasks_progress() == (0, 0)


def _legacy_tasks_progress(text: str) -> tuple[int, int]:
    """改為由 `task_groups` 推導之前的 `tasks_progress`（照抄作為 oracle）。"""
    done = total = 0
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s+\[([ xX])\]", line)
        if m:
            total += 1
            done += m.group(1) != " "
    return done, total


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("", (0, 0), id="empty"),
        pytest.param("- [ ] a\n- [x] b\n", (1, 2), id="no-heading"),
        pytest.param("# Tasks\n- [x] a\n- [ ] b\n", (1, 2), id="h1-only"),
        # code fence 內的 checkbox 照舊計入（舊行為，不在這次改動範圍）
        pytest.param(
            "## A\n```md\n- [x] 範例\n- [ ] 範例\n```\n- [ ] 真的\n",
            (1, 3),
            id="code-fence",
        ),
        pytest.param("## 階段\r\n- [x] a\r\n- [ ] b\r\n", (1, 2), id="crlf"),
        pytest.param(
            "## A\n- [x] 1\n#### 更深\n- [ ] 2\n##### 再深\n- [x] 3\n",
            (2, 3),
            id="h4-items",
        ),
        pytest.param("## A\n- [x]\n- [ ]\n- [ ] \n", (1, 3), id="empty-checkbox"),
        pytest.param(
            "- [x] 父\n  - [ ] 子\n    - [X] 孫\n\t- [ ] tab 縮排\n",
            (2, 4),
            id="nested",
        ),
        pytest.param("* [x] a\n* [ ] b\n  * [x] c\n", (2, 3), id="star"),
        pytest.param("- [X] a\n- [x] b\n- [ ] c\n", (2, 3), id="upper-x"),
        pytest.param(
            "## A ##\n- [x] 1\n## \n- [ ] 2\n## ##\n- [x] 3\n",
            (2, 3),
            id="closing-hashes-and-empty-title",
        ),
        # 不算 checkbox 的行：少空白、黏住符號、`+`、編號清單、其他字元；
        # `[x]` 後緊接文字仍算
        pytest.param(
            "- [] 少空白\n-[x] 黏住\n+ [x] 加號\n1. [x] 編號\n"
            "- [y] 其他字\n- [x]緊接文字\n",
            (1, 1),
            id="non-checkbox-lines",
        ),
        pytest.param(TASKS_MD, (3, 7), id="mixed"),
    ],
)
def test_tasks_progress_matches_legacy_counting(tmp_path, text, expected):
    """差分：新的 `tasks_progress`（由 `task_groups` 攤平）與改動前逐行 regex 計數一致。

    `expected` 另外釘住數字，避免 oracle 與實作一起漂移。
    """
    (tmp_path / "tasks.md").write_bytes(text.encode("utf-8"))
    change = Change("c", tmp_path, False, {})
    progress = change.tasks_progress()
    assert progress == _legacy_tasks_progress(change.read_file("tasks.md"))
    assert progress == expected
    assert sum(len(g["items"]) for g in change.task_groups()) == expected[1]


# ── task_groups：快照與對帳 ──


def _write_tasks(tasks_dir: TasksDir, name: str, text: str) -> None:
    (tasks_dir.change_dir(name) / "tasks.md").write_text(text, encoding="utf-8")


def test_snapshot_task_groups_reconcile_with_tasks(tasks_dir: TasksDir):
    tasks_dir.propose("c1", "--skip-specs", complete=False)
    _write_tasks(tasks_dir, "c1", TASKS_MD)
    tasks_dir.propose("empty", "--skip-specs", complete=False)
    _write_tasks(tasks_dir, "empty", "# Tasks\n")
    ws = _ws(tasks_dir)
    entries = _by_name(tasks_dir)
    assert entries["c1"]["task_groups"] == parse_task_groups(TASKS_MD)
    assert entries["c1"]["tasks"] == {"done": 3, "total": 7}
    # 進行中但沒有任何 checkbox：空陣列（不是省略——省略留給已封存與降級）
    assert entries["empty"]["task_groups"] == []
    assert snapshot.shape_errors(snapshot.snapshot_bytes(ws)) == []


def test_shape_errors_catch_task_groups_drift(tasks_dir: TasksDir, monkeypatch):
    """對帳會紅：計數與明細不再同源（拿掉 `tasks_progress` 由 `task_groups` 推導的
    保護）時，`shape_errors`（doctor `tasks.snapshot_shape`）報加總不一致。"""
    tasks_dir.propose("c1", "--skip-specs", complete=False)
    _write_tasks(tasks_dir, "c1", TASKS_MD)
    ws = _ws(tasks_dir)
    assert snapshot.shape_errors(snapshot.snapshot_bytes(ws)) == []
    # monkeypatch 在測試結束時還原，不污染其他測試
    monkeypatch.setattr(Change, "tasks_progress", lambda self: (3, 8))
    errors = snapshot.shape_errors(snapshot.snapshot_bytes(ws))
    assert errors == ["changes[0].task_groups 加總 3/7 與 tasks 3/8 不一致"]


@pytest.mark.parametrize(
    "groups",
    [
        None,
        {},
        [{"items": [{"text": "a", "done": False}]}, "x"],
        [{"title": 1, "items": []}],
        [{"title": None, "items": [{"text": "a"}]}],
        [{"title": None, "items": [{"text": "a", "done": 1}]}],
        [{"title": None, "items": [{"text": None, "done": True}]}],
        # 形狀對、加總不符（done 與 total 各一種）
        [{"title": None, "items": [{"text": "a", "done": True}]}],
        [{"title": None, "items": []}],
    ],
)
def test_shape_errors_reject_bad_task_groups(tasks_dir: TasksDir, groups):
    tasks_dir.propose("c1", "--skip-specs", complete=False)
    _write_tasks(tasks_dir, "c1", "- [ ] a\n")
    snap = snapshot.build_snapshot(_ws(tasks_dir))
    assert snapshot.shape_errors(snapshot.encode(snap)) == []
    snap["changes"][0]["task_groups"] = groups
    errors = snapshot.shape_errors(snapshot.encode(snap))
    assert len(errors) == 1 and "task_groups" in errors[0]
    # 省略欄位＝無明細，合法（舊版快照、降級）
    del snap["changes"][0]["task_groups"]
    assert snapshot.shape_errors(snapshot.encode(snap)) == []


def _remote_change(name: str, state: str, tasks_md: str) -> rs.RemoteChange:
    doc = rs.new_doc(name, {"vault": VAULT}, "# P\n", tasks_md)
    doc["state"] = state
    if state != rs.STATE_ACTIVE:
        doc["meta"]["archived_at"] = "2026-10-01T00:00:00Z"
    return rs.RemoteChange.from_doc(doc, 1, VAULT)


def test_remote_snapshot_task_groups_only_for_active():
    """服務端路徑：active 帶明細；已封存（待落地）與已落地都省略，計數照舊。"""
    changes = [
        _remote_change("live", rs.STATE_ACTIVE, TASKS_MD),
        _remote_change("pending", rs.STATE_PENDING_APPLY, "- [x] 待落地項目\n"),
        _remote_change("done", rs.STATE_ARCHIVED, "- [x] 已落地項目\n"),
    ]
    snap = snapshot.build_remote_snapshot(changes, {"done", "legacy"}, None)
    entries = {e["name"]: e for e in snap["changes"]}
    assert entries["live"]["task_groups"] == parse_task_groups(TASKS_MD)
    assert entries["live"]["tasks"] == {"done": 3, "total": 7}
    for name in ("pending", "done", "legacy"):
        assert snapshot.TASK_GROUPS not in entries[name]
    assert entries["pending"]["tasks"] == {"done": 1, "total": 1}
    data = snapshot.encode(snap)
    assert "待落地項目" not in data.decode("utf-8")
    assert "已落地項目" not in data.decode("utf-8")
    assert snapshot.shape_errors(data) == []


def test_snapshot_bytes_are_deterministic(tasks_dir: TasksDir):
    tasks_dir.propose("a", "--skip-specs", "--blocked-by", "D6")
    tasks_dir.propose("b", "--skip-specs")
    first = snapshot.snapshot_bytes(_ws(tasks_dir))
    assert snapshot.snapshot_bytes(_ws(tasks_dir)) == first
    tasks_dir.set_meta("a", blocked_by=["D12"])
    assert snapshot.snapshot_bytes(_ws(tasks_dir)) != first


def test_size_limit_drops_archived_why_then_refuses(tasks_dir: TasksDir):
    tasks_dir.propose("live", "--skip-specs")
    _write_why(tasks_dir, "live", "進行中" * 100)
    old = tasks_dir.root / "changes" / "archive" / "2026-10-01-old"
    old.mkdir(parents=True)
    (old / ".openspec.yaml").write_text("schema: spec-driven\n", encoding="utf-8")
    (old / "proposal.md").write_text("## Why\n\n" + "舊" * 900, encoding="utf-8")
    ws = _ws(tasks_dir)
    full = snapshot.snapshot_bytes(ws)
    data = snapshot.snapshot_bytes(ws, max_bytes=len(full) - 1)
    entries = {c["name"]: c for c in json.loads(data)["changes"]}
    assert entries["old"]["why"] is None and entries["live"]["why"]
    with pytest.raises(snapshot.SnapshotTooLarge):
        snapshot.snapshot_bytes(ws, max_bytes=200)


def test_size_limit_degrade_order(tasks_dir: TasksDir):
    """降級順序：已封存的 why → task_groups 由大到小逐一拿掉（夠小就停）→ 拒絕。
    被拿掉明細的 change 仍保留 tasks 計數與 why。"""
    for name, count in (("small", 2), ("big", 30), ("mid", 10)):
        tasks_dir.propose(name, "--skip-specs", complete=False)
        _write_why(tasks_dir, name, f"{name} 的理由")
        text = "## 階段\n" + "".join(f"- [x] {name} 項目 {i}\n" for i in range(count))
        _write_tasks(tasks_dir, name, text)
    old = tasks_dir.root / "changes" / "archive" / "2026-10-01-old"
    old.mkdir(parents=True)
    (old / ".openspec.yaml").write_text("schema: spec-driven\n", encoding="utf-8")
    (old / "proposal.md").write_text("## Why\n\n" + "舊" * 900, encoding="utf-8")
    ws = _ws(tasks_dir)

    def fit(drop: set[str], *, why: bool) -> bytes:
        """照指定的降級結果手工組出的位元組（期望值）。"""
        snap = snapshot.build_snapshot(ws)
        for entry in snap["changes"]:
            if entry["name"] in drop:
                del entry[snapshot.TASK_GROUPS]
            if entry["name"] == "old" and not why:
                entry["why"] = None
        return snapshot.encode(snap)

    def run(max_bytes: int) -> bytes:
        return snapshot.snapshot_bytes(ws, max_bytes=max_bytes)

    full = fit(set(), why=True)
    stages = [
        fit(set(), why=False),
        fit({"big"}, why=False),
        fit({"big", "mid"}, why=False),
        fit({"big", "mid", "small"}, why=False),
    ]
    sizes = [len(full), *(len(s) for s in stages)]
    assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == len(sizes)
    assert run(len(full)) == full
    # 每一階：上限剛好等於該階大小 → 停在該階；比前一階小 1 也是同一階
    previous = full
    for stage in stages:
        assert run(len(previous) - 1) == stage
        assert run(len(stage)) == stage
        previous = stage
    last = {c["name"]: c for c in json.loads(stages[-1])["changes"]}
    assert last["big"]["tasks"] == {"done": 30, "total": 30}
    assert last["big"]["why"] == "big 的理由"
    assert snapshot.shape_errors(stages[-1]) == []
    with pytest.raises(snapshot.SnapshotTooLarge):
        run(len(stages[-1]) - 1)


# ── CLI ──


def test_sync_pushes_snapshot(tasks_dir: TasksDir, vault):
    tasks_dir.propose("c1", "--skip-specs")
    code, out = tasks_dir.run("sync", "--vault", VAULT, client=vault.client)
    assert code == 0 and f"已同步 1 個 change 到 {VAULT}" in out
    assert _remote(vault) == snapshot.snapshot_bytes(_ws(tasks_dir))
    put = [r for r in vault.requests if r["path"] == "/v1/blob_put"][-1]
    assert put["body"]["space"] == "dev" and put["body"]["key"] == "tasks-snapshot"


def test_sync_failure_is_exit_1(tasks_dir: TasksDir):
    code, out = tasks_dir.run("sync", client=unreachable_client)
    assert code == 1 and "sync 失敗" in out


def test_sync_without_client_config_is_exit_1(tasks_dir: TasksDir):
    code, out = tasks_dir.run("sync")
    assert code == 1 and "推送未設定" in out


@pytest.mark.parametrize("command", ["list", "validate"])
def test_read_commands_push_and_never_fail_on_push(tasks_dir: TasksDir, command):
    tasks_dir.propose("c1", "--skip-specs")
    expected = tasks_dir.run(command)
    code, out = tasks_dir.run(command, client=unreachable_client)
    assert (code, out) == expected
    assert "任務快照未同步" in tasks_dir.err


def test_list_json_stays_parseable_when_push_fails(tasks_dir: TasksDir):
    tasks_dir.propose("c1", "--skip-specs")
    code, out = tasks_dir.run("list", "--json", client=unreachable_client)
    assert code == 0 and json.loads(out)[0]["change"] == "c1"
    assert "任務快照未同步" in tasks_dir.err and "任務快照" not in out


def test_each_command_pushes(tasks_dir: TasksDir, vault):
    # propose：建立後推送（vault 由專案目錄 binding 推算）
    code, _ = tasks_dir.run("propose", "c1", "--skip-specs", client=vault.client)
    assert code == 0 and vault.blob_puts == 1
    key = next(iter(vault.blobs))
    assert key[1] == "tasks-snapshot" and key[0] != VAULT
    tasks_dir.check_all_tasks("c1")
    for command in (("list",), ("validate", "c1")):
        before = vault.blob_puts
        assert tasks_dir.run(*command, client=vault.client)[0] == 0
        assert vault.blob_puts == before + 1
    before = vault.blob_puts
    code, out = tasks_dir.run("archive", "c1", "--vault", VAULT, client=vault.client)
    assert code == 0, out
    # 封存到 VAULT：binding 的 vault 重推（移除 c1）、VAULT 推一份
    assert vault.blob_puts == before + 2
    entry = json.loads(_remote(vault))["changes"][0]
    assert entry["status"] == "已完成" and entry["note_id"]
    assert json.loads(_remote(vault, key[0]))["changes"] == []


def test_failed_propose_and_refused_archive_do_not_push(tasks_dir: TasksDir, vault):
    assert tasks_dir.run("propose", "Bad_Name", client=vault.client)[0] == 1
    tasks_dir.propose("c1", "--skip-specs", "--blocked-by", "D6")
    code, _ = tasks_dir.run("archive", "c1", "--vault", VAULT, client=vault.client)
    assert code == 1
    assert vault.requests == []


def test_propose_push_failure_keeps_exit_0(tasks_dir: TasksDir):
    code, out = tasks_dir.run(
        "propose", "c1", "--skip-specs", client=unreachable_client
    )
    assert code == 0 and "已建立" in out
    assert "任務快照未同步" in tasks_dir.err


def test_archive_pushes_to_the_vault_it_archived_into(tasks_dir: TasksDir, vault):
    """metadata 記的 vault 與專案 binding 不同：推送打到 archive 實際使用的 vault。"""
    recorded = "folder/recorded-elsewhere"
    tasks_dir.propose("c1", "--skip-specs")
    tasks_dir.set_meta("c1", vault=recorded)
    code, out = tasks_dir.run("archive", "c1", client=vault.client)
    assert code == 0, out
    written = {r["body"]["vault"] for r in vault.requests if r["path"] == "/v1/write"}
    assert written == {recorded}
    names = {
        v: [c["name"] for c in json.loads(_remote(vault, v))["changes"]]
        for v, _ in vault.blobs
    }
    # binding 的 vault 也重推：c1 已改記在 recorded，從預設 vault 的快照移除
    assert names == {recorded: ["c1"], resolve_binding(tasks_dir.project).key: []}


def test_sync_splits_snapshot_by_recorded_vault(tasks_dir: TasksDir, vault):
    """同一 workspace 的 change 封存到不同 vault：每個 vault 只收自己的 change。"""
    tasks_dir.propose("stay", "--skip-specs")
    tasks_dir.propose("moved", "--skip-specs")
    tasks_dir.propose("alias", "--skip-specs")
    tasks_dir.set_meta("alias", vault=VAULT)
    code, out = tasks_dir.run(
        "archive", "moved", "--vault", "vault-b", client=vault.client
    )
    assert code == 0, out
    vault.blobs.clear()
    code, out = tasks_dir.run("sync", "--vault", VAULT, client=vault.client)
    assert code == 0, out
    names = {
        v: sorted(c["name"] for c in json.loads(_remote(vault, v))["changes"])
        for v, _ in vault.blobs
    }
    assert names == {VAULT: ["alias", "stay"], "vault-b": ["moved"]}
    assert "已同步 2 個 change 到 folder/demo" in out
    assert "已同步 1 個 change 到 vault-b" in out
