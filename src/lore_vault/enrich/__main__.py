"""`python -m lore_vault.enrich --once`：跑一輪背景補算（供排程或測試）。"""

import sys

from .command import main

sys.exit(main())
