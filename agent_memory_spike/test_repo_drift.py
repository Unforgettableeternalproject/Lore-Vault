"""非 git 目錄的 repo 歸屬：不跟著 shell 的 cwd 漂到子目錄。

2026-10-09 實際發生：專案目錄 ``E:/ProgramFiles/ComfyUI``（非 git）的三個 session，
shell ``cd`` 進子目錄後共 40 輪被標成 ``site-packages``／``character_specs``／``sheets``。
沒有 repo 根時 vault key 就是 ``folder/<repo>``，那些輪次因此進了雜項 vault，
同一個 session 被拆到兩個 vault，跨輪的 concept 被服務端判歸屬歧義、永遠推不上去。

涵蓋三件事：寫入端的規則（``transcript.rootless_repo``）、重建不改既有歸屬
（``hook_stop.pinned_repos``）、既有漂移的對帳與修復（doctor／``--repair-repo-drift``）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[1] / "src"))

import hook_stop  # noqa: E402
import transcript  # noqa: E402
from lore_vault.hooks import spool  # noqa: E402

SESSION = "sess-drift"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """模組級 cache 與全域日誌都換掉：測試不讀也不寫使用者的真實狀態。"""
    monkeypatch.setattr(transcript, "_repo_root_cache", {})
    monkeypatch.setattr(hook_stop, "load_injections", lambda: {})
    monkeypatch.setattr(hook_stop, "load_touches", lambda: {})
    monkeypatch.setattr(hook_stop, "current_machine", lambda: "desk-a")


def _old_rule(cwds, session_dir):
    """修正前的寫法：直接取第一個 cwd 的目錄名。"""
    return Path(cwds[0]).name if cwds else None


def _turn(prompt_id: str, *cwds: str) -> list[dict]:
    records = [{
        "type": "user", "promptId": prompt_id, "cwd": cwds[0], "gitBranch": "",
        "sessionId": SESSION, "version": "2.1.216", "origin": {"kind": "human"},
        "timestamp": "2026-10-09T13:00:00.000Z",
        "message": {"role": "user", "content": f"問題 {prompt_id}"},
    }]
    for cwd in cwds:
        records.append({
            "type": "assistant", "cwd": cwd, "gitBranch": "", "sessionId": SESSION,
            "version": "2.1.216", "timestamp": "2026-10-09T13:00:01.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {}},
                {"type": "text", "text": f"回答 {prompt_id}"},
            ]},
        })
    return records


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """非 git 的專案目錄 + 一份 cwd 會漂的 transcript。

    輪次：0 在專案目錄、1 整輪都在子目錄（實際 40 輪裡有 39 輪是這種）、
    2 先子目錄後回專案目錄（turn 53 的形狀）、3 回到專案目錄、4 是尾端未完成的一輪。
    """
    project = tmp_path / "Proj"
    sub = project / "python_embeded" / "Lib" / "site-packages"
    sub.mkdir(parents=True)
    records = (_turn("p0", str(project)) + _turn("p1", str(sub))
               + _turn("p2", str(sub), str(project)) + _turn("p3", str(project))
               + _turn("p4", str(project)))
    path = tmp_path / "projects" / "Proj" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    monkeypatch.setattr(hook_stop, "find_transcript",
                        lambda sid: path if sid == SESSION else None)
    ep_dir = tmp_path / "work" / "episodes"
    return project, sub, path, ep_dir


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _stored(ep_dir: Path) -> list[dict]:
    return _load(hook_stop.episode_path(ep_dir, SESSION))


# --- 寫入端的規則 ------------------------------------------------------------


def test_rootless_turns_stay_on_the_session_project_dir(layout):
    _, _, path, _ = layout
    episodes = transcript.episodes_from_transcript(path, {})
    assert [ep["repo"] for ep in episodes] == ["Proj"] * 5
    assert all(ep["repo_root"] is None for ep in episodes)


def test_subdirectory_name_leaks_without_the_session_rule(layout, monkeypatch):
    """拿掉修正會紅：上一條的斷言靠的就是 ``rootless_repo`` 的 session 基準。"""
    _, _, path, _ = layout
    monkeypatch.setattr(transcript, "rootless_repo", _old_rule)
    episodes = transcript.episodes_from_transcript(path, {})
    assert [ep["repo"] for ep in episodes] == [
        "Proj", "site-packages", "site-packages", "Proj", "Proj"]


def test_repo_and_spooled_vault_agree(layout):
    """``repo`` 與送出的 vault key 是同一條路：repo 對了，整個 session 就在同一個 vault。"""
    _, _, path, ep_dir = layout
    written, _ = hook_stop.sync(path, ep_dir, SESSION, injections={})
    assert written == 4
    pending = [json.loads(p.read_text(encoding="utf-8"))
               for p in (hook_stop.spool_dir_for(ep_dir) / spool.PENDING).glob("*.json")]
    assert len(pending) == 4
    assert {item["episode"]["vault"] for item in pending} == {"folder/proj"}


def test_git_repo_attribution_is_unchanged(tmp_path):
    """有 repo 根的情境照舊：取 git 根，session 的專案目錄不介入。"""
    project = tmp_path / "Proj"
    inner = project / "Inner"
    deep = inner / "src" / "pkg"
    deep.mkdir(parents=True)
    (inner / ".git").mkdir()

    ep = transcript.build_episode("p1", _turn("p1", str(deep)), session_dir=str(project))
    assert (ep["repo"], ep["repo_root"]) == ("Inner", str(inner))
    # 一輪內先碰到非 git 的 cwd、後面才進 repo：仍取解析得出的那個根
    ep = transcript.build_episode("p1", _turn("p1", str(project), str(deep)),
                                  session_dir=str(project))
    assert (ep["repo"], ep["repo_root"]) == ("Inner", str(inner))


def test_rootless_cwd_outside_the_project_keeps_its_own_name(tmp_path):
    """``cd`` 去了專案之外的非 git 位置：不是子目錄漂移，照舊取目錄名。

    同前綴的兄弟目錄（``Proj-other``）不算在 ``Proj`` 之內。
    """
    project = tmp_path / "Proj"
    sibling = tmp_path / "Proj-other" / "notes"
    project.mkdir()
    sibling.mkdir(parents=True)
    ep = transcript.build_episode("p1", _turn("p1", str(sibling)), session_dir=str(project))
    assert ep["repo"] == "notes"


def test_no_session_dir_falls_back_to_first_cwd(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert transcript.build_episode("p1", _turn("p1", str(plain)))["repo"] == "not-a-repo"


# --- 重建不改既有歸屬 ---------------------------------------------------------


def _seed_drifted(layout, monkeypatch) -> Path:
    """用修正前的規則寫入，造出「已經標錯」的存檔。"""
    _, _, path, ep_dir = layout
    with monkeypatch.context() as m:
        m.setattr(transcript, "rootless_repo", _old_rule)
        hook_stop.sync(path, ep_dir, SESSION, injections={}, spool=False)
    assert [r["repo"] for r in _stored(ep_dir)] == ["Proj", "site-packages", "site-packages", "Proj"]
    return ep_dir


def test_rebuild_keeps_the_stored_repo_name(layout, monkeypatch, capsys):
    """repo 歸屬是寫入當下的歷史事實：``--repair`` 與 doctor 的重建都沿用存檔的名字。"""
    _, _, path, ep_dir = layout
    _seed_drifted(layout, monkeypatch)
    before = _stored(ep_dir)

    assert hook_stop.repair(path, ep_dir, SESSION, {}) == (4, 0)
    assert _stored(ep_dir) == before

    assert hook_stop.doctor(ep_dir) == 0
    assert "site-packages !=" not in capsys.readouterr().err


def test_doctor_reports_mass_drift_without_the_repo_pin(layout, monkeypatch, capsys):
    """拿掉 pin 會紅：新規則重算既有輪次，doctor 逐輪報 repo 不一致（health 是硬閘門）。"""
    _, _, _, ep_dir = layout
    _seed_drifted(layout, monkeypatch)
    monkeypatch.setattr(hook_stop, "pinned_repos", lambda _records: {})
    assert hook_stop.doctor(ep_dir) == 1
    assert capsys.readouterr().err.count("repo site-packages != Proj") == 2


# --- 既有漂移的對帳與修復 -----------------------------------------------------


def test_doctor_warns_on_drift_until_repaired(layout, monkeypatch, capsys):
    """寫入端的規則退回去時 doctor 要看得見：存檔 vs transcript 的比對被 pin 遮住，靠這條對帳。"""
    _, _, _, ep_dir = layout
    _seed_drifted(layout, monkeypatch)

    assert hook_stop.doctor(ep_dir) == 0
    err = capsys.readouterr().err
    assert "2 輪的 repo 是專案目錄底下的子目錄名" in err and "--repair-repo-drift" in err

    hook_stop.repair_repo_drift(ep_dir, backup_dir=ep_dir.parent / "bk")
    capsys.readouterr()
    assert hook_stop.doctor(ep_dir) == 0
    assert "子目錄名" not in capsys.readouterr().err


def test_doctor_is_quiet_when_written_with_the_current_rule(layout, capsys):
    _, _, path, ep_dir = layout
    hook_stop.sync(path, ep_dir, SESSION, injections={}, spool=False)
    assert hook_stop.doctor(ep_dir) == 0
    assert "子目錄名" not in capsys.readouterr().err


def test_repair_dry_run_lists_without_writing(layout, monkeypatch, capsys):
    _, _, _, ep_dir = layout
    _seed_drifted(layout, monkeypatch)
    target = hook_stop.episode_path(ep_dir, SESSION)
    before = target.read_bytes()

    stats = hook_stop.repair_repo_drift(ep_dir, dry_run=True, backup_dir=ep_dir.parent / "bk")
    assert stats["drifted"] == 2 and stats["files_changed"] == 1
    assert [(t["turn_index"], t["from"], t["to"]) for t in stats["sessions"][SESSION]["turns"]] == [
        (1, "site-packages", "Proj"), (2, "site-packages", "Proj")]
    assert target.read_bytes() == before
    assert not (ep_dir.parent / "bk").exists()
    err = capsys.readouterr().err
    assert "site-packages → Proj  2 輪 turn_index=1-2" in err
    # 輸出只有結構性欄位，不帶語料內容
    assert "問題" not in err and "回答" not in err


def test_repair_changes_only_repo_on_drifted_lines(layout, monkeypatch):
    _, _, _, ep_dir = layout
    _seed_drifted(layout, monkeypatch)
    target = hook_stop.episode_path(ep_dir, SESSION)
    # 沒漂移的行刻意用非預設的 JSON 排版：修復不可重新序列化它（它可能已在服務端）
    lines = target.read_text(encoding="utf-8").splitlines()
    lines[0] = json.dumps(json.loads(lines[0]), separators=(",", ":"))
    target.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    before = _load(target)
    backup = ep_dir.parent / "bk"

    stats = hook_stop.repair_repo_drift(ep_dir, backup_dir=backup)
    assert stats["drifted"] == 2 and stats["backup_dir"] == str(backup)
    assert _load(backup / target.name) == before

    after_lines = target.read_text(encoding="utf-8").splitlines()
    assert after_lines[0] == lines[0] and after_lines[3] == lines[3]
    after = _load(target)
    assert [r["repo"] for r in after] == ["Proj"] * 4
    for old, new in zip(before, after):
        assert {k: v for k, v in old.items() if k != "repo"} == {
            k: v for k, v in new.items() if k != "repo"}
    assert all(spool.derive_vault(r["repo_root"], r["repo"]) == "folder/proj" for r in after)
    # 冪等
    assert hook_stop.repair_repo_drift(ep_dir, backup_dir=backup)["drifted"] == 0


def test_drift_is_not_judged_without_a_transcript(layout, monkeypatch):
    """transcript 已清就不判定：實測拿存檔第 0 輪代替會把改名前的巢狀 git repo 誤判成漂移。"""
    _, _, _, ep_dir = layout
    _seed_drifted(layout, monkeypatch)
    monkeypatch.setattr(hook_stop, "find_transcript", lambda _sid: None)
    assert hook_stop.repair_repo_drift(ep_dir, dry_run=True)["drifted"] == 0


def test_only_dirname_fallback_turns_count_as_drift(tmp_path):
    """有 repo 根的輪次、repo 不是第一個 cwd 目錄名的輪次，都是歷史事實，不碰。"""
    project = str(tmp_path / "Proj")
    sub = str(tmp_path / "Proj" / "Inner")
    assert transcript.drifted_repo(
        {"repo": "Inner", "repo_root": None, "cwd": [sub]}, project) == "Proj"
    assert transcript.drifted_repo(
        {"repo": "Inner", "repo_root": sub, "cwd": [sub]}, project) is None
    assert transcript.drifted_repo(
        {"repo": "Renamed", "repo_root": None, "cwd": [sub]}, project) is None
    assert transcript.drifted_repo(
        {"repo": "Proj", "repo_root": None, "cwd": [project]}, project) is None
    assert transcript.drifted_repo(
        {"repo": "Inner", "repo_root": None, "cwd": [sub]}, None) is None


# --- 行分隔字元 ---------------------------------------------------------------

# str.splitlines() 會切、但 json.dumps(ensure_ascii=False) 原樣寫出的字元
LINE_SEPARATORS = "  \x85"


def _seed_with_line_separators(layout, monkeypatch) -> tuple[Path, list[bytes]]:
    """第 0 輪（沒漂移）與第 1 輪（漂移）的內文都帶行分隔字元。"""
    ep_dir = _seed_drifted(layout, monkeypatch)
    target = hook_stop.episode_path(ep_dir, SESSION)
    records = _stored(ep_dir)
    for rec in records[:2]:
        rec["user_text"] = f"前{LINE_SEPARATORS}後"
    target.write_bytes("".join(
        json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8"))
    rows = target.read_bytes().split(b"\n")[:-1]
    assert len(rows) == 4 and LINE_SEPARATORS.encode("utf-8") in rows[0]
    return target, rows


def test_repair_keeps_rows_containing_line_separator_characters(layout, monkeypatch):
    target, rows = _seed_with_line_separators(layout, monkeypatch)
    stats = hook_stop.repair_repo_drift(target.parent, backup_dir=target.parent.parent / "bk")
    assert stats["drifted"] == 2

    after = target.read_bytes().split(b"\n")[:-1]
    assert len(after) == 4
    # 沒漂移的列逐位元組不變；漂移的列只有 repo 變了
    assert after[0] == rows[0] and after[3] == rows[3]
    fixed = json.loads(after[1])
    assert fixed["repo"] == "Proj" and fixed["user_text"] == f"前{LINE_SEPARATORS}後"
    assert {**fixed, "repo": "site-packages"} == json.loads(rows[1])


def test_splitlines_would_tear_those_rows_apart(layout, monkeypatch):
    """拿掉修正會紅：用 ``str.splitlines()`` 切，一列被永久切成好幾行、漂移也修不到。"""
    target, rows = _seed_with_line_separators(layout, monkeypatch)
    monkeypatch.setattr(hook_stop, "_jsonl_lines", lambda text: text.splitlines())
    hook_stop.repair_repo_drift(target.parent, backup_dir=target.parent.parent / "bk")
    after = target.read_bytes().split(b"\n")[:-1]
    assert len(after) > 4 and rows[0] not in after


# --- concept 的 scope 跟著漂移 --------------------------------------------------


def _write_pool(ep_dir: Path) -> Path:
    """c-1 的 scope 是漂移出來的子目錄名；其餘都不該被碰。"""
    pool = [
        {"id": "c-1", "statement": "陳述一", "scope": "site-packages",
         "source_turns": [["p1", 1], ["p2", 2]]},
        {"id": "c-2", "statement": "陳述二", "scope": "Proj", "source_turns": [["p0", 0]]},
        # 來源輪次找不到：沒有證據，不碰
        {"id": "c-3", "statement": "陳述三", "scope": "site-packages",
         "source_turns": [["gone", 9]]},
        {"id": "c-4", "statement": "陳述四", "scope": None, "source_turns": [["p1", 1]]},
        # LLM 自己選的 scope（不是任何來源輪次的目錄名）：不碰
        {"id": "c-5", "statement": "陳述五", "scope": "Elsewhere", "source_turns": [["p1", 1]]},
    ]
    path = hook_stop.concept_path_for(ep_dir)
    path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _scopes(path: Path) -> dict[str, str | None]:
    return {c["id"]: c["scope"] for c in json.loads(path.read_text(encoding="utf-8"))}


def test_concept_scope_dry_run_lists_without_writing(layout, monkeypatch, capsys):
    ep_dir = _seed_drifted(layout, monkeypatch)
    path = _write_pool(ep_dir)
    before = path.read_bytes()

    stats = hook_stop.repair_concept_scope(ep_dir, dry_run=True, backup_dir=ep_dir.parent / "bk")
    assert stats["drifted"] == [{"id": "c-1", "from": "site-packages", "to": "Proj"}]
    assert path.read_bytes() == before and not (ep_dir.parent / "bk").exists()
    err = capsys.readouterr().err
    assert "c-1  site-packages → Proj" in err and "陳述" not in err


def test_concept_scope_repair_changes_only_the_drifted_scope(layout, monkeypatch):
    ep_dir = _seed_drifted(layout, monkeypatch)
    path = _write_pool(ep_dir)
    before = json.loads(path.read_text(encoding="utf-8"))
    backup = ep_dir.parent / "bk"

    stats = hook_stop.repair_concept_scope(ep_dir, backup_dir=backup)
    assert [d["id"] for d in stats["drifted"]] == ["c-1"]
    assert json.loads((backup / path.name).read_text(encoding="utf-8")) == before
    after = json.loads(path.read_text(encoding="utf-8"))
    assert _scopes(path) == {"c-1": "Proj", "c-2": "Proj", "c-3": "site-packages",
                             "c-4": None, "c-5": "Elsewhere"}
    for old, new in zip(before, after):
        assert {k: v for k, v in old.items() if k != "scope"} == {
            k: v for k, v in new.items() if k != "scope"}
    # 排版與管線寫出的相同，不留暫存檔
    assert path.read_text(encoding="utf-8") == json.dumps(after, ensure_ascii=False, indent=2)
    assert not path.with_suffix(".json.tmp").exists()
    # 冪等
    assert hook_stop.repair_concept_scope(ep_dir, backup_dir=backup)["drifted"] == []


def test_concept_scope_is_found_after_episodes_are_repaired(layout, monkeypatch):
    """episode 先修好（甚至 transcript 已清）也判得出來：證據是來源輪次的 cwd，不是 repo 欄位。"""
    ep_dir = _seed_drifted(layout, monkeypatch)
    path = _write_pool(ep_dir)
    hook_stop.repair_repo_drift(ep_dir, backup_dir=ep_dir.parent / "bk")
    monkeypatch.setattr(hook_stop, "find_transcript", lambda _sid: None)
    assert [d["id"] for d in hook_stop.repair_concept_scope(ep_dir, dry_run=True)["drifted"]] == ["c-1"]
    assert _scopes(path)["c-1"] == "site-packages"


def test_doctor_warns_on_drifted_concept_scope_until_repaired(layout, monkeypatch, capsys):
    """episode 修好不代表 concept 也對：scope 是蒸餾當下抄下來的，要另外對帳。"""
    ep_dir = _seed_drifted(layout, monkeypatch)
    _write_pool(ep_dir)
    hook_stop.repair_repo_drift(ep_dir, backup_dir=ep_dir.parent / "bk")
    capsys.readouterr()

    assert hook_stop.doctor(ep_dir) == 0
    err = capsys.readouterr().err
    assert "1 條 concept 的 scope 是來源輪次的子目錄名" in err and "c-1" in err

    hook_stop.repair_concept_scope(ep_dir, backup_dir=ep_dir.parent / "bk2")
    capsys.readouterr()
    assert hook_stop.doctor(ep_dir) == 0
    assert "concept 的 scope" not in capsys.readouterr().err
