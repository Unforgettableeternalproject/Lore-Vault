"""Vault／Note／Episode／Concept／Injection 型別定義。

欄位依 docs/ARCHITECTURE.md「資料模型」；Episode／Concept／Injection 對照
agent_memory_spike 實際寫出的欄位（transcript.build_episode、distill.ingest、
calibrate.ingest、hook_*.record_injection）。

全部是 frozen dataclass：歷史歸屬（repo、repo_root、machine、scope）
寫入後不可在原物件上改；
要修改用 `dataclasses.replace` 產生新物件，讓「改了什麼」是顯式的動作。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from ._base import (
    MISSING,
    Record,
    _Missing,
    check_required_declared,
    fail,
    opt_mapping,
    opt_str,
    opt_utc_timestamp,
    req_bool,
    req_int,
    req_str,
    set_field,
    str_tuple,
    utc_timestamp,
)

VAULT_KINDS = frozenset({"repo", "global"})

# 內容分群（A18）：space 是 vault 的屬性，與 vault 為 AND 疊加的硬範圍。
# 資料庫不加 CHECK（ALTER TABLE ADD COLUMN 的限制），由這裡的白名單與
# doctor `space.valid_values` 把關。
SPACE_DEV = "dev"
SPACE_LORE = "lore"
SPACE_PERSONAL = "personal"
SPACES = frozenset({SPACE_DEV, SPACE_LORE, SPACE_PERSONAL})

# 與 agent_memory_spike/transcript.py 的 ORIGIN_* 一致
EPISODE_ORIGINS = frozenset({"human", "task-notification", "system", "meta", "unknown"})

CONCEPT_KINDS = frozenset({"project-fact", "belief-correction", "user-stance"})

# spike 的 distill.GLOBAL_LITERALS：蒸餾輸出裡代表「通用」的字串。
# 儲存層只接受 None 表示通用，字串形式一律拒絕——
# 同一語意兩種寫法，正是三條注入路徑判斷分岔的起因。
SCOPE_GLOBAL_LITERALS = frozenset({"null", "none", "global", "*"})


# ── Vault ────────────────────────────────────────────────────────────


def canonical_key(key: str) -> str:
    """vault key／別名的標準形式：小寫（與 pm-bind 的正規化一致）。

    現行 ON 有手寫的 `folder/MCSF`，pm-bind 產生 `folder/mcsf`——
    建構時就統一，避免同一個 vault 因大小寫分裂成兩個。
    """
    return key.lower()


@dataclass(frozen=True)
class Vault(Record):
    """一個記憶範圍，通常對應一個 repo。

    `key` 與 `aliases` 在建構時正規化成小寫（`canonical_key`）；`display` 保留原大小寫。
    """

    key: str
    display: str
    kind: str = "repo"
    # 改名前的舊 key／舊 repo 名（取代 spike 的 REPO_ALIASES）
    aliases: tuple[str, ...] = ()
    # 所屬 space（A18）；非 dev 的 key 前綴規則由儲存層寫入路徑驗證
    space: str = SPACE_DEV

    REQUIRED: ClassVar[frozenset[str]] = frozenset({"key", "display"})

    def __post_init__(self) -> None:
        req_str("Vault", "key", self.key)
        if self.key != self.key.strip():
            raise fail("Vault", "key", f"前後不可有空白：{self.key!r}")
        set_field(self, "key", canonical_key(self.key))
        req_str("Vault", "display", self.display)
        if self.kind not in VAULT_KINDS:
            allowed = sorted(VAULT_KINDS)
            raise fail("Vault", "kind", f"必須是 {allowed}，得到 {self.kind!r}")
        aliases = tuple(
            canonical_key(a) for a in str_tuple("Vault", "aliases", self.aliases)
        )
        if self.key in aliases:
            raise fail("Vault", "aliases", f"不可包含自己的 key {self.key!r}")
        if len(set(aliases)) != len(aliases):
            raise fail("Vault", "aliases", "有重複項")
        set_field(self, "aliases", aliases)
        if self.space not in SPACES:
            allowed = sorted(SPACES)
            raise fail("Vault", "space", f"必須是 {allowed}，得到 {self.space!r}")


# ── Note ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Note(Record):
    """寫下的結論。`updated` 同時是樂觀鎖的版本（update 帶 expected_updated 比對）。"""

    id: str
    vault: str
    title: str
    body: str
    created: str
    updated: str
    # write 不等 LLM，摘要允許暫缺（D1／A9；產生方式待 D4）
    summary: str | None = None
    topics: tuple[str, ...] = ()
    # `[[標題]]` 解析後的 note id
    links: tuple[str, ...] = ()
    # 更正關係：這篇取代哪篇（不另建更正篇）
    supersedes: str | None = None

    REQUIRED: ClassVar[frozenset[str]] = frozenset(
        {"id", "vault", "title", "body", "created", "updated"}
    )

    def __post_init__(self) -> None:
        req_str("Note", "id", self.id)
        req_str("Note", "vault", self.vault)
        req_str("Note", "title", self.title)
        req_str("Note", "body", self.body, allow_empty=True)
        # 空字串摘要與「還沒產生」無法區分，一律要求用 None
        opt_str("Note", "summary", self.summary)
        utc_timestamp("Note", "created", self.created)
        utc_timestamp("Note", "updated", self.updated)
        if _ts(self.updated) < _ts(self.created):
            raise fail("Note", "updated", "不可早於 created")
        set_field(self, "topics", str_tuple("Note", "topics", self.topics))
        set_field(self, "links", str_tuple("Note", "links", self.links))
        opt_str("Note", "supersedes", self.supersedes)
        if self.supersedes == self.id:
            raise fail("Note", "supersedes", "不可取代自己")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ── Episode ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolCount(Record):
    """Episode.tool_sequence 的一項：`{"name": ..., "count": ...}`。"""

    name: str
    count: int

    REQUIRED: ClassVar[frozenset[str]] = frozenset({"name", "count"})

    def __post_init__(self) -> None:
        req_str("ToolCount", "name", self.name)
        req_int("ToolCount", "count", self.count, minimum=1)


@dataclass(frozen=True)
class Episode(Record):
    """發生過的事：每輪對話一筆。欄位對照 spike `transcript.build_episode`。

    凍結欄位：`repo`、`repo_root`、`machine`——寫入當下固定，
    讀取時不依環境重算（spike 2026-08-24 repo 改名事故，A7）。
    唯一鍵是 (session_id, prompt_id, turn_index)。
    """

    prompt_id: str
    turn_index: int
    session_id: str
    agent: str
    origin: str
    machine: str
    started_at: str | None
    ended_at: str | None
    cwd: tuple[str, ...]
    repo: str | None
    repo_root: str | None
    git_branch: tuple[str, ...]
    cc_version: str | None
    user_text: str
    assistant_text: str
    tool_sequence: tuple[ToolCount, ...]
    tool_calls_total: int
    mcp_tools: tuple[str, ...]
    skills: tuple[str, ...]
    files_edited: tuple[str, ...]
    files_read: tuple[str, ...]
    symbols_edited: tuple[str, ...]
    thinking_blocks: int
    # 三態：[] = 這輪沒被注入；MISSING = 語料早於注入 schema（spike 刻意區分）。
    # 校準要排除非空輪次，MISSING 則是「不知道」，不可當成乾淨語料。
    injected: tuple[str, ...] | _Missing = MISSING

    REQUIRED: ClassVar[frozenset[str]] = frozenset(
        {
            "prompt_id",
            "turn_index",
            "session_id",
            "agent",
            "origin",
            "machine",
            "started_at",
            "ended_at",
            "cwd",
            "repo",
            "repo_root",
            "git_branch",
            "cc_version",
            "user_text",
            "assistant_text",
            "tool_sequence",
            "tool_calls_total",
            "mcp_tools",
            "skills",
            "files_edited",
            "files_read",
            "symbols_edited",
            "thinking_blocks",
        }
    )

    def __post_init__(self) -> None:
        o = "Episode"
        req_str(o, "prompt_id", self.prompt_id)
        req_int(o, "turn_index", self.turn_index)
        req_str(o, "session_id", self.session_id)
        req_str(o, "agent", self.agent)
        if self.origin not in EPISODE_ORIGINS:
            allowed = sorted(EPISODE_ORIGINS)
            raise fail(o, "origin", f"必須是 {allowed}，得到 {self.origin!r}")
        req_str(o, "machine", self.machine)
        opt_utc_timestamp(o, "started_at", self.started_at)
        opt_utc_timestamp(o, "ended_at", self.ended_at)
        if (
            self.started_at is not None
            and self.ended_at is not None
            and _ts(self.ended_at) < _ts(self.started_at)
        ):
            raise fail(o, "ended_at", "不可早於 started_at")
        opt_str(o, "repo", self.repo)
        opt_str(o, "repo_root", self.repo_root)
        opt_str(o, "cc_version", self.cc_version)
        req_str(o, "user_text", self.user_text, allow_empty=True)
        req_str(o, "assistant_text", self.assistant_text, allow_empty=True)
        for name in (
            "cwd",
            "git_branch",
            "mcp_tools",
            "skills",
            "files_edited",
            "files_read",
            "symbols_edited",
        ):
            set_field(self, name, str_tuple(o, name, getattr(self, name)))
        seq = self.tool_sequence
        if isinstance(seq, str) or not isinstance(seq, (list, tuple)):
            raise fail(o, "tool_sequence", "必須是清單")
        for index, item in enumerate(seq):
            if not isinstance(item, ToolCount):
                raise fail(o, f"tool_sequence[{index}]", "必須是 ToolCount")
        set_field(self, "tool_sequence", tuple(seq))
        req_int(o, "tool_calls_total", self.tool_calls_total)
        req_int(o, "thinking_blocks", self.thinking_blocks)
        if self.injected is not MISSING:
            set_field(self, "injected", str_tuple(o, "injected", self.injected))

    @classmethod
    def _convert(cls, data: dict[str, Any]) -> dict[str, Any]:
        seq = data.get("tool_sequence")
        if isinstance(seq, (list, tuple)):
            data["tool_sequence"] = [
                ToolCount.from_dict(item) if not isinstance(item, ToolCount) else item
                for item in seq
            ]
        return data


# ── Concept ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceTurn(Record):
    """Concept 溯源的一輪：spike 存成 `[prompt_id, turn_index]`。"""

    prompt_id: str
    turn_index: int

    REQUIRED: ClassVar[frozenset[str]] = frozenset({"prompt_id", "turn_index"})

    def __post_init__(self) -> None:
        req_str("SourceTurn", "prompt_id", self.prompt_id)
        req_int("SourceTurn", "turn_index", self.turn_index)

    def to_dict(self) -> list[Any]:  # type: ignore[override]
        # 維持 spike 的 pair 形狀，匯入匯出不需轉換
        return [self.prompt_id, self.turn_index]

    @classmethod
    def from_value(cls, value: Any) -> SourceTurn:
        if isinstance(value, SourceTurn):
            return value
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return cls(value[0], value[1])
        raise fail(
            "Concept", "source_turns", f"每項必須是 [prompt_id, turn_index]：{value!r}"
        )


@dataclass(frozen=True)
class Concept(Record):
    """蒸餾出的記憶。欄位對照 spike `distill.ingest` 與 `calibrate.ingest`。

    `scope` 三態：
    - `str`：repo 名（蒸餾當下的名稱，改名靠 Vault.aliases 在讀取端接起來）
    - `None`：明確表態為跨專案通用
    - `MISSING`：沒說（spike 的 resolve_scope 會退回觀察到它的 repo；
      這裡保留原狀，由上層決定，不在 schema 層猜）
    """

    id: str
    statement: str
    kind: str | None
    scope: str | None | _Missing = MISSING
    # 檢索索引的是 cue 而非 statement
    cue: str | None = None
    probe: str | None = None
    why: str | None = None
    # 溯源
    source_candidate: str | None = None
    from_signal: bool = True
    source_turns: tuple[SourceTurn, ...] = ()
    # 產生這條記憶那一輪碰過的檔案——溯源用，不可當檢索錨點
    source_files: tuple[str, ...] = ()
    # 檢索錨點：檔案路徑、函式名、欄位名
    anchors: tuple[str, ...] = ()
    # 校準（行為測試）紀錄；蒸餾階段為 None
    surprisal: float | None = None
    probe_result: dict[str, Any] | None = None
    usability: dict[str, Any] | None = None

    REQUIRED: ClassVar[frozenset[str]] = frozenset({"id", "statement", "kind"})

    def __post_init__(self) -> None:
        o = "Concept"
        req_str(o, "id", self.id)
        req_str(o, "statement", self.statement)
        if self.kind is not None and self.kind not in CONCEPT_KINDS:
            allowed = sorted(CONCEPT_KINDS)
            raise fail(o, "kind", f"必須是 {allowed} 或 null，得到 {self.kind!r}")
        if self.scope is not MISSING and self.scope is not None:
            req_str(o, "scope", self.scope)
            if self.scope.strip().lower() in SCOPE_GLOBAL_LITERALS:
                raise fail(
                    o, "scope", f"跨專案通用請用 null，不可用字串 {self.scope!r}"
                )
        opt_str(o, "cue", self.cue)
        opt_str(o, "probe", self.probe)
        opt_str(o, "why", self.why)
        opt_str(o, "source_candidate", self.source_candidate)
        req_bool(o, "from_signal", self.from_signal)
        turns = self.source_turns
        if isinstance(turns, str) or not isinstance(turns, (list, tuple)):
            raise fail(o, "source_turns", "必須是清單")
        set_field(self, "source_turns", tuple(SourceTurn.from_value(t) for t in turns))
        set_field(self, "source_files", str_tuple(o, "source_files", self.source_files))
        set_field(self, "anchors", str_tuple(o, "anchors", self.anchors))
        if self.surprisal is not None:
            if isinstance(self.surprisal, bool) or not isinstance(
                self.surprisal, (int, float)
            ):
                raise fail(o, "surprisal", "必須是數值或 null")
            if not 0.0 <= self.surprisal <= 1.0:
                raise fail(o, "surprisal", f"必須在 0–1 之間，得到 {self.surprisal}")
        for name in ("probe_result", "usability"):
            set_field(self, name, opt_mapping(o, name, getattr(self, name)))

    @property
    def is_global(self) -> bool:
        """明確表態為跨專案通用。MISSING 不算（沒說不等於通用）。"""
        return self.scope is None


# ── Injection ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Injection(Record):
    """注入 side-car 紀錄，不含原文。對照 spike 各 hook 的 record_injection。

    `prompt_id`：PreToolUse 帶真實 id；SessionStart 用哨兵 `__session__`；
    UserPromptSubmit 可能為 None，此時靠 `prompt_fingerprint` 歸屬。
    兩者皆缺就無法標記被影響的輪次，直接拒絕。
    """

    session_id: str
    prompt_id: str | None
    injected: tuple[str, ...]
    prompt_fingerprint: str | None = None
    # 重新編號遷移時標記：injected 中曾撞號的 id，無法判定當時是哪一條 concept。
    # MISSING = 未經重新編號檢查（一般紀錄），輸出時省略。
    ambiguous_ids: tuple[str, ...] | _Missing = MISSING

    REQUIRED: ClassVar[frozenset[str]] = frozenset(
        {"session_id", "prompt_id", "injected"}
    )

    def __post_init__(self) -> None:
        o = "Injection"
        req_str(o, "session_id", self.session_id)
        opt_str(o, "prompt_id", self.prompt_id)
        opt_str(o, "prompt_fingerprint", self.prompt_fingerprint)
        if self.prompt_id is None and self.prompt_fingerprint is None:
            raise fail(o, "prompt_id", "prompt_id 與 prompt_fingerprint 至少要有一個")
        set_field(self, "injected", str_tuple(o, "injected", self.injected))
        if self.ambiguous_ids is not MISSING:
            ids = str_tuple(o, "ambiguous_ids", self.ambiguous_ids)
            if not set(ids) <= set(self.injected):
                raise fail(o, "ambiguous_ids", "必須是 injected 的子集")
            set_field(self, "ambiguous_ids", ids)


for _cls in (Vault, Note, ToolCount, Episode, SourceTurn, Concept, Injection):
    check_required_declared(_cls, _cls.REQUIRED)
del _cls
