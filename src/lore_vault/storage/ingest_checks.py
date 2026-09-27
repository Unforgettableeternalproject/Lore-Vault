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
from .vaults import (
    AUTO_ORIGINS,
    FOLDER_KEY_PREFIX,
    KIND_MISC,
    MISC_VAULT_KEY,
    ORIGIN_EPISODE,
    VAULT_ORIGINS,
)

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


def misc_routing(conn: sqlite3.Connection) -> Reconciliation:
    """雜項 vault（D14）的路由不變量。

    fail：
    - kind=misc 的 vault 超過一個，或其 key 不是 `misc`；key `misc` 的 kind 不是 misc
    - 雜項 vault 有別名，或任何 vault 以 `misc` 當別名（vault_resolve 會解析到雜項）
    - episode／injection 的 `origin_key` 與歸屬不一致：有 origin_key 卻不在雜項
      vault、在雜項 vault 卻沒有 origin_key，或 origin_key 不是 `folder/` 開頭
    warn：
    - 仍有 episode 收料自動建立的 `folder/*` vault（新規則下不會再產生；通常是 v16
      遷移因該 vault 有 note／文件／墓碑／別名而跳過的舊資料，需人工處理）
    資訊（counts）：雜項 episode 數、來源位置數、其中已正式註冊的位置數
    （`registered_origins`：收料當下路由正確、歸屬凍結，不自動搬移）。
    """
    problems: list[str] = []
    misc_rows = conn.execute(
        "SELECT key FROM vaults WHERE kind = ? ORDER BY key", (KIND_MISC,)
    ).fetchall()
    misc_keys = [r[0] for r in misc_rows]
    if len(misc_keys) > 1:
        problems.append(f"雜項 vault 超過一個：{misc_keys}")
    for key in misc_keys:
        if key != MISC_VAULT_KEY:
            problems.append(f"雜項 vault 的 key 不是 {MISC_VAULT_KEY!r}：{key!r}")
    row = conn.execute(
        "SELECT kind FROM vaults WHERE key = ?", (MISC_VAULT_KEY,)
    ).fetchone()
    if row is not None and row[0] != KIND_MISC:
        problems.append(f"key {MISC_VAULT_KEY!r} 被 kind={row[0]!r} 的 vault 佔用")
    aliases = conn.execute(
        """
        SELECT a.alias, a.vault FROM vault_aliases a JOIN vaults v ON v.key = a.vault
        WHERE v.kind = ? OR a.alias = ? ORDER BY a.alias
        """,
        (KIND_MISC, MISC_VAULT_KEY),
    ).fetchall()
    for r in aliases:
        problems.append(
            f"別名 {r[0]!r} → {r[1]!r}：雜項 vault 不可有別名、misc 不可當別名"
        )
    misc_clause = "(SELECT key FROM vaults WHERE kind = ?)"
    stray: list[sqlite3.Row] = []
    for table in ("episodes", "injections"):
        rows = conn.execute(
            f"""
            SELECT vault, origin_key, count(*) AS n FROM {table}
            WHERE (origin_key IS NOT NULL AND vault NOT IN {misc_clause})
               OR (vault IN {misc_clause}
                   AND (origin_key IS NULL OR substr(origin_key, 1, ?) != ?))
            GROUP BY vault, origin_key ORDER BY vault, origin_key
            """,
            (KIND_MISC, KIND_MISC, len(FOLDER_KEY_PREFIX), FOLDER_KEY_PREFIX),
        ).fetchall()
        stray.extend(rows)
        for r in rows:
            problems.append(
                f"{table} 歸屬與 origin_key 不一致：vault={r['vault']!r}、"
                f"origin_key={r['origin_key']!r}（{r['n']} 筆）"
            )
    leftovers = [
        r[0]
        for r in conn.execute(
            "SELECT key FROM vaults WHERE origin = ? AND kind = 'repo' "
            "AND substr(key, 1, ?) = ? ORDER BY key",
            (ORIGIN_EPISODE, len(FOLDER_KEY_PREFIX), FOLDER_KEY_PREFIX),
        )
    ]
    origins = conn.execute(
        f"""
        SELECT origin_key, count(*) AS n FROM episodes
        WHERE vault IN {misc_clause} AND origin_key IS NOT NULL
        GROUP BY origin_key ORDER BY origin_key
        """,
        (KIND_MISC,),
    ).fetchall()
    registered = [
        r["origin_key"]
        for r in origins
        if conn.execute(
            "SELECT 1 FROM vaults WHERE key = ? UNION ALL "
            "SELECT 1 FROM vault_aliases WHERE alias = ?",
            (r["origin_key"], r["origin_key"]),
        ).fetchone()
        is not None
    ]
    counts = {
        "misc_vaults": len(misc_keys),
        "misc_episodes": sum(int(r["n"]) for r in origins),
        "misc_origins": len(origins),
        "registered_origins": len(registered),
        "stray_episode_groups": len(stray),
        "folder_auto_vaults": len(leftovers),
    }
    details = tuple(
        [*problems, *(f"{r['origin_key']}：{r['n']} 筆" for r in origins)][:MAX_DETAILS]
    )
    if problems:
        return Reconciliation(
            "fail", f"雜項 vault 路由不變量破壞 {len(problems)} 項", counts, details
        )
    if leftovers:
        return Reconciliation(
            "warn",
            f"仍有 {len(leftovers)} 個收料自動建立的 folder vault（應已併入雜項）："
            + "、".join(leftovers[:MAX_DETAILS]),
            counts,
            details,
        )
    summary = (
        f"雜項 vault {counts['misc_episodes']} 筆 episode、"
        f"{counts['misc_origins']} 個來源位置"
    )
    if registered:
        summary += f"（其中 {len(registered)} 個已正式註冊，舊 episode 維持在雜項）"
    return Reconciliation("pass", summary, counts, details)
