"""SQLite 備份（T-27）：`VACUUM INTO` 到備份目錄，驗證後才落地，保留最近 N 份。

流程（`backup_database`）：
1. 以唯讀連線開來源 DB（不遷移、不寫 live DB），`VACUUM INTO` 到同目錄的暫存名
2. 唯讀開啟暫存檔：`integrity_check` 必須為 ok、`user_version` 必須等於來源
3. 關閉驗證連線後 `os.replace` 成正式檔名（檔名含 UTC 時間，字串排序 = 時間排序）
4. 寫 sidecar `last_backup.json`（同樣暫存名 + rename），再刪掉超出保留數的舊備份

任何一步失敗都刪掉暫存檔並拋 `BackupError`，不會留下半檔、也不更新 sidecar。
最近一次備份時間記在 sidecar 而非 live DB：備份流程不寫它正在備份的資料庫，
也不佔用 schema 遷移版本號。doctor 讀 sidecar 並確認引用的備份檔仍在
（`backup_freshness`）。

CLI：`python -m lore_vault.storage.backup [--config PATH] [--db PATH] [--dest DIR]
[--keep N]`；`--db`／`--dest`／`--keep` 缺省時走設定 `database.path`、`backup.dir`、
`backup.keep`（容器內以環境變數 `LORE_VAULT_DATABASE_PATH`、`LORE_VAULT_BACKUP_DIR`
覆寫）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from typing import TextIO

from lore_vault.schema import SchemaError

from .checks import Reconciliation
from .db import connect_readonly
from .errors import StorageError
from .migrate import SCHEMA_VERSION, current_version
from .timeutil import format_utc, parse_utc

SIDECAR_NAME = "last_backup.json"
# 正式備份檔名：lore-20260926T031500123Z.db（毫秒精度、UTC）
BACKUP_NAME_RE = re.compile(r"^lore-\d{8}T\d{9}Z\.db$")
_PARTIAL_SUFFIX = ".partial"


class BackupError(StorageError):
    """備份失敗（來源、寫出或驗證）；失敗時不留下半檔。"""


@dataclass(frozen=True)
class BackupRecord:
    """一次成功備份的紀錄（sidecar 內容）。"""

    created: str
    file: str
    bytes: int
    schema_version: int
    expected_schema_version: int
    integrity: str


def backup_file_name(moment: datetime) -> str:
    stamp = format_utc(moment)  # 2026-09-26T03:15:00.123Z
    digits = stamp.replace("-", "").replace(":", "").replace(".", "")
    return f"lore-{digits}.db"


def verify_backup(path: str | PathLike[str], *, expected_version: int) -> str:
    """唯讀開啟備份檔，驗證 integrity_check 與 schema 版本；回傳 integrity 結果。"""
    target = Path(path)
    try:
        conn = sqlite3.connect(
            f"{target.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None
        )
    except sqlite3.Error as exc:
        raise BackupError(f"備份檔無法開啟：{target.name}（{exc}）") from None
    try:
        try:
            rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            version = current_version(conn)
        except sqlite3.DatabaseError as exc:
            raise BackupError(
                f"備份檔不是有效的資料庫：{target.name}（{exc}）"
            ) from None
    finally:
        conn.close()
    if rows != ["ok"]:
        raise BackupError(f"備份檔 integrity_check 失敗：{rows[:5]}")
    if version != expected_version:
        raise BackupError(
            f"備份檔 schema 版本 {version} 與來源 {expected_version} 不符"
        )
    return "ok"


def _write_sidecar(dest: Path, record: BackupRecord) -> None:
    tmp = dest / f".{SIDECAR_NAME}{_PARTIAL_SUFFIX}"
    try:
        tmp.write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, dest / SIDECAR_NAME)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def prune_backups(dest: str | PathLike[str], *, keep: int) -> list[str]:
    """只保留最新 `keep` 份正式備份；只動符合備份檔名格式的檔案。回傳刪除清單。"""
    if keep < 1:
        raise BackupError("keep 必須 ≥ 1")
    names = sorted(
        p.name
        for p in Path(dest).iterdir()
        if p.is_file() and BACKUP_NAME_RE.match(p.name)
    )
    removed = names[:-keep]
    for name in removed:
        (Path(dest) / name).unlink()
    return removed


def backup_database(
    db_path: str | PathLike[str],
    dest_dir: str | PathLike[str],
    *,
    keep: int,
    now: datetime | None = None,
) -> tuple[BackupRecord, list[str]]:
    """備份一次；回傳（紀錄, 被清掉的舊備份檔名）。"""
    if keep < 1:
        raise BackupError("keep 必須 ≥ 1")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    moment = now if now is not None else datetime.now(UTC)
    name = backup_file_name(moment)
    final = dest / name
    if final.exists():
        raise BackupError(f"備份檔已存在，不覆寫：{final}")
    tmp = dest / f".{name}{_PARTIAL_SUFFIX}"
    tmp.unlink(missing_ok=True)  # VACUUM INTO 拒絕寫入已存在的檔案

    try:
        source = connect_readonly(db_path)
    except sqlite3.Error as exc:
        raise BackupError(f"來源資料庫無法開啟：{exc}") from None
    try:
        try:
            source_version = current_version(source)
            source.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            source.close()
        integrity = verify_backup(tmp, expected_version=source_version)
        os.replace(tmp, final)
    except sqlite3.Error as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"VACUUM INTO 失敗：{exc}") from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    record = BackupRecord(
        created=format_utc(moment),
        file=name,
        bytes=final.stat().st_size,
        schema_version=source_version,
        expected_schema_version=SCHEMA_VERSION,
        integrity=integrity,
    )
    _write_sidecar(dest, record)
    return record, prune_backups(dest, keep=keep)


def backup_freshness(
    dest_dir: str | PathLike[str], *, now: datetime, max_age_seconds: float
) -> Reconciliation:
    """doctor 對帳：最近一次備份是否在門檻內、引用的備份檔是否仍在。

    從未備份（沒有 sidecar）、sidecar 損毀、檔案已不在、超過門檻一律 fail。
    """
    dest = Path(dest_dir)
    sidecar = dest / SIDECAR_NAME
    if not sidecar.is_file():
        return Reconciliation("fail", f"從未備份（{dest} 沒有 {SIDECAR_NAME}）")
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        created = parse_utc(data["created"])
        file_name = str(data["file"])
    except (OSError, ValueError, KeyError, TypeError, SchemaError) as exc:
        return Reconciliation("fail", f"{SIDECAR_NAME} 無法解析：{exc}")
    if not BACKUP_NAME_RE.match(file_name):
        return Reconciliation("fail", f"{SIDECAR_NAME} 的檔名不合格式：{file_name!r}")
    age = max(0.0, (now - created).total_seconds())
    counts = {"age_seconds": int(age), "max_age_seconds": int(max_age_seconds)}
    details = (f"最近一次：{data['created']}（{file_name}）",)
    if not (dest / file_name).is_file():
        return Reconciliation(
            "fail", f"sidecar 引用的備份檔不存在：{file_name}", counts, details
        )
    if age > max_age_seconds:
        hours = age / 3600
        return Reconciliation(
            "fail",
            f"最近一次備份在 {hours:.1f} 小時前，超過門檻 "
            f"{max_age_seconds / 3600:g} 小時",
            counts,
            details,
        )
    return Reconciliation(
        "pass", f"最近一次備份在 {age / 3600:.1f} 小時前", counts, details
    )


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    from lore_vault.config import ConfigError, load_config

    parser = argparse.ArgumentParser(prog="python -m lore_vault.storage.backup")
    parser.add_argument("--config", help="設定檔路徑（TOML）")
    parser.add_argument("--db", help="來源資料庫路徑（覆寫 database.path）")
    parser.add_argument("--dest", help="備份目錄（覆寫 backup.dir）")
    parser.add_argument(
        "--keep", type=int, default=None, help="保留幾份（覆寫 backup.keep）"
    )
    args = parser.parse_args(argv)
    out = stdout if stdout is not None else sys.stdout

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        return 2
    db_path = args.db or config.database.path
    dest = args.dest or config.backup.dir
    keep = args.keep if args.keep is not None else config.backup.keep
    if not db_path:
        print("缺少資料庫路徑：用 --db 或設定 database.path", file=sys.stderr)
        return 2
    if not dest:
        print("缺少備份目錄：用 --dest 或設定 backup.dir", file=sys.stderr)
        return 2
    if keep < 1:
        parser.error("--keep 必須 ≥ 1")
    try:
        record, removed = backup_database(db_path, dest, keep=keep)
    except (StorageError, OSError) as exc:
        print(f"備份失敗：{exc}", file=sys.stderr)
        return 1
    payload = {**asdict(record), "pruned": removed}
    print(json.dumps(payload, ensure_ascii=False), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
