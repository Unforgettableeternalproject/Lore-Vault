"""待審提案對帳（add-agent-review-proposals）：`proposals.integrity`、
`proposals.expiry`、`notes.write_origin`。

資源 `db`：sqlite3 連線；設定鍵 `now`（datetime，測試注入）、`proposals_open_warn_days`
（開放提案放多久開始警告，預設 10 天）。比照 `notes.review` 的對帳風格：只輸出計數與
前幾筆 id，**不輸出任何提案 payload 或筆記內容**。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from lore_vault.storage.checks import MAX_DETAILS
from lore_vault.storage.timeutil import format_utc

from .framework import CheckResult, CheckSkipped, DoctorContext

DEFAULT_OPEN_WARN_DAYS = 10.0
_OPEN = "('pending', 'approved', 'executing')"


def _db(ctx: DoctorContext) -> sqlite3.Connection:
    db = ctx.require("db")
    has = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'review_proposals'"
    ).fetchone()
    if has is None:
        raise CheckSkipped("缺少 review_proposals 表（schema 未遷移到 v20）")
    return db


def _now(ctx: DoctorContext) -> datetime:
    now = ctx.settings.get("now")
    return now if isinstance(now, datetime) else datetime.now(UTC)


def _ids(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[str]:
    return [r[0] for r in db.execute(sql, args)]


def proposals_integrity(ctx: DoctorContext) -> CheckResult:
    """違反任一條為 fail：

    1. 開放提案的目標不存在（應已轉 obsolete）
    2. 同 (vault, kind, target) 有多筆開放提案（唯一索引被拿掉）
    3. 開放提案的 space 與 vault 目前所屬 space 不一致，或 vault 不存在
    4. applied 的 note_update／link_change：目標 `updated` 等於 `applied_updated` 卻不是
       ui 來源，或目標比套用結果更舊（後續合法寫入讓 `updated` 前進則不算）
    5. executing 的租約已逾時（殼當掉且沒有被回復）
    6. task_archive 的 change 已不是 active（應已轉 obsolete 或 applied）
    """
    db = _db(ctx)
    now = format_utc(_now(ctx))
    rules: dict[str, str] = {
        "open_target_missing": f"""
            SELECT p.id FROM review_proposals p WHERE p.state IN {_OPEN} AND (
              (p.target_type = 'note' AND NOT EXISTS (
                 SELECT 1 FROM notes n WHERE n.id = p.target_id AND n.vault = p.vault))
              OR (p.target_type = 'document' AND NOT EXISTS (
                 SELECT 1 FROM documents d WHERE d.id = p.target_id
                 AND d.vault = p.vault))
              OR (p.target_type = 'task_change' AND NOT EXISTS (
                 SELECT 1 FROM sidecar_blobs s WHERE s.vault = p.vault
                 AND s.key = 'task-change:' || p.target_id)))
            ORDER BY p.id""",
        "duplicate_open": f"""
            SELECT min(id) FROM review_proposals WHERE state IN {_OPEN}
            GROUP BY vault, kind, target_id HAVING count(*) > 1 ORDER BY 1""",
        "space_mismatch": f"""
            SELECT p.id FROM review_proposals p
            WHERE p.state IN {_OPEN} AND NOT EXISTS (
              SELECT 1 FROM vaults v WHERE v.key = p.vault AND v.space = p.space)
            ORDER BY p.id""",
        "applied_not_ui": """
            SELECT p.id FROM review_proposals p JOIN notes n
              ON n.id = p.target_id AND n.vault = p.vault
            WHERE p.state = 'applied' AND p.kind IN ('note_update', 'link_change')
              AND p.applied_updated IS NOT NULL
              AND n.updated = p.applied_updated AND n.write_origin != 'ui'
            ORDER BY p.id""",
        "applied_target_older": """
            SELECT p.id FROM review_proposals p JOIN notes n
              ON n.id = p.target_id AND n.vault = p.vault
            WHERE p.state = 'applied' AND p.kind IN ('note_update', 'link_change')
              AND p.applied_updated IS NOT NULL AND n.updated < p.applied_updated
            ORDER BY p.id""",
        "lease_expired": """
            SELECT id FROM review_proposals WHERE state = 'executing'
              AND (claim_expires_at IS NULL OR claim_expires_at <= ?) ORDER BY id""",
    }
    counts: dict[str, int] = {}
    details: list[str] = []
    for name, sql in rules.items():
        ids = _ids(db, sql, (now,) if "?" in sql else ())
        counts[name] = len(ids)
        details.extend(f"proposal {i} 違反 {name}" for i in ids)
    # task_archive 的 change 內容在 sidecar（JSON）；不是 active 就不該還開著
    stale_archive = []
    for row in db.execute(
        f"""SELECT p.id, s.content FROM review_proposals p JOIN sidecar_blobs s
            ON s.vault = p.vault AND s.key = 'task-change:' || p.target_id
            WHERE p.kind = 'task_archive' AND p.state IN {_OPEN} ORDER BY p.id"""
    ):
        text = bytes(row[1]).decode("utf-8", "replace")
        if '"state":"active"' not in text.replace(" ", ""):
            stale_archive.append(row[0])
    counts["archive_change_not_active"] = len(stale_archive)
    details.extend(
        f"proposal {i} 違反 archive_change_not_active" for i in stale_archive
    )
    total = int(db.execute("SELECT count(*) FROM review_proposals").fetchone()[0])
    open_count = int(
        db.execute(
            f"SELECT count(*) FROM review_proposals WHERE state IN {_OPEN}"
        ).fetchone()[0]
    )
    counts.update(proposals=total, open=open_count)
    bad = sum(v for k, v in counts.items() if k not in ("proposals", "open"))
    if not bad:
        return CheckResult.ok(
            f"提案資料一致（共 {total} 筆，開放 {open_count} 筆）", counts=counts
        )
    return CheckResult.fail(
        f"{bad} 項提案資料不一致", details=details[:MAX_DETAILS], counts=counts
    )


def proposals_expiry(ctx: DoctorContext) -> CheckResult:
    """pending／approved 超過 expires_at 還沒轉 expired，或開放太久沒人處理（warn）。"""
    db = _db(ctx)
    moment = _now(ctx)
    now = format_utc(moment)
    days = float(ctx.settings.get("proposals_open_warn_days", DEFAULT_OPEN_WARN_DAYS))
    old = format_utc(moment - timedelta(days=days))
    overdue = _ids(
        db,
        "SELECT id FROM review_proposals WHERE state IN ('pending', 'approved') "
        "AND expires_at <= ? ORDER BY id",
        (now,),
    )
    aged = _ids(
        db,
        "SELECT id FROM review_proposals WHERE state IN ('pending', 'approved') "
        "AND expires_at > ? AND created_at <= ? ORDER BY id",
        (now, old),
    )
    counts = {"overdue": len(overdue), "aged": len(aged)}
    if not overdue and not aged:
        return CheckResult.ok("沒有逾期或久未處理的提案", counts=counts)
    details = [f"proposal {i} 已過期但尚未轉 expired" for i in overdue] + [
        f"proposal {i} 開放超過 {days:g} 天" for i in aged
    ]
    return CheckResult.warn(
        f"{len(overdue)} 筆逾期未轉換、{len(aged)} 筆久未處理",
        details=details[:MAX_DETAILS],
        counts=counts,
    )


def notes_write_origin(ctx: DoctorContext) -> CheckResult:
    """`notes.write_origin` 在白名單內（CHECK 被繞過時抓得到），且 ui 來源的 note 一定
    有最後修改者 principal（受保護的判定依據）。"""
    db = ctx.require("db")
    if not any(r[1] == "write_origin" for r in db.execute("PRAGMA table_info(notes)")):
        raise CheckSkipped("缺少 notes.write_origin 欄（schema 未遷移到 v20）")
    rules = {
        "invalid_origin": (
            "SELECT id FROM notes WHERE write_origin NOT IN ('ui', 'agent') ORDER BY id"
        ),
        "ui_without_editor": (
            "SELECT id FROM notes WHERE write_origin = 'ui' AND "
            "(updated_by_principal IS NULL OR trim(updated_by_principal) = '') "
            "ORDER BY id"
        ),
    }
    counts: dict[str, int] = {}
    details: list[str] = []
    for name, sql in rules.items():
        ids = _ids(db, sql)
        counts[name] = len(ids)
        details.extend(f"note {i} 違反 {name}" for i in ids)
    counts["ui_origin"] = int(
        db.execute("SELECT count(*) FROM notes WHERE write_origin = 'ui'").fetchone()[0]
    )
    bad = counts["invalid_origin"] + counts["ui_without_editor"]
    if not bad:
        return CheckResult.ok(
            f"寫入來源一致（{counts['ui_origin']} 則 note 為 ui 來源）", counts=counts
        )
    return CheckResult.fail(
        f"{bad} 項寫入來源不一致", details=details[:MAX_DETAILS], counts=counts
    )
