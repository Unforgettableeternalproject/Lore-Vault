"""E2E 專用：在臨時資料庫預先建立 UI 帳號（A23）。

只給 Playwright 的 webServer 在啟動服務前呼叫；資料庫路徑與帳密取自 playwright.config.ts
設定的環境變數（測試專用固定值，不是任何正式環境的密碼）。正式環境一律用
`python -m lore_vault.cli.admin ui-set-password` 互動設定，不走這支腳本。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from lore_vault.storage import ui_login
from lore_vault.storage.db import connect


def main() -> None:
    conn = connect(os.environ["LORE_VAULT_DATABASE_PATH"])
    try:
        ui_login.set_password(
            conn,
            os.environ["E2E_UI_USER"],
            os.environ["E2E_UI_PASSWORD"],
            now=datetime.now(UTC),
            display=os.environ["E2E_UI_DISPLAY"],
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
