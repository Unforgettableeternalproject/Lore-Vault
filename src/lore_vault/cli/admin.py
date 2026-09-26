"""管理指令（不提供 MCP 工具）：`python -m lore_vault.cli.admin <子指令>`。

- `delete-note --space SPACE --vault KEY --id NOTE_ID [--reason TEXT]`：vault 在
  該 space 內解析（space 必填，與服務端相同無預設）
- `delete-vault --key KEY [--force] [--reason TEXT]`：vault 內有 note 或其他紀錄時
  必須 `--force`
- `undelete-note --id NOTE_ID`：移除墓碑，下次重跑匯入時該 note 會匯回來
- `set-space --key KEY --space SPACE [--new-key NEW]`：把 vault 換到另一個 space
  （A19／D-space-3 只走管理指令；A20 規則）。只允許 `lore`↔`personal`，dev 與非 dev
  兩個方向都拒絕。換 space 同時改 key：缺省把前綴換掉（`lore/x`→`personal/x`），
  別名一律換前綴（換不了就拒絕），舊 key 不留別名；新 key／新別名已存在則拒絕。
  所有引用該 key 的表在單一交易內改寫，執行後核對各表筆數與規劃一致，否則 rollback。
  換完後 MCP 殼的降級快照要等下次快照更新才反映

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
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from lore_vault.schema import SPACES
from lore_vault.storage import admin
from lore_vault.storage.db import connect
from lore_vault.storage.errors import StorageError
from lore_vault.storage.migrate import SCHEMA_VERSION, current_version


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

    p_space = sub.add_parser("set-space", help="把 vault 換到另一個 space")
    p_space.add_argument("--key", required=True, help="vault 正式 key（不接受別名）")
    p_space.add_argument(
        "--space", required=True, choices=sorted(SPACES), help="目標 space"
    )
    p_space.add_argument(
        "--new-key", help="新 key（缺省把 '<舊 space>/' 前綴換成 '<新 space>/'）"
    )
    p_space.add_argument("--yes", action="store_true", help="真的變更（預設 dry-run）")

    p_undel = sub.add_parser("undelete-note", help="移除墓碑，讓下次匯入可匯回")
    p_undel.add_argument("--id", required=True, dest="note_id", help="note id")
    p_undel.add_argument("--yes", action="store_true", help="真的移除（預設 dry-run）")
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
        if args.command == "undelete-note":
            if args.yes:
                grave = admin.undelete_note(conn, args.note_id)
            else:
                grave = admin.find_tombstone(conn, args.note_id)
            result = {"mode": "undeleted" if args.yes else "dry_run", **grave}
            if not args.yes:
                result["hint"] = "確認無誤後加 --yes 執行"
            out.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.command == "set-space":
            changed = _set_space(conn, args)
            out.write(json.dumps(changed, ensure_ascii=False, indent=2) + "\n")
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
    except StorageError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
