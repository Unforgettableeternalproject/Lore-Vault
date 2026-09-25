"""`python -m lore_vault.doctor`：執行所有內建檢查項，有 fail 時 exit code 為 1。"""

import sys

from .command import main

sys.exit(main())
