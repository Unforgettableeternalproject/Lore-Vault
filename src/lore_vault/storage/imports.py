"""外部來源匯入的對帳清單（T-37）：寫入與對帳（純標準庫）。

流程：匯入工具先以 `record_manifest` 整批登記「來源應有什麼」（每則 note 的
來源內容雜湊、每個 vault 的來源筆數），**之後**才逐筆寫 note，成功後以
`mark_imported` 記下當時存下的 `updated`。清單與 note 不在同一交易——
漏匯一筆時清單仍在，對帳才看得出來。

對帳（`reconcile`）規則：
- 清單有、`notes` 沒有，或清單標記尚未匯入 → 漏筆（fail）
- note 的 `updated` 等於匯入當下的值、但 (title, body) 雜湊不符 → 被竄改（fail）。
  經服務正常修改一定會推進 `updated`；補摘要不推進 `updated`、也不動 title／body
- `updated` 已推進 → 匯入後在新系統修改過（只報告）
- vault 內多出清單沒有的 note → 新系統新增的（只報告）
- 每個 vault 的來源筆數 ≠ 清單列數或 ≠ 實際存在的筆數 → fail
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .checks import MAX_DETAILS, Reconciliation
from .db import transaction
from .timeutil import utc_now


class MissingImportTables(LookupError):
    """資料庫尚未遷移到含 `import_sources` 的版本。"""


def content_sha256(title: str, body: str) -> str:
    """來源內容雜湊：只含 title 與 body（摘要、連結、topics 會由新系統補或改）。"""
    payload = json.dumps([title, body], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ManifestEntry:
    source_id: str
    note_id: str
    vault: str
    content_sha256: str
    source_updated: str


@dataclass(frozen=True)
class ManifestRow:
    source_id: str
    note_id: str
    vault: str
    content_sha256: str
    source_updated: str
    imported_updated: str | None
    imported_at: str | None


def _has_tables(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT count(*) FROM sqlite_master WHERE type = 'table'
          AND name IN ('import_sources', 'import_vault_counts')
        """
    ).fetchone()
    return int(rows[0]) == 2


def require_tables(conn: sqlite3.Connection) -> None:
    if not _has_tables(conn):
        raise MissingImportTables(
            "缺少 import_sources／import_vault_counts 表（schema 未遷移）"
        )


def manifest_rows(conn: sqlite3.Connection, source: str) -> dict[str, ManifestRow]:
    """source_id → 清單列。"""
    require_tables(conn)
    rows = conn.execute(
        """
        SELECT source_id, note_id, vault, content_sha256, source_updated,
               imported_updated, imported_at
        FROM import_sources WHERE source = ?
        """,
        (source,),
    ).fetchall()
    return {r[0]: ManifestRow(*r) for r in rows}


def record_manifest(
    conn: sqlite3.Connection,
    source: str,
    entries: Iterable[ManifestEntry],
    vault_counts: Mapping[str, int],
) -> dict[str, int]:
    """整批登記來源清單（同一交易）。回傳 {"added", "changed", "removed"}。

    - 新來源列：`imported_updated` 為 NULL（尚未匯入）
    - 既有列內容雜湊或 vault 變了：更新雜湊，`imported_updated` 保留（由匯入端
      決定覆寫或跳過後再標記）
    - 這次來源沒有的舊列刪除（其 note 若還在，對帳時算「多出來」）
    - vault 來源筆數整批替換
    """
    require_tables(conn)
    entries = list(entries)
    stats = {"added": 0, "changed": 0, "removed": 0}
    with transaction(conn):
        existing = manifest_rows(conn, source)
        seen: set[str] = set()
        for entry in entries:
            if entry.source_id in seen:
                raise ValueError(f"來源 id 重複：{entry.source_id!r}")
            seen.add(entry.source_id)
            old = existing.get(entry.source_id)
            if old is None:
                conn.execute(
                    """
                    INSERT INTO import_sources (source, source_id, note_id, vault,
                        content_sha256, source_updated, imported_updated, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        source,
                        entry.source_id,
                        entry.note_id,
                        entry.vault,
                        entry.content_sha256,
                        entry.source_updated,
                    ),
                )
                stats["added"] += 1
            elif (
                old.content_sha256,
                old.source_updated,
                old.vault,
                old.note_id,
            ) != (
                entry.content_sha256,
                entry.source_updated,
                entry.vault,
                entry.note_id,
            ):
                conn.execute(
                    """
                    UPDATE import_sources SET note_id = ?, vault = ?,
                        content_sha256 = ?, source_updated = ?
                    WHERE source = ? AND source_id = ?
                    """,
                    (
                        entry.note_id,
                        entry.vault,
                        entry.content_sha256,
                        entry.source_updated,
                        source,
                        entry.source_id,
                    ),
                )
                stats["changed"] += 1
        for source_id in sorted(set(existing) - seen):
            conn.execute(
                "DELETE FROM import_sources WHERE source = ? AND source_id = ?",
                (source, source_id),
            )
            stats["removed"] += 1
        now = utc_now()
        conn.execute("DELETE FROM import_vault_counts WHERE source = ?", (source,))
        conn.executemany(
            """
            INSERT INTO import_vault_counts (source, vault, source_count, recorded)
            VALUES (?, ?, ?, ?)
            """,
            [(source, vault, int(n), now) for vault, n in sorted(vault_counts.items())],
        )
    return stats


def mark_imported(
    conn: sqlite3.Connection, source: str, source_id: str, imported_updated: str
) -> None:
    """記下這則來源 note 在新系統存下的 `updated`（對帳判斷「是否被改過」的基準）。"""
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE import_sources SET imported_updated = ?, imported_at = ?
            WHERE source = ? AND source_id = ?
            """,
            (imported_updated, utc_now(), source, source_id),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"清單沒有來源 {source!r}/{source_id!r}")


def import_sources(conn: sqlite3.Connection) -> list[str]:
    """資料庫中有對帳清單的來源名稱。"""
    require_tables(conn)
    rows = conn.execute(
        """
        SELECT source FROM import_sources
        UNION SELECT source FROM import_vault_counts ORDER BY 1
        """
    ).fetchall()
    return [r[0] for r in rows]


def reconcile(conn: sqlite3.Connection, source: str) -> Reconciliation:
    """比對對帳清單與目前的 notes。見模組說明。"""
    require_tables(conn)
    manifest = manifest_rows(conn, source)
    expected_counts = {
        r[0]: int(r[1])
        for r in conn.execute(
            "SELECT vault, source_count FROM import_vault_counts WHERE source = ?",
            (source,),
        )
    }
    notes = {
        r[0]: (r[1], r[2], r[3], r[4])
        for r in conn.execute("SELECT id, vault, title, body, updated FROM notes")
    }

    missing: list[str] = []
    tampered: list[str] = []
    modified: list[str] = []
    wrong_vault: list[str] = []
    present_by_vault: dict[str, int] = {}
    manifest_by_vault: dict[str, int] = {}
    for row in sorted(manifest.values(), key=lambda r: (r.vault, r.source_id)):
        manifest_by_vault[row.vault] = manifest_by_vault.get(row.vault, 0) + 1
        found = notes.get(row.note_id)
        if found is None or row.imported_updated is None:
            state = "尚未匯入" if found is not None else "notes 中不存在"
            missing.append(f"漏筆 {row.vault}/{row.note_id}（{state}）")
            continue
        vault, title, body, updated = found
        if vault != row.vault:
            wrong_vault.append(
                f"vault 不符 {row.note_id}：清單 {row.vault}、實際 {vault}"
            )
            continue
        present_by_vault[vault] = present_by_vault.get(vault, 0) + 1
        if updated != row.imported_updated:
            modified.append(row.note_id)
            continue
        if content_sha256(title, body) != row.content_sha256:
            tampered.append(f"內容雜湊不符 {vault}/{row.note_id}（updated 未推進）")

    count_errors: list[str] = []
    vaults = sorted(set(expected_counts) | set(manifest_by_vault))
    for vault in vaults:
        expected = expected_counts.get(vault)
        listed = manifest_by_vault.get(vault, 0)
        present = present_by_vault.get(vault, 0)
        if expected is None:
            count_errors.append(f"{vault}：清單有 {listed} 列但沒有來源筆數")
        elif listed != expected or present != expected:
            count_errors.append(
                f"{vault}：來源 {expected}、清單 {listed}、實際 {present}"
            )

    manifest_ids = {r.note_id for r in manifest.values()}
    extras = sum(
        1
        for note_id, (vault, *_rest) in notes.items()
        if vault in expected_counts and note_id not in manifest_ids
    )

    counts = {
        "vaults": len(vaults),
        "source_notes": sum(expected_counts.values()),
        "manifest": len(manifest),
        "present": sum(present_by_vault.values()),
        "missing": len(missing),
        "tampered": len(tampered),
        "wrong_vault": len(wrong_vault),
        "count_mismatch": len(count_errors),
        "modified_after_import": len(modified),
        "extra_notes": extras,
    }
    problems = count_errors + missing + tampered + wrong_vault
    if not problems:
        note = []
        if modified:
            note.append(f"{len(modified)} 則匯入後已在新系統修改")
        if extras:
            note.append(f"{extras} 則為新系統新增")
        suffix = f"（{'；'.join(note)}）" if note else ""
        return Reconciliation(
            "pass",
            f"{source}：{len(vaults)} 個 vault、{len(manifest)} 則與來源一致{suffix}",
            counts,
            tuple(f"{v}: {expected_counts.get(v, 0)}" for v in vaults[:MAX_DETAILS]),
        )
    return Reconciliation(
        "fail",
        f"{source}：漏筆 {len(missing)}、竄改 {len(tampered)}、"
        f"vault 不符 {len(wrong_vault)}、筆數不符 {len(count_errors)} 個 vault",
        counts,
        tuple(problems[:MAX_DETAILS]),
    )
