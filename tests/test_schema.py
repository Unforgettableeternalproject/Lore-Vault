"""T-11：Vault／Note／Episode／Concept／Injection schema。

重點是「驗證會紅」：未知欄位、缺必填、scope 三態、UTC 時間、vault 不可空。
`test_validation_is_load_bearing` 以 monkeypatch 拿掉驗證，證明相應測試依賴它。
"""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest

from lore_vault.schema import (
    MISSING,
    Concept,
    Episode,
    Injection,
    Note,
    SchemaError,
    SourceTurn,
    ToolCount,
    Vault,
    _base,
)

# ── 範例資料（形狀照 spike 實際寫出的欄位，內容為虛構） ──────────────


def episode_dict(**overrides):
    data = {
        "prompt_id": "p-1",
        "turn_index": 0,
        "session_id": "s-1",
        "agent": "claude-code",
        "origin": "human",
        "machine": "desktop-a",
        "started_at": "2026-09-01T02:00:00.000Z",
        "ended_at": "2026-09-01T02:05:00.000Z",
        "cwd": ["C:/repos/Demo"],
        "repo": "Demo",
        "repo_root": "C:/repos/Demo",
        "git_branch": ["main"],
        "cc_version": "2.0.0",
        "user_text": "修 bug",
        "assistant_text": "好",
        "injected": [],
        "tool_sequence": [{"name": "Edit", "count": 2}, {"name": "Read", "count": 1}],
        "tool_calls_total": 3,
        "mcp_tools": [],
        "skills": [],
        "files_edited": ["src/a.py"],
        "files_read": ["src/b.py"],
        "symbols_edited": ["foo"],
        "thinking_blocks": 1,
    }
    data.update(overrides)
    return data


def concept_dict(**overrides):
    data = {
        "id": "c-001",
        "statement": "設定檔路徑集中在 config.py",
        "kind": "project-fact",
        "scope": "Demo",
        "cue": "改路徑常數時",
        "probe": "路徑常數放哪？",
        "why": "非慣例位置",
        "source_candidate": "t-abc",
        "from_signal": True,
        "source_turns": [["p-1", 0], ["p-2", 1]],
        "source_files": ["src/a.py"],
        "anchors": ["config.py", "DATA_DIR"],
        "surprisal": None,
        "probe_result": None,
    }
    data.update(overrides)
    return data


def note_dict(**overrides):
    data = {
        "id": "n-1",
        "vault": "github.com/owner/demo",
        "title": "標題",
        "body": "正文",
        "created": "2026-09-01T00:00:00Z",
        "updated": "2026-09-02T00:00:00+00:00",
    }
    data.update(overrides)
    return data


# ── round-trip ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cls, data",
    [
        (
            Vault,
            {"key": "github.com/o/r", "display": "R", "kind": "repo", "aliases": []},
        ),
        (
            Note,
            note_dict(summary="一句話", topics=["t"], links=["n-0"], supersedes="n-0"),
        ),
        (Episode, episode_dict()),
        (Concept, concept_dict()),
        (
            Injection,
            {"session_id": "s", "prompt_id": "__session__", "injected": ["c-1"]},
        ),
    ],
)
def test_round_trip_through_json(cls, data):
    obj = cls.from_dict(copy.deepcopy(data))
    dumped = obj.to_dict()
    # 可直接 JSON 化，再讀回得到相等物件
    again = cls.from_dict(json.loads(json.dumps(dumped, ensure_ascii=False)))
    assert again == obj
    # 輸入有的鍵、值都原樣保留（預設欄位可能額外出現）
    for key, value in data.items():
        assert dumped[key] == value, key


def test_records_are_frozen():
    ep = Episode.from_dict(episode_dict())
    with pytest.raises(dataclasses.FrozenInstanceError):
        ep.repo = "Renamed"  # type: ignore[misc]
    # 清單欄位被轉成 tuple，不能就地改
    assert isinstance(ep.files_edited, tuple)
    assert isinstance(ep.tool_sequence[0], ToolCount)


# ── 未知欄位／缺必填 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cls, data",
    [
        (Vault, {"key": "k", "display": "d"}),
        (Note, note_dict()),
        (Episode, episode_dict()),
        (Concept, concept_dict()),
        (Injection, {"session_id": "s", "prompt_id": "p", "injected": []}),
    ],
)
def test_unknown_field_is_rejected(cls, data):
    with pytest.raises(SchemaError, match="未知欄位"):
        cls.from_dict({**data, "surprise_field": 1})


@pytest.mark.parametrize(
    "cls, data",
    [
        (Vault, {"key": "k", "display": "d"}),
        (Note, note_dict()),
        (Episode, episode_dict()),
        (Concept, concept_dict()),
        (Injection, {"session_id": "s", "prompt_id": "p", "injected": []}),
    ],
)
def test_every_required_field_is_enforced(cls, data):
    for name in cls.REQUIRED:
        broken = {k: v for k, v in data.items() if k != name}
        with pytest.raises(SchemaError, match="缺少必填欄位"):
            cls.from_dict(broken)


def test_nullable_but_required_keys_must_be_present():
    """repo／repo_root 可以是 None，但鍵不能缺。

    缺鍵代表「沒凍結」，不是「沒有 repo」。
    """
    ep = Episode.from_dict(episode_dict(repo=None, repo_root=None))
    assert ep.repo is None
    for name in ("repo", "repo_root", "machine"):
        data = episode_dict()
        del data[name]
        with pytest.raises(SchemaError, match=name):
            Episode.from_dict(data)


def test_nested_unknown_field_is_rejected():
    with pytest.raises(SchemaError, match="ToolCount: 未知欄位"):
        Episode.from_dict(
            episode_dict(tool_sequence=[{"name": "Edit", "count": 1, "x": 1}])
        )


def test_non_mapping_input_is_rejected():
    with pytest.raises(SchemaError):
        Note.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]


# ── Vault ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["", "   ", " github.com/o/r"])
def test_vault_key_must_be_non_empty_and_trimmed(key):
    with pytest.raises(SchemaError, match="Vault.key"):
        Vault(key=key, display="d")


def test_vault_kind_and_aliases():
    with pytest.raises(SchemaError, match="Vault.kind"):
        Vault(key="k", display="d", kind="team")
    with pytest.raises(SchemaError, match="aliases"):
        Vault(key="k", display="d", aliases=("k",))
    with pytest.raises(SchemaError, match="重複"):
        Vault(key="k", display="d", aliases=("a", "a"))
    with pytest.raises(SchemaError, match="字串清單"):
        Vault(key="k", display="d", aliases="old-key")  # type: ignore[arg-type]
    assert Vault(key="k", display="d", kind="global").kind == "global"


def test_vault_key_and_aliases_normalized_to_lowercase():
    v = Vault(key="folder/MCSF", display="MCSF", aliases=("AI-Website-API",))
    assert v.key == "folder/mcsf"
    assert v.aliases == ("ai-website-api",)
    assert v.display == "MCSF"
    assert Vault.from_dict(v.to_dict()) == v
    # 大小寫不同但正規化後相同，仍算自我引用／重複
    with pytest.raises(SchemaError, match="aliases"):
        Vault(key="folder/mcsf", display="d", aliases=("folder/MCSF",))
    with pytest.raises(SchemaError, match="重複"):
        Vault(key="k", display="d", aliases=("Old", "old"))


# ── Note ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("vault", ["", "  ", None])
def test_note_vault_is_hard_required(vault):
    with pytest.raises(SchemaError, match="Note.vault"):
        Note.from_dict(note_dict(vault=vault))


def test_note_summary_nullable_but_not_empty_string():
    assert Note.from_dict(note_dict()).summary is None
    assert Note.from_dict(note_dict(summary=None)).summary is None
    with pytest.raises(SchemaError, match="summary"):
        Note.from_dict(note_dict(summary=""))


def test_note_supersedes_and_updated_order():
    with pytest.raises(SchemaError, match="supersedes"):
        Note.from_dict(note_dict(supersedes="n-1"))
    with pytest.raises(SchemaError, match="不可早於"):
        Note.from_dict(note_dict(updated="2026-08-01T00:00:00Z"))


def test_note_update_via_replace_bumps_version():
    note = Note.from_dict(note_dict())
    newer = dataclasses.replace(note, body="新正文", updated="2026-09-03T00:00:00Z")
    assert newer.updated != note.updated
    with pytest.raises(SchemaError):
        dataclasses.replace(note, updated="2026-01-01T00:00:00Z")


# ── 時間 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-01T00:00:00",  # 沒有時區
        "2026-09-01T08:00:00+08:00",  # 非 UTC
        "2026/09/01 00:00",  # 非 ISO-8601
        "",
        1756684800,
    ],
)
def test_timestamps_must_be_iso_utc(value):
    with pytest.raises(SchemaError, match="created"):
        Note.from_dict(note_dict(created=value))


@pytest.mark.parametrize(
    "value",
    ["2026-09-01T00:00:00Z", "2026-09-01T00:00:00.123Z", "2026-09-01T00:00:00+00:00"],
)
def test_utc_timestamps_are_kept_verbatim(value):
    note = Note.from_dict(note_dict(created=value, updated="2026-09-02T00:00:00Z"))
    assert note.created == value


def test_episode_timestamps_nullable_but_checked():
    ep = Episode.from_dict(episode_dict(started_at=None, ended_at=None))
    assert ep.started_at is None
    with pytest.raises(SchemaError, match="started_at"):
        Episode.from_dict(episode_dict(started_at="2026-09-01T10:00:00+08:00"))
    with pytest.raises(SchemaError, match="ended_at"):
        Episode.from_dict(episode_dict(ended_at="2026-08-01T00:00:00Z"))


# ── Episode ──────────────────────────────────────────────────────


def test_episode_injected_three_states():
    """[] = 沒被注入；缺鍵 = 早於 schema，不可與 [] 混為一談（spike 刻意區分）。"""
    data = episode_dict()
    del data["injected"]
    legacy = Episode.from_dict(data)
    assert legacy.injected is MISSING
    assert "injected" not in legacy.to_dict()

    clean = Episode.from_dict(episode_dict(injected=[]))
    assert clean.injected == ()
    assert clean.to_dict()["injected"] == []
    assert legacy != clean


@pytest.mark.parametrize(
    "field, value",
    [
        ("machine", ""),
        ("origin", "robot"),
        ("turn_index", -1),
        ("turn_index", True),
        ("tool_calls_total", "3"),
        ("files_edited", "src/a.py"),
        ("injected", None),
        ("tool_sequence", [{"name": "Edit", "count": 0}]),
    ],
)
def test_episode_field_validation(field, value):
    with pytest.raises(SchemaError):
        Episode.from_dict(episode_dict(**{field: value}))


# ── Concept：scope 三態 ───────────────────────────────────────────


def test_concept_scope_three_states_round_trip():
    repo = Concept.from_dict(concept_dict(scope="Demo"))
    declared_global = Concept.from_dict(concept_dict(scope=None))
    data = concept_dict()
    del data["scope"]
    unspecified = Concept.from_dict(data)

    assert repo.scope == "Demo" and not repo.is_global
    assert declared_global.scope is None and declared_global.is_global
    assert unspecified.scope is MISSING and not unspecified.is_global

    assert declared_global.to_dict()["scope"] is None
    assert "scope" not in unspecified.to_dict()
    # 三者兩兩不相等，且 round-trip 後維持原狀
    assert len({repr(c.scope) for c in (repo, declared_global, unspecified)}) == 3
    for c in (repo, declared_global, unspecified):
        assert Concept.from_dict(c.to_dict()) == c


@pytest.mark.parametrize("scope", ["", "  ", "global", "*", "None", "null"])
def test_concept_scope_rejects_ambiguous_strings(scope):
    with pytest.raises(SchemaError, match="scope"):
        Concept.from_dict(concept_dict(scope=scope))


def test_missing_sentinel_cannot_be_used_as_falsy():
    # 防止 `concept.scope or repo` 這類寫法把 MISSING 當成假值吞掉
    with pytest.raises(TypeError):
        bool(MISSING)
    assert copy.deepcopy(MISSING) is MISSING


@pytest.mark.parametrize(
    "field, value",
    [
        ("kind", "opinion"),
        ("surprisal", 1.5),
        ("surprisal", True),
        ("source_turns", [["p-1"]]),
        ("source_turns", [[1, 0]]),
        ("anchors", [""]),
        ("from_signal", "yes"),
        ("probe_result", "APPLIED"),
    ],
)
def test_concept_field_validation(field, value):
    with pytest.raises(SchemaError):
        Concept.from_dict(concept_dict(**{field: value}))


def test_concept_calibration_fields():
    c = Concept.from_dict(
        concept_dict(
            kind=None,
            surprisal=1.0,
            probe_result={"verdict": "MISSED", "evidence": "e", "note": None},
            usability={"verdict": "APPLIED", "evidence": "e", "note": None},
        )
    )
    assert c.kind is None
    assert c.source_turns == (SourceTurn("p-1", 0), SourceTurn("p-2", 1))
    assert c.to_dict()["probe_result"]["verdict"] == "MISSED"


# ── Injection ─────────────────────────────────────────────────────


def test_injection_attribution_key_required():
    ok = Injection.from_dict(
        {
            "session_id": "s",
            "prompt_id": None,
            "prompt_fingerprint": "abcd",
            "injected": ["c-1"],
        }
    )
    assert ok.prompt_id is None
    with pytest.raises(SchemaError, match="prompt_fingerprint"):
        Injection.from_dict({"session_id": "s", "prompt_id": None, "injected": []})
    with pytest.raises(SchemaError, match="session_id"):
        Injection.from_dict({"session_id": "", "prompt_id": "p", "injected": []})


# ── 防呆本身 ──────────────────────────────────────────────────────


def test_required_declaration_guard():
    @dataclasses.dataclass(frozen=True)
    class Bad(_base.Record):
        a: str
        b: str = ""

    with pytest.raises(TypeError, match="不存在"):
        _base.check_required_declared(Bad, {"a", "typo"})
    with pytest.raises(TypeError, match="漏列"):
        _base.check_required_declared(Bad, set())


def test_validation_is_load_bearing(monkeypatch):
    """拿掉驗證時，上面的關鍵案例必須失守（證明測試不是空轉）。"""
    # 1. 未知欄位：from_dict 不檢查時，建構子以 TypeError 爆，而非 SchemaError
    monkeypatch.setattr(Vault, "from_dict", classmethod(lambda cls, d: cls(**d)))
    with pytest.raises(TypeError):
        Vault.from_dict({"key": "k", "display": "d", "surprise": 1})
    monkeypatch.undo()

    # 2. 缺必填：REQUIRED 清空後，缺 repo_root 變成 TypeError 而非 SchemaError
    data = episode_dict()
    del data["injected"]
    monkeypatch.setattr(Episode, "REQUIRED", frozenset())
    with pytest.raises(TypeError):
        Episode.from_dict({k: v for k, v in data.items() if k != "repo_root"})
    monkeypatch.undo()

    # 3. vault 空字串：拿掉 __post_init__ 驗證後就會被接受
    monkeypatch.setattr(Note, "__post_init__", lambda self: None)
    assert Note.from_dict(note_dict(vault="")).vault == ""
    monkeypatch.undo()

    # 4. 時區：拿掉 utc_timestamp 驗證後，+08:00 會被接受
    monkeypatch.setattr(
        "lore_vault.schema.models.utc_timestamp", lambda owner, field, value: value
    )
    Note.from_dict(note_dict(created="2026-09-01T00:00:00+08:00"))
    monkeypatch.undo()

    # 5. scope 字串字面值：拿掉 __post_init__ 後 "global" 會被當成 repo 名存下
    monkeypatch.setattr(Concept, "__post_init__", lambda self: None)
    assert Concept.from_dict(concept_dict(scope="global")).scope == "global"
    monkeypatch.undo()

    # 還原後全部恢復報錯
    with pytest.raises(SchemaError):
        Note.from_dict(note_dict(vault=""))


def test_injection_ambiguous_ids_round_trip_and_subset():
    from lore_vault.schema import Injection, SchemaError

    base = {"session_id": "s", "prompt_id": "p", "injected": ["c-001", "c-002"]}
    plain = Injection.from_dict(base)
    assert "ambiguous_ids" not in plain.to_dict()
    marked = Injection.from_dict({**base, "ambiguous_ids": ["c-002"]})
    assert marked.to_dict()["ambiguous_ids"] == ["c-002"]
    import pytest

    with pytest.raises(SchemaError):
        Injection.from_dict({**base, "ambiguous_ids": ["c-999"]})
