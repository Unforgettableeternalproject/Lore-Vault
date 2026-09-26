"""T-21：write 查重、update 樂觀鎖與重算旗標、get 預算、list 分頁。"""

from __future__ import annotations

import pytest

from lore_vault.notes import (
    InvalidCursor,
    NoChanges,
    VersionConflict,
    get,
    list_,
    update,
    write,
)
from lore_vault.notes import service as notes_service
from lore_vault.storage import notes as storage_notes
from lore_vault.storage import vectors
from lore_vault.storage.errors import NotFound, VaultRequired

from .conftest import DIM, TS, RaisingEmbedder

A = "folder/a"
B = "folder/b"
NOW = "2026-09-02T00:00:00.000Z"


@pytest.fixture
def vaults(conn, add_vault):
    add_vault(A)
    add_vault(B)
    return conn


# ── write ───────────────────────────────────────────────────────────


def test_write_stores_note_without_waiting_for_summary_or_embedding(vaults, embedder):
    result = write(
        vaults,
        A,
        "新筆記",
        "正文內容",
        space="dev",
        principal="xavier",
        topics=["t"],
        now=NOW,
    )
    note = storage_notes.get_note(vaults, A, result.note.id, space="dev")
    assert note.summary is None
    assert note.created == note.updated == NOW
    assert (
        vectors.get_embedding(vaults, A, note.id, space="dev") is None
    )  # 背景補，不在 write
    payload = result.to_dict()
    assert payload == {
        "id": note.id,
        "vault": A,
        "updated": NOW,
        # 作者契約（A22）：未填 author 就是 None，不代填；principal 由呼叫端傳入
        "author": None,
        "principal": "xavier",
        "links": [],
        "unresolved_links": [],
        "duplicates": [],
        "dedup_degraded": True,  # 沒給 embedder
        "dedup_reason": "embedder_unavailable",
        "dry_run": False,
    }


def test_write_flags_known_similar_note(vaults, add_note, embedder):
    add_note(
        A,
        "old",
        "FTS5 中文檢索",
        "trigram 對兩字詞「記憶」靜默回 0 筆，改用 CJK bigram 索引。",
    )
    add_note(A, "other", "樂觀鎖", "update 帶 expected_updated，版本不符回衝突。")
    add_note(B, "b-same", "FTS5 中文檢索", "trigram 對兩字詞「記憶」靜默回 0 筆。")
    result = write(
        vaults,
        A,
        "FTS5 中文檢索",
        "trigram 對兩字詞「記憶」會靜默回 0 筆，所以改用 CJK bigram 索引。",
        space="dev",
        principal="xavier",
        embedder=embedder,
        dim=DIM,
    )
    assert not result.dedup_degraded
    [dup] = result.duplicates  # 只在同一 vault 內查重；無關 note 不列
    assert dup.id == "old"
    assert set(dup.reasons) == {"title", "lexical", "vector"}
    assert dup.to_dict()["vector"] >= notes_service.DEDUP_VECTOR_THRESHOLD


def test_write_vector_only_duplicate_is_found(vaults, add_note, embedder):
    """中英改寫：詞彙幾乎不重疊，只有向量那一路抓得到。"""
    add_note(A, "zh", "中文 全文 檢索", "記憶 搜尋")
    result = write(
        vaults,
        A,
        "chinese full text search",
        "memory search",
        space="dev",
        principal="xavier",
        embedder=embedder,
        dim=DIM,
    )
    [dup] = result.duplicates
    assert dup.id == "zh" and dup.reasons == ("vector",)


def test_write_dedup_degrades_to_lexical_when_embedder_fails(vaults, add_note):
    add_note(A, "old", "FTS5 中文檢索", "改用 CJK bigram 索引")
    result = write(
        vaults,
        A,
        "FTS5 中文檢索",
        "改用 CJK bigram 索引",
        space="dev",
        principal="xavier",
        embedder=RaisingEmbedder(TimeoutError("timed out")),
        dim=DIM,
    )
    assert result.dedup_degraded is True
    assert result.dedup_reason == "embedder_timeout"
    assert [d.id for d in result.duplicates] == ["old"]
    assert result.duplicates[0].vector is None


def test_write_dissimilar_note_has_no_duplicates(vaults, add_note, embedder):
    add_note(A, "old", "FTS5 中文檢索", "改用 CJK bigram 索引")
    result = write(
        vaults,
        A,
        "Docker volume",
        "named volume 預設",
        space="dev",
        principal="xavier",
        embedder=embedder,
        dim=DIM,
    )
    assert result.duplicates == []


def test_write_supersedes_must_exist_and_is_not_a_duplicate(vaults, add_note, embedder):
    add_note(A, "old", "FTS5 中文檢索", "改用 CJK bigram 索引")
    with pytest.raises(NotFound):
        write(
            vaults, A, "t", "b", space="dev", principal="xavier", supersedes="missing"
        )
    with pytest.raises(NotFound):  # 其他 vault 的 note 不可被取代
        write(vaults, B, "t", "b", space="dev", principal="xavier", supersedes="old")
    result = write(
        vaults,
        A,
        "FTS5 中文檢索",
        "改用 CJK bigram 索引",
        space="dev",
        principal="xavier",
        supersedes="old",
        embedder=embedder,
        dim=DIM,
    )
    assert result.duplicates == []
    assert result.note.supersedes == "old"


def test_write_rejects_wildcard_vault(vaults):
    with pytest.raises(VaultRequired):
        write(vaults, "*", "t", "b", space="dev", principal="xavier")


def test_embedding_text_has_single_source():
    """查重與背景補算用同一個函式算 embedding 輸入，cosine 才可比。"""
    from lore_vault.enrich import worker
    from lore_vault.notes import text

    assert notes_service.embedding_text is text.embedding_text
    assert worker.embedding_text is text.embedding_text
    assert text.embedding_text("標題", "正文") == "標題\n\n正文"
    assert text.embedding_text("標題", "") == "標題"


def test_dedup_embeds_title_and_body(vaults, add_note, embedder):
    add_note(A, "old", "舊標題", "舊內容")
    embedder.calls.clear()
    write(
        vaults,
        A,
        "新標題",
        "新內容",
        space="dev",
        principal="xavier",
        embedder=embedder,
        dim=DIM,
    )
    assert embedder.calls == ["新標題\n\n新內容"]


# ── update ──────────────────────────────────────────────────────────


def _conflict_detected(conn, add_note) -> bool:
    """以過期版本更新：有衝突錯誤且沒寫入 → True。"""
    add_note(A, "n", "標題", "原文", summary="摘要", embed=False)
    first = update(
        conn, A, "n", TS, space="dev", principal="xavier", body="第一次修改", now=NOW
    )
    try:
        update(
            conn, A, "n", TS, space="dev", principal="xavier", body="拿舊版本覆蓋"
        )  # TS 已過期
    except VersionConflict as exc:
        assert exc.current.updated == first.note.updated
        assert exc.expected == TS
        assert storage_notes.get_note(conn, A, "n", space="dev").body == "第一次修改"
        return True
    return False


def test_update_with_stale_version_is_a_conflict(vaults, add_note):
    assert _conflict_detected(vaults, add_note) is True


def test_conflict_test_is_load_bearing(vaults, add_note, monkeypatch):
    """把版本檢查拿掉（永遠用資料庫目前版本），衝突測試必須紅。"""
    original = storage_notes.update_note_if

    def no_version_check(conn, vault, note_id, expected, changes, **kwargs):
        current = storage_notes.get_note(conn, vault, note_id, space="dev").updated
        return original(conn, vault, note_id, current, changes, **kwargs)

    monkeypatch.setattr(notes_service, "update_note_if", no_version_check)
    assert _conflict_detected(vaults, add_note) is False


def test_update_body_clears_summary_and_embedding(vaults, add_note):
    note = add_note(A, "n", "標題", "原文", summary="舊摘要")
    result = update(
        vaults,
        A,
        "n",
        note.updated,
        space="dev",
        principal="xavier",
        body="新正文",
        now=NOW,
    )
    assert (result.summary_stale, result.embedding_stale) == (True, True)
    stored = storage_notes.get_note(vaults, A, "n", space="dev")
    assert stored.summary is None and stored.body == "新正文"
    assert vectors.get_embedding(vaults, A, "n", space="dev") is None
    assert result.to_dict() == {
        "id": "n",
        "vault": A,
        "updated": stored.updated,
        "author": None,
        "updated_by": None,
        "updated_by_principal": "xavier",
        "links": [],
        "unresolved_links": [],
        "summary_stale": True,
        "embedding_stale": True,
    }


def test_update_title_only_keeps_summary(vaults, add_note):
    note = add_note(A, "n", "標題", "原文", summary="舊摘要")
    result = update(
        vaults, A, "n", note.updated, space="dev", principal="xavier", title="新標題"
    )
    assert (result.summary_stale, result.embedding_stale) == (False, True)
    assert storage_notes.get_note(vaults, A, "n", space="dev").summary == "舊摘要"


def test_update_topics_or_identical_body_keeps_everything(vaults, add_note):
    note = add_note(A, "n", "標題", "原文", summary="舊摘要")
    result = update(
        vaults,
        A,
        "n",
        note.updated,
        space="dev",
        principal="xavier",
        topics=["x"],
        body="原文",
    )
    assert (result.summary_stale, result.embedding_stale) == (False, False)
    stored = storage_notes.get_note(vaults, A, "n", space="dev")
    assert stored.summary == "舊摘要" and stored.topics == ("x",)
    assert vectors.get_embedding(vaults, A, "n", space="dev") is not None
    assert stored.updated > note.updated


def test_update_argument_errors(vaults, add_note):
    note = add_note(A, "n", "標題", "原文", embed=False)
    with pytest.raises(NoChanges):
        update(vaults, A, "n", note.updated, space="dev", principal="xavier")
    with pytest.raises(ValueError):
        update(vaults, A, "n", "", space="dev", principal="xavier", body="x")
    with pytest.raises(NotFound):
        update(
            vaults, B, "n", note.updated, space="dev", principal="xavier", body="x"
        )  # 其他 vault
    with pytest.raises(NotFound):
        update(
            vaults,
            A,
            "n",
            note.updated,
            space="dev",
            principal="xavier",
            supersedes="missing",
        )


# ── get ─────────────────────────────────────────────────────────────


def test_get_batch_keeps_order_and_reports_missing(vaults, add_note):
    add_note(A, "a1", "一", "甲" * 10, summary="摘要一", embed=False)
    add_note(A, "a2", "二", "乙" * 10, embed=False)
    add_note(B, "b1", "三", "丙", embed=False)
    result = get(vaults, A, ["a2", "b1", "a1", "nope", "a2"], space="dev")
    assert [i["id"] for i in result.items] == ["a2", "a1"]
    assert result.missing == ["b1", "nope"]
    assert not result.truncated
    assert result.items[0]["summary_source"] == "lead"
    assert result.items[1]["summary_source"] == "summary"


def test_get_truncates_over_budget_and_marks_it(vaults, add_note):
    add_note(A, "a1", "一", "甲" * 10, embed=False)
    add_note(A, "a2", "二", "乙" * 10, embed=False)
    add_note(A, "a3", "三", "丙" * 10, embed=False)
    result = get(vaults, A, ["a1", "a2", "a3"], space="dev", budget=15)
    assert result.truncated is True and result.used_chars == 15
    first, second, third = result.items
    assert (first["body"], first["truncated"]) == ("甲" * 10, False)
    assert (second["body"], second["truncated"]) == ("乙" * 5, True)
    assert (third["body"], third["truncated"], third["body_chars"]) == ("", True, 10)


def test_get_argument_errors(vaults):
    with pytest.raises(TypeError):
        get(vaults, A, "a1", space="dev")
    with pytest.raises(ValueError):
        get(vaults, A, [], space="dev")
    with pytest.raises(ValueError):
        get(vaults, A, ["a"], space="dev", budget=0)
    with pytest.raises(VaultRequired):
        get(vaults, None, ["a"], space="dev")


# ── list ────────────────────────────────────────────────────────────


def test_list_paginates_with_opaque_cursor(vaults, add_note):
    for i in range(5):
        add_note(A, f"n-{i}", f"標題 {i}", ts=f"2026-09-0{i + 1}T00:00:00.000Z")
    first = list_(vaults, A, space="dev", limit=2)
    assert first.has_more and first.to_dict()["has_more"] is True
    assert [i["id"] for i in first.items] == ["n-4", "n-3"]
    seen = [i["id"] for i in first.items]
    cursor = first.next_cursor
    while cursor is not None:
        page = list_(vaults, A, space="dev", limit=2, cursor=cursor)
        seen += [i["id"] for i in page.items]
        cursor = page.next_cursor
    assert seen == ["n-4", "n-3", "n-2", "n-1", "n-0"]
    assert list_(vaults, A, space="dev", limit=5).has_more is False


def test_list_filters_topics_inside_the_query(vaults, add_note):
    add_note(A, "x1", "一", topics=("x",), ts="2026-09-05T00:00:00.000Z")
    for i in range(3):
        add_note(A, f"y{i}", "y", topics=("y",), ts=f"2026-09-0{i + 2}T00:00:00.000Z")
    add_note(A, "x2", "二", topics=("x", "z"), ts="2026-09-01T00:00:00.000Z")
    page = list_(vaults, A, space="dev", topics=["x"], limit=1)
    assert [i["id"] for i in page.items] == ["x1"] and page.has_more
    rest = list_(vaults, A, space="dev", topics=["x"], limit=1, cursor=page.next_cursor)
    assert [i["id"] for i in rest.items] == ["x2"] and not rest.has_more
    assert (
        list_(vaults, A, space="dev", since="2026-09-04T00:00:00Z").items[0]["id"]
        == "x1"
    )


@pytest.mark.parametrize("cursor", ["not-base64!", "WzFd", "e30="])
def test_list_rejects_bad_cursor(vaults, cursor):
    with pytest.raises(InvalidCursor):
        list_(vaults, A, space="dev", cursor=cursor)


# ── list 摘要預算：公平分配 ──────────────────────────────────────────


def _add_summaries(add_note, lengths, *, lead=False):
    """依頁序（新到舊）建 note；第 i 則摘要為 `lengths[i]` 個字。回傳頁序的 id。"""
    ids = [f"s{i:02d}" for i in range(len(lengths))]
    for i, (note_id, n) in enumerate(zip(ids, lengths, strict=True)):
        text = chr(0x4E00 + i) * n
        add_note(
            A,
            note_id,
            f"標題 {i}",
            text if lead else "正文",
            summary=None if lead else text,
            embed=False,
            # 越前面越新
            ts=f"2026-09-01T00:00:{59 - i:02d}.000Z",
        )
    return ids


def _page(vaults, budget, limit=50):
    result = list_(vaults, A, space="dev", budget=budget, limit=limit, kinds=["note"])
    return result, {i["id"]: i for i in result.items}


def test_list_budget_is_shared_fairly_across_long_summaries(vaults, add_note):
    ids = _add_summaries(add_note, [300] * 20)
    result, by_id = _page(vaults, 4000)
    # 依序分配時第 14 則起全部 omitted；公平分配每則都有開頭
    assert result.summaries_omitted == 0
    assert result.summaries_truncated == 20 and result.truncated is True
    for i, note_id in enumerate(ids):
        item = by_id[note_id]
        assert item["summary_source"] == "summary"
        assert item["summary_truncated"] is True
        assert len(item["summary"]) == 200
        assert item["summary"] == chr(0x4E00 + i) * 199 + "…"
    assert result.used_chars == 4000
    data = result.to_dict()
    assert data["summaries_truncated"] == 20 and data["summaries_omitted"] == 0


def test_list_budget_redistributes_unused_share(vaults, add_note):
    ids = _add_summaries(add_note, [500, 10, 500, 20])
    result, by_id = _page(vaults, 401)
    # 配額 100：10、20 全給，剩 371 由兩則長的平分（185），零頭 1 給頁序在前者
    assert [len(by_id[i]["summary"]) for i in ids] == [186, 10, 185, 20]
    assert [by_id[i]["summary_truncated"] for i in ids] == [True, False, True, False]
    assert result.used_chars == 401
    assert (result.summaries_truncated, result.summaries_omitted) == (2, 0)


def test_list_budget_omits_tail_only_below_floor(vaults, add_note):
    ids = _add_summaries(add_note, [100] * 5)
    result, by_id = _page(vaults, 130)
    # 下限 40：只給得起前 3 則，130 平分（44／43／43），尾端 2 則省略
    assert [len(by_id[i]["summary"] or "") for i in ids] == [44, 43, 43, 0, 0]
    assert [by_id[i]["summary_source"] for i in ids[3:]] == ["omitted"] * 2
    assert all(by_id[i]["summary_truncated"] is False for i in ids[3:])
    assert (result.summaries_truncated, result.summaries_omitted) == (3, 2)
    assert result.used_chars == 130 and result.truncated is True


def test_list_floor_counts_short_summaries_at_their_length(vaults, add_note):
    ids = _add_summaries(add_note, [100, 10, 10, 100])
    result, by_id = _page(vaults, 60)
    # 短摘要的下限需求就是它的長度（40+10+10），不會被無謂省略
    assert [len(by_id[i]["summary"] or "") for i in ids] == [40, 10, 10, 0]
    assert by_id[ids[3]]["summary_source"] == "omitted"
    assert (result.summaries_truncated, result.summaries_omitted) == (1, 1)


def test_list_budget_below_floor_still_gives_first_note(vaults, add_note):
    ids = _add_summaries(add_note, [100, 100])
    result, by_id = _page(vaults, 10)
    assert by_id[ids[0]]["summary"] == chr(0x4E00) * 9 + "…"
    assert by_id[ids[0]]["summary_truncated"] is True
    assert by_id[ids[1]]["summary_source"] == "omitted"
    assert (result.summaries_truncated, result.summaries_omitted) == (1, 1)


def test_list_lead_follows_same_budget_rule(vaults, add_note):
    ids = _add_summaries(add_note, [160, 160], lead=True)
    result, by_id = _page(vaults, 100)
    for i, note_id in enumerate(ids):
        item = by_id[note_id]
        assert item["summary_source"] == "lead"
        assert item["summary"] == chr(0x4E00 + i) * 49 + "…"
        assert item["summary_truncated"] is True
    assert (result.summaries_truncated, result.summaries_omitted) == (2, 0)


def test_list_notes_without_summary_take_no_share(vaults, add_note):
    add_note(A, "empty", "空", "", embed=False, ts="2026-09-02T00:00:00.000Z")
    add_note(A, "full", "滿", "正文", summary="甲" * 100, embed=False)
    result, by_id = _page(vaults, 60)
    assert by_id["empty"]["summary_source"] == "none"
    assert by_id["empty"]["summary_truncated"] is False
    assert len(by_id["full"]["summary"]) == 60
    assert (result.summaries_truncated, result.summaries_omitted) == (1, 0)


def test_list_budget_fits_everything_marks_nothing(vaults, add_note):
    _add_summaries(add_note, [100, 50])
    result, by_id = _page(vaults, 150)
    assert all(i["summary_truncated"] is False for i in result.items)
    assert result.truncated is False and result.used_chars == 150
    assert (result.summaries_truncated, result.summaries_omitted) == (0, 0)
