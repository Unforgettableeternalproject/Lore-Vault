"""補算每輪進度 log（docker logs 監控用）。"""

from lore_vault.api.background import _has_activity, _progress_line


def test_progress_line_reports_done_and_pending():
    stats = {
        "summary": {"done": 3, "retry": 1, "gave_up": 0},
        "embedding": {"done": 2, "retry": 0, "gave_up": 1},
    }
    line = _progress_line(stats, (10, 7))
    assert "摘要 完成 3／重試 1／放棄 0" in line
    assert "向量 完成 2／重試 0／放棄 1" in line
    assert "剩餘 摘要 10、向量 7" in line


def test_idle_round_is_not_logged():
    idle = {
        "summary": {"done": 0, "retry": 0, "gave_up": 0, "stale": 0, "stopped": None},
        "embedding": {"done": 0, "retry": 0, "gave_up": 0, "stale": 0, "stopped": None},
    }
    assert not _has_activity(idle)
    busy = {**idle, "summary": {**idle["summary"], "retry": 1}}
    assert _has_activity(busy)
