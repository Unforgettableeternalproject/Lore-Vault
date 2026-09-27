"""D13：服務關閉 episode 收料（403 `episode_ingest_disabled`）時，客戶端 spool 的行為。

- 檔案留在 pending：不移到 rejected、不標損毀、不刪
- `last_error_kind = "disabled"`、退避拉長到小時級（Stop hook 不會每輪都打服務）
- doctor `spool.pending` 為 warn（說明服務未開啟），不因年齡升成 fail
- 服務開啟後 `--push`（push_all）補推成功
- 與暫時錯誤（503／500）分得開：那些仍是 60 秒退避
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.hooks import spool

from .fake_service import FakeService
from .test_hooks_client import TOKEN, _settings, _spool_n, _write_client_env

DISABLED = (
    403,
    {
        "error": {
            "code": "episode_ingest_disabled",
            "message": "服務未開啟 episode 收料（設定 episodes.ingest）",
        }
    },
    {},
)


def _pending(report) -> object:
    return next(o.result for o in report.outcomes if o.name == "spool.pending")


def test_disabled_keeps_pending_and_backs_off_for_hours(tmp_path):
    _spool_n(tmp_path, 3)
    with FakeService(lambda *a: DISABLED) as svc:
        settings = _settings(svc.url)
        started = datetime.now(UTC)
        result = spool.push_pending(tmp_path, settings)
        assert result.kept == 3 and result.rejected == 0
        assert "未開啟 episode 收料" in (result.error or "")
        # 退避期間 Stop hook 不再打服務
        assert spool.push_pending(tmp_path, settings).skipped_reason
        assert len(svc.requests) == 1
    stats = spool.spool_stats(tmp_path)
    assert (stats.pending, stats.rejected) == (3, 0)
    state = spool.load_push_state(tmp_path)
    assert state["last_error_kind"] == spool.ERROR_KIND_DISABLED
    wait = state["backoff_until_ts"] - started.timestamp()
    assert wait >= spool.DISABLED_BACKOFF_SECONDS - 5
    assert TOKEN not in str(state)


def test_temporary_errors_keep_short_backoff(tmp_path):
    _spool_n(tmp_path, 1)
    # 其他 403（例如 Cloudflare Access，沒有服務的錯誤格式）不算收料關閉
    with FakeService(lambda *a: (403, b"<html>denied</html>", {})) as svc:
        started = datetime.now(UTC)
        spool.push_pending(tmp_path, _settings(svc.url))
    state = spool.load_push_state(tmp_path)
    assert state["last_error_kind"] == "rejected"
    assert state["backoff_until_ts"] - started.timestamp() <= 61
    assert "CF_ACCESS" in state["last_error"]


def test_service_error_message_used_for_own_403(tmp_path):
    from lore_vault.hooks.service import _rejected_detail

    detail = _rejected_detail(403, DISABLED[1])
    assert "episode_ingest_disabled" in detail and "CF_ACCESS" not in detail
    assert "CF_ACCESS" in _rejected_detail(403, None)


def test_doctor_warns_not_fails_while_ingest_disabled(tmp_path):
    spool_dir = tmp_path / "spool"
    _write_client_env(tmp_path / "client.env", "http://127.0.0.1:1")
    _spool_n(spool_dir, 2)
    with FakeService(lambda *a: DISABLED) as svc:
        spool.push_pending(spool_dir, _settings(svc.url))
    # 兩天後仍只是 warn：資料安全留在本機，是服務端的設定
    later = datetime.now(UTC) + timedelta(hours=48)
    report = default_registry().run(
        DoctorContext(settings={"spool_dir": str(spool_dir), "now": later}),
        categories=["spool"],
    )
    result = _pending(report)
    assert result.status is Status.WARN
    assert "未開啟 episode 收料" in result.summary
    conflicts = next(o.result for o in report.outcomes if o.name == "spool.conflicts")
    assert conflicts.status is Status.PASS

    # 服務開啟後手動補推清空 → pass
    with FakeService() as fixed:
        total = spool.push_all(spool_dir, _settings(fixed.url))
    assert total.accepted == 2
    report = default_registry().run(
        DoctorContext(settings={"spool_dir": str(spool_dir), "now": later}),
        categories=["spool"],
    )
    assert _pending(report).status is Status.PASS
