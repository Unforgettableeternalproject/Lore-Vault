"""spike 接入的服務端對帳（純函式、純標準庫）：episode 收料新鮮度、自動建立的 vault。

與 `checks.py` 同樣回傳 `Reconciliation`，doctor 在 `builtin.py` 轉成 CheckResult。
收料時間用服務端寫入時的 `recorded`（不是 episode 自己的 started_at）：
要抓的是「某台機器的 spool 停止推送」，spool 晚推的舊 episode 也算有在收料。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .checks import MAX_DETAILS, Reconciliation
from .timeutil import parse_utc
from .vaults import AUTO_ORIGINS, VAULT_ORIGINS

DEFAULT_EPISODE_INGEST_MAX_AGE_HOURS = 48.0


def episode_ingest_recency(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    max_age_hours: float = DEFAULT_EPISODE_INGEST_MAX_AGE_HOURS,
) -> Reconciliation:
    """各 machine 的 episode 筆數與最近收料時間；任一台超過門檻或從未收料為 warn。

    只能看到「曾經推送過」的機器；從未推送的機器由客戶端 doctor 的 spool 對帳負責。
    """
    rows = conn.execute(
        """
        SELECT machine, count(*) AS n, max(recorded) AS last_recorded
        FROM episodes GROUP BY machine ORDER BY machine
        """
    ).fetchall()
    total = sum(int(r["n"]) for r in rows)
    stale: list[str] = []
    details: list[str] = []
    for r in rows:
        age_hours = (now - parse_utc(r["last_recorded"])).total_seconds() / 3600
        line = (
            f"{r['machine']}：{r['n']} 筆，最近收料 {r['last_recorded']}"
            f"（{age_hours:.1f} 小時前）"
        )
        if age_hours > max_age_hours:
            stale.append(r["machine"])
            line += " ⚠ 超過門檻"
        details.append(line)
    counts = {"episodes": total, "machines": len(rows), "stale_machines": len(stale)}
    if not rows:
        return Reconciliation(
            "warn", "尚無任何 episode 收料（spool 推送是否已啟用？）", counts
        )
    if stale:
        return Reconciliation(
            "warn",
            f"{len(stale)} 台機器超過 {max_age_hours:g} 小時沒有收料："
            + "、".join(stale[:MAX_DETAILS]),
            counts,
            tuple(details[:MAX_DETAILS]),
        )
    return Reconciliation(
        "pass",
        f"{len(rows)} 台機器、{total} 筆 episode，皆在 {max_age_hours:g} 小時內收料",
        counts,
        tuple(details[:MAX_DETAILS]),
    )


def auto_created_vaults(
    conn: sqlite3.Connection, *, warn_above: int | None = None
) -> Reconciliation:
    """自動建立（episode 收料／管線 global）的 vault 數，列出 key 與觸發來源供審視。

    預設只報數（pass）；`warn_above` 設定時，自動建立數超過它為 warn。
    """
    rows = conn.execute(
        "SELECT key, origin, origin_detail FROM vaults ORDER BY key"
    ).fetchall()
    counts = {f"origin_{o}": 0 for o in sorted(VAULT_ORIGINS)}
    auto: list[str] = []
    for r in rows:
        counts[f"origin_{r['origin']}"] = counts.get(f"origin_{r['origin']}", 0) + 1
        if r["origin"] in AUTO_ORIGINS:
            auto.append(f"{r['key']}（{r['origin']}）{r['origin_detail'] or ''}")
    counts["auto_created"] = len(auto)
    details = tuple(auto[:MAX_DETAILS])
    if warn_above is not None and len(auto) > warn_above:
        return Reconciliation(
            "warn",
            f"自動建立的 vault {len(auto)} 個，超過門檻 {warn_above}，請審視",
            counts,
            details,
        )
    return Reconciliation(
        "pass", f"自動建立的 vault {len(auto)} 個（共 {len(rows)} 個）", counts, details
    )
