"""`[[標題]]` 連結：從正文抽出目標、在同一 vault 內依標題解析成 note id。

規則（寫入與舊 PM 匯入共用）：
- 標題本身可能含一層方括號（如 `[Decision] X` → `[[[Decision] X]]`）
- 先比對原文，再比對去掉 `|別名`、`#段落` 的形式
- 標題比對形式：壓空白 + casefold（同查重的標題規則）
- 只在同一 vault 內解析；唯一命中才採用。解析不到（`unresolved`）或同 vault 多則同名
  （`ambiguous`，附候選 id）的保留原文、不寫進 links；指向自己的（`self`）不寫入
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

LINK_PATTERN = re.compile(r"\[\[((?:[^\[\]\n]|\[[^\[\]\n]*\])+?)\]\]")
_WHITESPACE = re.compile(r"\s+")

STATUS_UNRESOLVED = "unresolved"
STATUS_AMBIGUOUS = "ambiguous"


def norm_title(title: str) -> str:
    """標題比對形式：壓空白 + casefold。"""
    return _WHITESPACE.sub(" ", title).strip().casefold()


def link_targets(body: str) -> list[str]:
    """正文中所有 `[[…]]` 的原文（依出現順序，可重複）。"""
    return LINK_PATTERN.findall(body)


def target_candidates(raw: str) -> list[str]:
    """先比對原文，再比對去掉 `|別名`、`#段落` 的形式。"""
    forms = [norm_title(raw)]
    stripped = norm_title(raw.split("|", 1)[0].split("#", 1)[0])
    if stripped and stripped not in forms:
        forms.append(stripped)
    return forms


@dataclass(frozen=True)
class BodyLinks:
    # 解析成功的 note id（依首次出現順序、去重、不含自己）
    ids: tuple[str, ...]
    # 解析不到或歧義的連結：{target, status, candidates}
    unresolved: tuple[dict[str, Any], ...]


def _title_index(conn: sqlite3.Connection, vault_key: str) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for note_id, title in conn.execute(
        "SELECT id, title FROM notes WHERE vault = ? ORDER BY id", (vault_key,)
    ):
        index.setdefault(norm_title(title), []).append(note_id)
    return index


def resolve_body_links(
    conn: sqlite3.Connection,
    vault_key: str,
    body: str,
    *,
    self_id: str | None = None,
    self_title: str | None = None,
) -> BodyLinks:
    """在已解析的 vault key 內解析 `body` 的 `[[標題]]`。

    `self_id`／`self_title`：正在寫入的 note 本身（update 時為其 id 與新標題；write 時
    只有標題）。指向自己的連結靜默略過，不寫入也不列為 unresolved。
    """
    targets = link_targets(body)
    if not targets:
        return BodyLinks((), ())
    index = _title_index(conn, vault_key)
    own = norm_title(self_title) if self_title is not None else None
    ids: list[str] = []
    unresolved: list[dict[str, Any]] = []
    reported: set[str] = set()
    for raw in targets:
        status = STATUS_UNRESOLVED
        candidates: list[str] = []
        is_self = False
        for form in target_candidates(raw):
            if own is not None and form == own:
                is_self = True
                break
            hits = [i for i in index.get(form, []) if i != self_id]
            if len(hits) == 1:
                status, candidates = "resolved", hits
                break
            if len(hits) > 1:
                status, candidates = STATUS_AMBIGUOUS, hits
                break
        if is_self:
            continue
        if status == "resolved":
            if candidates[0] not in ids:
                ids.append(candidates[0])
            continue
        if raw in reported:
            continue
        reported.add(raw)
        unresolved.append({"target": raw, "status": status, "candidates": candidates})
    return BodyLinks(tuple(ids), tuple(unresolved))


def merge_links(explicit: Sequence[str], resolved: Iterable[str]) -> tuple[str, ...]:
    """明確傳入的在前、正文解析的在後，去重保序。"""
    return tuple(dict.fromkeys([*explicit, *resolved]))
