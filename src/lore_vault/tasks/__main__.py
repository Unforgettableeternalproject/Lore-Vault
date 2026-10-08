import sys

from .cli import main

if hasattr(sys.stdout, "reconfigure"):
    # Windows 主控台預設 cp950，中文表格與路徑一律以 UTF-8 輸出
    sys.stdout.reconfigure(encoding="utf-8")

raise SystemExit(main())
