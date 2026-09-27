"""doctor 通用框架：檢查項註冊、結果型別、結構化報告與 exit code 慣例（純標準庫）。

設計重點：
- 檢查函式簽名固定為 `(ctx: DoctorContext) -> CheckResult`，所需的設定與資源
  （db 連線等）一律從 context 取，不自己讀全域狀態——測試才能隔離。
- 檢查拋例外、回傳型別不對，一律記成 fail：對帳本身壞掉不可被當成通過，
  也不可讓整個 doctor 崩潰而看不到其他項目的結果。
- 缺少必要資源用 `ctx.require()` → 記成 skipped 並附原因，不靜默略過。
- exit code：有任何 fail 為 1，否則 0（warn、skipped 不影響）；
  參數錯誤由 argparse 給 2。
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

EXIT_OK = 0
EXIT_FAIL = 1


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CheckResult:
    """單一檢查項的結果。

    - `summary`：一行說明；fail／warn／skipped 必填（skipped 的 summary 即原因）
    - `details`：逐筆明細（例如每一筆違規）
    - `counts`：對帳用的計數（例如 `{"notes": 120, "fts_rows": 118}`）
    """

    status: Status
    summary: str = ""
    details: tuple[str, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, Status):
            object.__setattr__(self, "status", Status(self.status))
        if not isinstance(self.summary, str):
            raise TypeError(f"summary 必須是字串，得到 {type(self.summary).__name__}")
        if self.status is not Status.PASS and not self.summary.strip():
            raise ValueError(f"{self.status.value} 結果必須附 summary（原因）")
        if isinstance(self.details, str):
            raise TypeError("details 必須是字串序列，不可是單一字串")
        details = tuple(self.details)
        for item in details:
            if not isinstance(item, str):
                raise TypeError(f"details 每項必須是字串，得到 {type(item).__name__}")
        object.__setattr__(self, "details", details)
        counts = dict(self.counts)
        for key, value in counts.items():
            if not isinstance(key, str):
                raise TypeError(f"counts 的鍵必須是字串，得到 {key!r}")
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"counts[{key!r}] 必須是整數，得到 {value!r}")
        object.__setattr__(self, "counts", MappingProxyType(counts))

    # 便利建構子
    @classmethod
    def ok(
        cls,
        summary: str = "",
        *,
        details: Iterable[str] = (),
        counts: Mapping[str, int] | None = None,
    ) -> CheckResult:
        return cls(Status.PASS, summary, details, counts or {})  # type: ignore[arg-type]

    @classmethod
    def fail(
        cls,
        summary: str,
        *,
        details: Iterable[str] = (),
        counts: Mapping[str, int] | None = None,
    ) -> CheckResult:
        return cls(Status.FAIL, summary, details, counts or {})  # type: ignore[arg-type]

    @classmethod
    def warn(
        cls,
        summary: str,
        *,
        details: Iterable[str] = (),
        counts: Mapping[str, int] | None = None,
    ) -> CheckResult:
        return cls(Status.WARN, summary, details, counts or {})  # type: ignore[arg-type]

    @classmethod
    def skipped(cls, reason: str) -> CheckResult:
        return cls(Status.SKIPPED, reason)


class CheckSkipped(Exception):
    """檢查函式內拋出 → 該項記為 skipped，訊息即原因。"""


@dataclass(frozen=True)
class DoctorContext:
    """傳給每個檢查函式的環境。

    - `settings`：設定值（路徑、門檻等）
    - `resources`：執行期資源（例如 `"db"` → sqlite3 連線）；缺少時用 `require()`
    """

    settings: Mapping[str, Any] = field(default_factory=dict)
    resources: Mapping[str, Any] = field(default_factory=dict)

    def require(self, name: str) -> Any:
        """取資源；不存在或為 None 時拋 `CheckSkipped`，該項記為 skipped。"""
        value = self.resources.get(name)
        if value is None:
            raise CheckSkipped(f"缺少 context 資源：{name}")
        return value


CheckFunc = Callable[[DoctorContext], CheckResult]


@dataclass(frozen=True)
class Check:
    """一個已註冊的檢查項。`name` 全域唯一，慣例為 `<分類>.<項目>`。"""

    name: str
    category: str
    func: CheckFunc
    description: str = ""

    def __post_init__(self) -> None:
        for attr in ("name", "category"):
            value = getattr(self, attr)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Check.{attr} 不可為空")
        if not callable(self.func):
            raise TypeError(f"Check.func 必須可呼叫：{self.name}")


@dataclass(frozen=True)
class CheckOutcome:
    """執行後的一項：檢查項資訊 + 結果。"""

    name: str
    category: str
    description: str
    result: CheckResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "status": self.result.status.value,
            "summary": self.result.summary,
            "details": list(self.result.details),
            "counts": dict(self.result.counts),
        }


@dataclass(frozen=True)
class DoctorReport:
    outcomes: tuple[CheckOutcome, ...]

    def count(self, status: Status) -> int:
        return sum(1 for o in self.outcomes if o.result.status is status)

    @property
    def ok(self) -> bool:
        return self.count(Status.FAIL) == 0

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.ok else EXIT_FAIL

    def to_dict(self) -> dict[str, Any]:
        summary = {"total": len(self.outcomes)}
        summary.update({s.value: self.count(s) for s in Status})
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "summary": summary,
            "checks": [o.to_dict() for o in self.outcomes],
        }


def _run_one(check: Check, ctx: DoctorContext) -> CheckResult:
    try:
        result = check.func(ctx)
    except CheckSkipped as exc:
        return CheckResult.skipped(str(exc) or "檢查項自行略過（未附原因）")
    except Exception as exc:
        # 對帳本身壞掉：記成 fail，不讓 doctor 崩潰，也不當成通過
        return CheckResult.fail(
            f"檢查拋出例外：{type(exc).__name__}: {exc}",
            details=traceback.format_exc().rstrip().splitlines(),
        )
    if not isinstance(result, CheckResult):
        return CheckResult.fail(
            f"檢查回傳型別錯誤：預期 CheckResult，得到 {type(result).__name__}"
        )
    return result


class Registry:
    """檢查項註冊表。刻意不提供模組級單例：各處自行建立，測試互不干擾。"""

    def __init__(self, checks: Iterable[Check] = ()) -> None:
        self._checks: dict[str, Check] = {}
        for check in checks:
            self.add(check)

    def add(self, check: Check) -> Check:
        if check.name in self._checks:
            raise ValueError(f"檢查項名稱重複：{check.name}")
        self._checks[check.name] = check
        return check

    def register(
        self, name: str, category: str, description: str = ""
    ) -> Callable[[CheckFunc], CheckFunc]:
        """裝飾器形式：`@registry.register("storage.schema_version", "storage")`。"""

        def decorator(func: CheckFunc) -> CheckFunc:
            self.add(Check(name, category, func, description))
            return func

        return decorator

    @property
    def checks(self) -> tuple[Check, ...]:
        return tuple(self._checks.values())

    def __len__(self) -> int:
        return len(self._checks)

    def run(
        self,
        ctx: DoctorContext | None = None,
        *,
        categories: Sequence[str] | None = None,
    ) -> DoctorReport:
        """依註冊順序執行；`categories` 給定時只跑這些分類。"""
        ctx = ctx if ctx is not None else DoctorContext()
        selected = [
            c
            for c in self._checks.values()
            if not categories or c.category in categories
        ]
        return DoctorReport(
            tuple(
                CheckOutcome(c.name, c.category, c.description, _run_one(c, ctx))
                for c in selected
            )
        )
