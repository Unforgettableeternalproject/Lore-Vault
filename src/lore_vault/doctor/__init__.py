"""服務層：全資料流對帳。

框架 API 見 `framework.py`；內建檢查項清單見 `builtin.default_registry()`；
CLI：`python -m lore_vault.doctor [--json]`。
"""

from .builtin import default_registry
from .framework import (
    EXIT_FAIL,
    EXIT_OK,
    Check,
    CheckFunc,
    CheckOutcome,
    CheckResult,
    CheckSkipped,
    DoctorContext,
    DoctorReport,
    Registry,
    Status,
)

__all__ = [
    "EXIT_FAIL",
    "EXIT_OK",
    "Check",
    "CheckFunc",
    "CheckOutcome",
    "CheckResult",
    "CheckSkipped",
    "DoctorContext",
    "DoctorReport",
    "Registry",
    "Status",
    "default_registry",
]
