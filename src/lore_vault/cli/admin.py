"""管理指令（不提供 MCP 工具）：`python -m lore_vault.cli.admin <子指令>`。

- `delete-note --space SPACE --vault KEY --id NOTE_ID [--reason TEXT]`：vault 在
  該 space 內解析（space 必填，與服務端相同無預設）
- `delete-vault --key KEY [--force] [--reason TEXT]`：vault 內有 note、文件或其他
  紀錄時必須 `--force`
- `delete-document --space SPACE --vault KEY --id DOC_ID [--reason TEXT]`：刪一份
  文件（chunk、FTS、向量、抽取紀錄），寫文件墓碑；blob 不刪（沒人引用時 doctor
  `documents.orphan_blobs` 回報）。不提供 undelete：要恢復就重新上傳
- `undelete-note --id NOTE_ID`：取消刪除。墓碑有內容快照（schema v12 起刪除的）時以
  原 id 與原內容還原（FTS 同交易重建，向量由服務的背景補算重算）；所屬 vault 已刪除
  則拒絕（先重建 vault）。v12 前的舊墓碑只移除墓碑，下次重跑匯入時該 note 會匯回來
- `set-space --key KEY --space SPACE [--new-key NEW]`：把 vault 換到另一個 space
  （A19／D-space-3 只走管理指令；A20 規則）。只允許 `lore`↔`personal`，dev 與非 dev
  兩個方向都拒絕。換 space 同時改 key：缺省把前綴換掉（`lore/x`→`personal/x`），
  別名一律換前綴（換不了就拒絕），舊 key 不留別名；新 key／新別名已存在則拒絕。
  所有引用該 key 的表在單一交易內改寫，執行後核對各表筆數與規劃一致，否則 rollback。
  換完後 MCP 殼的降級快照要等下次快照更新才反映
- `gc-blobs [--blob-dir DIR] [--min-age-hours N] [--yes]`：清理沒有任何 documents 列
  引用的孤兒 blob（判定與 doctor `documents.orphan_blobs` 共用）與中斷遺留的暫存檔。
  只刪 mtime 超過 N 小時（預設 1）的孤兒；刪前持 DB 寫鎖再確認無引用、檔案未變動；
  不符佈局的檔案只報告不碰；刪完移除空的子目錄。dry-run 只列雜湊前 12 碼與位元組
- `purge-tombstones --older-than-days N [--kinds note,document] [--yes]`：永久清除刪除
  時間早於 N 天前的墓碑（note 墓碑連同內容快照）。**清除後無法還原**
  （undelete 回 404），也**不再擋重跑匯入**（被刪的匯入 note 會匯回來；
  其對帳清單列一併移除、來源筆數減一，見 `storage.admin`）。文件原始檔 blob
  不在這裡刪，之後由 `gc-blobs` 回收。
  dry-run 只列筆數、快照位元組與最舊／最新刪除時間。不做自動清除
- `ui-set-password --user NAME [--display TEXT]`：建立 UI 帳號或更新密碼（A23）。
  密碼**只**以 getpass 互動輸入兩次（至少 12 字元），不接受參數、環境變數或 pipe
  （stdin 不是終端機就拒絕；容器內用 `docker exec -it`）。不動鎖定狀態
- `ui-lock-status`：鎖定狀態、目前失敗次數、剩餘次數與帳號清單（不含雜湊）
- `ui-login-log [--limit N]`：最近的登入紀錄（新到舊；不含密碼）
- `ui-unlock [--yes]`：人工解鎖並歸零失敗計數，寫一筆 unlock 紀錄（預設 dry-run）

刪除會為每則被刪的 note 寫墓碑（`note_tombstones`）：重跑匯入不會匯回，
匯入對帳把它算成「刻意刪除」而非漏匯。

預設 dry-run：只印將刪／將改內容的 metadata（筆數、id；不印標題與內文）。
加 `--yes` 才真的執行，在單一交易內完成（見 `lore_vault.storage.admin`）。
資料庫路徑缺省走設定 `database.path`
（容器內 `LORE_VAULT_CONFIG` 已指向 /data/lore.db）。
不遷移資料庫：schema 版本與程式不符時拒絕執行（先讓服務啟動遷移）。

exit code：0 成功（含 dry-run）；1 找不到、需要 --force、schema 不符等；2 參數錯誤。
"""

from __future__ import annotations

import argparse
import getpass
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from lore_vault.schema import SPACES
from lore_vault.storage import admin, ui_login
from lore_vault.storage import blobs as storage_blobs
from lore_vault.storage.db import connect
from lore_vault.storage.errors import StorageError
from lore_vault.storage.migrate import SCHEMA_VERSION, current_version

DEFAULT_GC_MIN_AGE_HOURS = 1.0


def _non_negative_hours(value: str) -> float:
    try:
        hours = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"不是數字：{value}") from None
    if not hours >= 0 or hours == float("inf"):
        raise argparse.ArgumentTypeError(f"必須是非負有限數：{value}")
    return hours


def _non_negative_days(value: str) -> float:
    return _non_negative_hours(value)


def _purge_kinds(value: str) -> list[str]:
    kinds = [k.strip() for k in value.split(",") if k.strip()]
    unknown = sorted(set(kinds) - set(admin.PURGE_KINDS))
    if not kinds or unknown:
        raise argparse.ArgumentTypeError(
            f"kinds 必須是 {','.join(admin.PURGE_KINDS)} 的非空子集，得到 {value!r}"
        )
    return kinds


PURGE_WARNING = (
    "清除後無法還原（undelete 會回 404 not_found）；被清除的匯入 note 不再擋重跑匯入，"
    "重跑 import_on 會把它匯回來（其對帳清單列一併移除、來源筆數減一）；"
    "文件原始檔 blob 不在此刪除，之後以 gc-blobs 回收"
)


def _purge(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, object]:
    if args.yes:
        plan, done = admin.purge_tombstones(
            conn, args.older_than_days, kinds=args.kinds
        )
        return {
            "mode": "purged",
            **plan.to_dict(),
            "purged": done,
            "warning": PURGE_WARNING,
        }
    plan = admin.plan_tombstone_purge(conn, args.older_than_days, kinds=args.kinds)
    return {
        "mode": "dry_run",
        **plan.to_dict(),
        "warning": PURGE_WARNING,
        "hint": "確認無誤後加 --yes 執行（不可逆）",
    }


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必須是正整數，得到 {value!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"必須是正整數，得到 {value!r}")
    return number


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _read_new_password() -> str:
    """互動輸入兩次；不是終端機（pipe、agent 代跑）一律拒絕。"""
    if not _stdin_is_tty():
        raise ui_login.AccountError(
            "密碼只能在互動終端機輸入（容器內用 docker exec -it）；"
            "不接受 pipe、參數或環境變數"
        )
    first = getpass.getpass("新密碼：")
    ui_login.validate_password(first)
    second = getpass.getpass("再輸入一次：")
    if first != second:
        raise ui_login.AccountError("兩次輸入的密碼不一致")
    return first


def _ui_command(
    conn: sqlite3.Connection, args: argparse.Namespace
) -> dict[str, object]:
    now = datetime.now(UTC)
    if args.command == "ui-set-password":
        ui_login.validate_username(args.user)
        if args.display is not None:
            ui_login.validate_display(args.display)
        password = _read_new_password()
        account, created = ui_login.set_password(
            conn, args.user, password, now=now, display=args.display
        )
        return {"mode": "created" if created else "updated", **account.to_dict()}
    if args.command == "ui-lock-status":
        return {
            **ui_login.lock_status(conn, now).to_dict(),
            "accounts": [a.to_dict() for a in ui_login.list_accounts(conn)],
        }
    if args.command == "ui-login-log":
        return {"items": ui_login.login_log(conn, limit=args.limit)}
    # ui-unlock
    if args.yes:
        return {"mode": "unlocked", **ui_login.unlock(conn, now=now)}
    return {
        "mode": "dry_run",
        **ui_login.lock_status(conn, now).to_dict(),
        "hint": "確認無誤後加 --yes 解鎖（同時歸零失敗計數並寫一筆 unlock 紀錄）",
    }


UI_COMMANDS = frozenset(
    {"ui-set-password", "ui-lock-status", "ui-login-log", "ui-unlock"}
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lore_vault.cli.admin")
    parser.add_argument("--db", help="資料庫路徑（覆寫 database.path）")
    parser.add_argument("--config", help="設定檔（缺省走 LORE_VAULT_CONFIG）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_note = sub.add_parser("delete-note", help="刪除單則 note")
    p_note.add_argument(
        "--space", required=True, choices=sorted(SPACES), help="vault 所屬 space"
    )
    p_note.add_argument("--vault", required=True, help="vault key 或別名")
    p_note.add_argument("--id", required=True, dest="note_id", help="note id")
    p_note.add_argument("--reason", default=admin.DEFAULT_NOTE_REASON, help="刪除原因")
    p_note.add_argument("--yes", action="store_true", help="真的刪除（預設 dry-run）")

    p_vault = sub.add_parser("delete-vault", help="刪除整個 vault")
    p_vault.add_argument("--key", required=True, help="vault 正式 key（不接受別名）")
    p_vault.add_argument(
        "--force", action="store_true", help="vault 內有 note 或其他紀錄時仍刪除"
    )
    p_vault.add_argument(
        "--reason", default=admin.DEFAULT_VAULT_REASON, help="刪除原因"
    )
    p_vault.add_argument("--yes", action="store_true", help="真的刪除（預設 dry-run）")

    p_doc = sub.add_parser("delete-document", help="刪除單份文件")
    p_doc.add_argument(
        "--space", required=True, choices=sorted(SPACES), help="vault 所屬 space"
    )
    p_doc.add_argument("--vault", required=True, help="vault key 或別名")
    p_doc.add_argument(
        "--id", required=True, dest="document_id", help="文件 id（doc:…）"
    )
    p_doc.add_argument(
        "--reason", default=admin.DEFAULT_DOCUMENT_REASON, help="刪除原因"
    )
    p_doc.add_argument("--yes", action="store_true", help="真的刪除（預設 dry-run）")

    p_space = sub.add_parser("set-space", help="把 vault 換到另一個 space")
    p_space.add_argument("--key", required=True, help="vault 正式 key（不接受別名）")
    p_space.add_argument(
        "--space", required=True, choices=sorted(SPACES), help="目標 space"
    )
    p_space.add_argument(
        "--new-key", help="新 key（缺省把 '<舊 space>/' 前綴換成 '<新 space>/'）"
    )
    p_space.add_argument("--yes", action="store_true", help="真的變更（預設 dry-run）")

    p_gc = sub.add_parser("gc-blobs", help="清理孤兒 blob 與遺留暫存檔")
    p_gc.add_argument("--blob-dir", help="blob 目錄（覆寫 documents.blob_dir）")
    p_gc.add_argument(
        "--min-age-hours",
        type=_non_negative_hours,
        default=DEFAULT_GC_MIN_AGE_HOURS,
        help="只刪 mtime 超過這個時數的孤兒（預設 1；避免刪到 DB 交易未提交的新 blob）",
    )
    p_gc.add_argument("--yes", action="store_true", help="真的刪除（預設 dry-run）")

    p_purge = sub.add_parser(
        "purge-tombstones", help="永久清除舊墓碑（清除後無法還原、不再擋重新匯入）"
    )
    p_purge.add_argument(
        "--older-than-days",
        required=True,
        type=_non_negative_days,
        help="只清除刪除時間早於 N 天前的墓碑（0 = 全部）",
    )
    p_purge.add_argument(
        "--kinds",
        type=_purge_kinds,
        default=None,
        help="逗號分隔：note、document（預設兩者）",
    )
    p_purge.add_argument("--yes", action="store_true", help="真的清除（預設 dry-run）")

    p_undel = sub.add_parser(
        "undelete-note", help="取消刪除（有快照則還原內容，舊墓碑只移除墓碑）"
    )
    p_undel.add_argument("--id", required=True, dest="note_id", help="note id")
    p_undel.add_argument("--yes", action="store_true", help="真的移除（預設 dry-run）")
    p_pw = sub.add_parser(
        "ui-set-password",
        help="建立 UI 帳號或更新密碼（密碼只以互動方式輸入）",
    )
    p_pw.add_argument(
        "--user",
        required=True,
        help="帳號（即 principal，如 UEPBernie；比對不分大小寫）",
    )
    p_pw.add_argument(
        "--display", default=None, help="顯示名稱（前端署名，如 'Xavier (Bernie)'）"
    )
    sub.add_parser("ui-lock-status", help="UI 登入鎖定狀態與帳號清單")
    p_log = sub.add_parser("ui-login-log", help="最近的 UI 登入紀錄")
    p_log.add_argument(
        "--limit", type=_positive_int, default=50, help="筆數（預設 50）"
    )
    p_unlock = sub.add_parser("ui-unlock", help="人工解鎖 UI 登入並歸零失敗計數")
    p_unlock.add_argument("--yes", action="store_true", help="真的解鎖（預設 dry-run）")
    return parser


def _db_path(args: argparse.Namespace) -> str:
    if args.db:
        return args.db
    from lore_vault.config import load_config

    path = load_config(args.config).database.path
    if not path:
        raise StorageError("缺少資料庫路徑：用 --db 或設定 database.path")
    return path


def _existing(path: str) -> str:
    # connect() 會建新檔；路徑打錯時不可默默建出空資料庫
    if not Path(path).is_file():
        raise StorageError(f"資料庫檔案不存在：{path}")
    return path


def _blob_store(args: argparse.Namespace) -> storage_blobs.BlobStore:
    value = args.blob_dir
    if not value:
        from lore_vault.config import load_config

        value = load_config(args.config).documents.blob_dir
    if not value:
        raise StorageError("缺少 blob 目錄：用 --blob-dir 或設定 documents.blob_dir")
    store = storage_blobs.BlobStore(value)
    if not store.root.is_dir():
        raise StorageError(f"blob 目錄不存在：{store.root}")
    return store


def _gc_blobs(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, object]:
    store = _blob_store(args)
    plan = storage_blobs.plan_gc(conn, store, min_age_seconds=args.min_age_hours * 3600)
    result: dict[str, object] = {
        "mode": "deleted" if args.yes else "dry_run",
        "blob_dir": str(store.root),
        **plan.to_dict(store.root),
    }
    if args.yes:
        result.update(storage_blobs.execute_gc(conn, store, plan).to_dict())
    else:
        result["hint"] = "確認無誤後加 --yes 執行；不符佈局的檔案不會被刪，需人工處理"
    return result


def _set_space(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, object]:
    if args.yes:
        plan = admin.change_vault_space(
            conn, args.key, args.space, new_key=args.new_key
        )
        return {"mode": "changed", **plan.to_dict()}
    # dry-run 跑完整規劃：A20 拒絕、前綴、衝突在這裡就報錯，不等 --yes
    plan = admin.plan_space_change(conn, args.key, args.space, new_key=args.new_key)
    return {
        "mode": "dry_run",
        **plan.to_dict(),
        "hint": "確認無誤後加 --yes 執行；舊 key 不留別名，引用舊 key 的外部設定需自行"
        "更新；MCP 殼的降級快照要等下次快照更新才反映",
    }


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    from lore_vault.config import ConfigError

    out = stdout or sys.stdout
    args = _parser().parse_args(argv)
    try:
        conn = connect(_existing(_db_path(args)), run_migrations=False)
    except (ConfigError, StorageError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    try:
        version = current_version(conn)
        if version != SCHEMA_VERSION:
            print(
                f"錯誤：資料庫 schema 版本 {version} 與程式 {SCHEMA_VERSION} 不符；"
                "先讓服務啟動完成遷移",
                file=sys.stderr,
            )
            return 1
        if args.command in UI_COMMANDS:
            ui_result = _ui_command(conn, args)
            out.write(json.dumps(ui_result, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.command == "undelete-note":
            if args.yes:
                restored = admin.restore_note(conn, args.note_id)
                grave = {**restored["tombstone"], "restored": restored["restored"]}
            else:
                grave = admin.find_tombstone(conn, args.note_id)
            result = {"mode": "undeleted" if args.yes else "dry_run", **grave}
            if not args.yes:
                result["hint"] = "確認無誤後加 --yes 執行"
            out.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.command == "purge-tombstones":
            purged = _purge(conn, args)
            out.write(json.dumps(purged, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.command == "gc-blobs":
            gc = _gc_blobs(conn, args)
            out.write(json.dumps(gc, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.command == "set-space":
            changed = _set_space(conn, args)
            out.write(json.dumps(changed, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.command == "delete-document":
            if args.yes:
                doc_plan = admin.delete_document(
                    conn,
                    args.vault,
                    args.document_id,
                    space=args.space,
                    reason=args.reason,
                )
            else:
                doc_plan = admin.plan_document_deletion(
                    conn, args.vault, args.document_id, space=args.space
                )
            doc_result = {
                "mode": "deleted" if args.yes else "dry_run",
                **doc_plan.to_dict(),
            }
            if not args.yes:
                doc_result["hint"] = "確認無誤後加 --yes 執行"
            out.write(json.dumps(doc_result, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.command == "delete-note":
            if args.yes:
                plan = admin.delete_note(
                    conn,
                    args.vault,
                    args.note_id,
                    space=args.space,
                    reason=args.reason,
                )
            else:
                plan = admin.plan_note_deletion(
                    conn, args.vault, args.note_id, space=args.space
                )
        else:
            if args.yes:
                plan = admin.delete_vault(
                    conn, args.key, force=args.force, reason=args.reason
                )
            else:
                plan = admin.plan_vault_deletion(conn, args.key)
        result = {"mode": "deleted" if args.yes else "dry_run", **plan.to_dict()}
        if not args.yes:
            if plan.requires_force and not args.force:
                result["hint"] = "vault 內有資料：確定要刪請加 --force --yes"
            else:
                result["hint"] = "確認無誤後加 --yes 執行"
        out.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0
    except (ConfigError, StorageError, ui_login.AccountError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
