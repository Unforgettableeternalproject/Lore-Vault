"""D13：管線從服務拉取 episode（episode_source.py 與 pipeline 的 pull 階段）。

假服務是真的 HTTP（``tests/fake_service.py``），在記憶體裡模擬 ``GET /v1/episodes`` 的
``after_seq`` 增量模式與舊版 cursor 模式。所有路徑都指到 tmp_path，不碰 ~/.lore-vault。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import episode_source as es  # noqa: E402
import pipeline  # noqa: E402
from fake_service import FakeService, closed_port_url  # noqa: E402
from lore_vault.hooks.client_env import ClientSettings, Secret  # noqa: E402
from lore_vault.hooks.spool import spool_id  # noqa: E402

TOKEN = "t" * 40
HERE = "host-a"


@pytest.fixture(autouse=True)
def _never_adjudicate(monkeypatch):
    """本檔任何測試都不可叫真的 `claude -p`，也不可寫進真的資料目錄。"""
    monkeypatch.setattr(pipeline, "adjudicate",
                        lambda *a, **k: pytest.fail("測試不可呼叫 claude -p"))


def _settings(url: str) -> ClientSettings:
    return ClientSettings(env_file=None, url=url, token=Secret(TOKEN))


def ep(session: str, turn: int, *, machine: str = HERE, started: str = "2026-09-01T00:00:00Z",
       text: str = "a", repo_root: str | None = "C:/repo") -> dict:
    """結構化假 episode（只有管線看得到的欄位，內容是佔位字）。"""
    return {"session_id": session, "prompt_id": f"p-{session}-{turn}", "turn_index": turn,
            "origin": "human", "machine": machine, "started_at": started,
            "repo": "Repo-X", "repo_root": repo_root, "cwd": [repo_root or "/x"],
            "user_text": "u", "assistant_text": text, "files_edited": ["src/a.py"],
            "tool_calls_total": 1}


class Server:
    """記憶體裡的 episodes 表：只插入；seq 由呼叫端指定（模擬 seq 重用、資料庫還原）。"""

    def __init__(self, *, legacy: bool = False) -> None:
        self.rows: list[tuple[int, dict]] = []
        self.legacy = legacy
        self.next_seq = 1

    def add(self, episode: dict, *, vault: str = "github.com/o/repo-x",
            seq: int | None = None) -> int:
        if seq is None:
            seq = self.next_seq
        self.next_seq = max(self.next_seq, seq + 1)
        wire = {k: v for k, v in episode.items()}
        wire["vault"] = vault
        self.rows.append((seq, wire))
        self.rows.sort(key=lambda r: r[0])
        return seq

    def handler(self, method, path, headers, body):
        query = {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}
        assert method == "GET" and query["vault"] == "*"
        limit = int(query["limit"])
        if self.legacy or "after_seq" not in query:
            start = int(query.get("cursor") or 0)
            page = self.rows[start:start + limit]
            nxt = str(start + limit) if start + limit < len(self.rows) else None
            return 200, {"items": [w for _, w in page], "next_cursor": nxt}, {}
        after = int(query["after_seq"])
        max_seq = max((s for s, _ in self.rows), default=0)
        total = sum(1 for s, _ in self.rows if s <= max_seq)
        rows = [(s, w) for s, w in self.rows if after < s <= max_seq]
        page = rows[:limit]
        return 200, {
            "items": [{**w, "seq": s} for s, w in page],
            "next_cursor": None,
            "next_after_seq": page[-1][0] if len(rows) > limit else None,
            "max_seq": max_seq,
            "total": total,
        }, {}


def _after_seqs(svc: FakeService) -> list[str]:
    return [parse_qs(urlparse(r["path"]).query).get("after_seq", [None])[0]
            for r in svc.requests]


def _local_dir(tmp_path: Path, episodes: list[dict]) -> Path:
    directory = tmp_path / "episodes"
    directory.mkdir(exist_ok=True)
    for e in episodes:
        with (directory / f"{e['session_id']}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: v for k, v in e.items() if k != "machine"}) + "\n")
    return directory


# --- 增量水位 ----------------------------------------------------------------------

def test_incremental_pull_only_asks_after_the_watermark(tmp_path):
    server = Server()
    for turn in range(3):
        server.add(ep("s1", turn))
    cache = tmp_path / "cache"
    with FakeService(server.handler) as svc:
        first = es.pull(_settings(svc.url), cache, page_size=2)
        assert first.resync_reason and first.after_seq == 3 and first.cached == 3
        server.add(ep("s1", 3))
        svc.requests.clear()
        second = es.pull(_settings(svc.url), cache, page_size=2)
    assert _after_seqs(svc) == ["3"]
    assert second.resync_reason is None and second.fetched == 1 and second.cached == 4
    assert es.load_cache_state(cache)["after_seq"] == 4


def test_late_arriving_old_episode_is_not_skipped(tmp_path):
    """遠端機器離線後補推：started_at 比水位時的所有 episode 都舊，seq 仍在水位之後。"""
    server = Server()
    server.add(ep("s1", 0, started="2026-09-20T00:00:00Z"))
    cache = tmp_path / "cache"
    with FakeService(server.handler) as svc:
        es.pull(_settings(svc.url), cache)
        server.add(ep("remote", 0, machine="host-b", started="2026-08-01T00:00:00Z"))
        result = es.pull(_settings(svc.url), cache)
    assert result.resync_reason is None
    assert ("remote", "p-remote-0", 0) in es.load_service_log(cache)


def test_count_mismatch_forces_full_resync(tmp_path, monkeypatch):
    """保護 3：有列落在水位之前（seq 重用等）時只有筆數對帳抓得到。"""
    server = Server()
    for turn in range(3):
        server.add(ep("s1", turn), seq=turn + 10)
    cache = tmp_path / "cache"

    def run() -> dict:
        with FakeService(server.handler) as svc:
            es.pull(_settings(svc.url), cache)
            server.add(ep("hidden", 0), seq=5)  # 水位 12 之前
            result = es.pull(_settings(svc.url), cache)
        assert result.resync_reason is None or "不符" in result.resync_reason
        return es.load_service_log(cache)

    assert ("hidden", "p-hidden-0", 0) in run()
    # 拿掉保護就會漏：證明上面的斷言真的在檢查對帳
    server.rows = [r for r in server.rows if r[1]["session_id"] != "hidden"]
    cache = tmp_path / "cache2"
    monkeypatch.setattr(es, "count_mismatch", lambda present, total: None)
    assert ("hidden", "p-hidden-0", 0) not in run()


def test_restored_database_resyncs_and_reports_missing(tmp_path):
    """保護 2：服務端 max_seq 退到水位之前＝資料庫被還原；曾收下的鍵不見了要報出來。"""
    server = Server()
    for turn in range(4):
        server.add(ep("s1", turn))
    cache = tmp_path / "cache"
    with FakeService(server.handler) as svc:
        es.pull(_settings(svc.url), cache)
        server.rows = server.rows[:2]  # 還原到只有兩筆的備份
        result = es.pull(_settings(svc.url), cache)
    assert "max_seq" in (result.resync_reason or "")
    assert result.server_missing == [["s1", "p-s1-2", 2], ["s1", "p-s1-3", 3]]
    assert result.cached == 4  # 快取不丟資料
    assert es.load_cache_state(cache)["server_missing"] == result.server_missing


def test_legacy_server_falls_back_to_cursor_full_pull(tmp_path):
    server = Server(legacy=True)
    for turn in range(3):
        server.add(ep("s1", turn))
    cache = tmp_path / "cache"
    with FakeService(server.handler) as svc:
        result = es.pull(_settings(svc.url), cache, page_size=2)
    assert result.legacy and result.after_seq is None and result.cached == 3
    assert es.load_cache_state(cache)["after_seq"] is None


def test_interrupted_append_does_not_duplicate_or_skip(tmp_path):
    """追加後、寫水位前中斷：下次從舊水位重拉，同鍵不再追加。"""
    server = Server()
    server.add(ep("s1", 0))
    cache = tmp_path / "cache"
    with FakeService(server.handler) as svc:
        es.pull(_settings(svc.url), cache)
        server.add(ep("s1", 1))
        state = es.load_cache_state(cache)
        es.pull(_settings(svc.url), cache)
        es.save_cache_state(cache, state)  # 模擬水位沒寫成
        result = es.pull(_settings(svc.url), cache)
    lines = (cache / es.SERVICE_LOG).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and result.appended == 0 and result.cached == 2


# --- 合併與去重 -----------------------------------------------------------------------

def test_merge_dedups_by_spool_key_and_keeps_local_copy(tmp_path):
    local_ep = ep("s1", 0, text="本機完整版")
    local = es.load_local(_local_dir(tmp_path, [local_ep, ep("s1", 1)]))
    server_copy = {**ep("s1", 0, text="服務端版"), "vault": "github.com/o/repo-x", "seq": 1}
    mine_only_on_server = {**ep("s0", 0), "vault": "github.com/o/repo-x", "seq": 2}
    remote = {**ep("r1", 0, machine="host-b", repo_root="/home/b/repo"),
              "vault": "github.com/o/repo-x", "seq": 3}
    service = {es.episode_key(e): e for e in (server_copy, mine_only_on_server, remote)}
    spool = tmp_path / "spool" / "pending"
    spool.mkdir(parents=True)
    (spool / f"{spool_id(ep('s1', 1))}.json").write_text("{}", encoding="utf-8")

    merged, counts = es.merge(local, service, machine=HERE, spool_dir=tmp_path / "spool")
    by_key = {es.episode_key(e): e for e in merged}
    assert len(merged) == len(by_key) == 4
    both = by_key[("s1", "p-s1-0", 0)]
    assert both["assistant_text"] == "本機完整版"
    assert both["vault"] == "github.com/o/repo-x" and both["machine"] == HERE
    assert by_key[("r1", "p-r1-0", 0)]["repo_root"] is None  # 別台機器的路徑不外流
    assert by_key[("s0", "p-s0-0", 0)]["repo_root"] == "C:/repo"  # 本機的保留
    assert "seq" not in by_key[("r1", "p-r1-0", 0)]
    assert counts == {"local": 2, "service": 3, "both": 1, "local_only": 1,
                      "local_only_spooled": 1, "service_only": 2, "foreign": 1}


def test_distill_reads_the_merged_dir_like_the_local_one(tmp_path):
    """合併目錄交給蒸餾端既有的 load_deduped：遠端輪次進得了候選、本機行為不變。"""
    import distill

    local = [ep("s1", 0), ep("s1", 1)]
    remote = [{**ep("r1", t, machine="host-b"), "vault": "v", "seq": t + 1} for t in range(2)]
    merged, _ = es.merge(es.load_local(_local_dir(tmp_path, local)),
                         {es.episode_key(e): e for e in remote}, machine=HERE)
    directory = es.write_merged(tmp_path / "cache", merged)
    candidates = distill.find_candidates(distill.load_episodes(directory))
    assert sorted(c["session_id"] for c in candidates) == ["r1", "s1"]
    alone = distill.find_candidates(distill.load_episodes(tmp_path / "episodes"))
    assert [c["session_id"] for c in alone] == ["s1"]


# --- 降級與旗標 -----------------------------------------------------------------------

def _resolve(tmp_path, url, **kw):
    return es.resolve_source(
        source=kw.pop("source", "service"), on_failure=kw.pop("on_failure", "local"),
        local_dir=_local_dir(tmp_path, [ep("s1", 0)]), cache_dir=tmp_path / "cache",
        spool_dir=None, settings_loader=lambda: _settings(url), previous=kw.pop("previous", None),
        machine=HERE, timeout=2, **kw)


def test_unreachable_service_falls_back_to_local_plus_stale_cache(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    stale = {**ep("r1", 0, machine="host-b"), "vault": "v", "seq": 1}
    (cache / es.SERVICE_LOG).write_text(json.dumps(stale) + "\n", encoding="utf-8")
    outcome = _resolve(tmp_path, closed_port_url(),
                       previous={"last_ok_at": "2026-09-26T03:30:00+00:00"})
    assert outcome.ok and outcome.record["mode"] == es.MODE_FALLBACK
    assert outcome.record["error_kind"] == es.ERROR_UNAVAILABLE
    assert outcome.record["last_ok_at"] == "2026-09-26T03:30:00+00:00"
    assert outcome.record["counts"]["merged"] == 2
    assert "降級" in outcome.summary


def test_unreachable_service_with_fail_policy_stops(tmp_path):
    outcome = _resolve(tmp_path, closed_port_url(), on_failure="fail")
    assert not outcome.ok and outcome.episode_dir is None
    assert outcome.record["mode"] == es.MODE_FAILED


def test_unconfigured_client_is_a_fallback_not_a_crash(tmp_path):
    def unconfigured():
        raise RuntimeError("推送未設定：缺 URL")

    outcome = es.resolve_source(
        source="service", on_failure="local", local_dir=_local_dir(tmp_path, [ep("s1", 0)]),
        cache_dir=tmp_path / "cache", spool_dir=None, settings_loader=unconfigured,
        previous=None, machine=HERE)
    assert outcome.ok and outcome.record["error_kind"] == es.ERROR_UNCONFIGURED


def test_forced_local_uses_the_episode_dir_untouched(tmp_path):
    outcome = _resolve(tmp_path, "http://unused", source="local")
    assert outcome.episode_dir == tmp_path / "episodes"
    assert outcome.record["mode"] == es.MODE_FORCED
    assert not (tmp_path / "cache").exists()


def test_successful_resolve_records_counts(tmp_path):
    server = Server()
    server.add(ep("s1", 0))
    server.add(ep("r1", 0, machine="host-b"))
    with FakeService(server.handler) as svc:
        outcome = _resolve(tmp_path, svc.url)
    record = outcome.record
    assert record["mode"] == es.MODE_SERVICE and record["ok"]
    assert record["service_total"] == 2 and record["counts"]["cached"] == 2
    assert record["counts"]["both"] == 1 and record["counts"]["foreign"] == 1
    assert record["last_ok_at"] == record["at"]


# --- 管線接線 ---------------------------------------------------------------------------

@pytest.fixture
def isolated_pipeline(tmp_path, monkeypatch):
    """pipeline 的模組常數在 import 時定值，LORE_VAULT_SPIKE_HOME 事後設定無效，逐一改指 tmp。"""
    monkeypatch.setenv("LORE_VAULT_SPIKE_HOME", str(tmp_path))
    monkeypatch.setattr(pipeline, "WORK_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "STATE_PATH", tmp_path / "pipeline_state.json")
    monkeypatch.setattr(pipeline, "EPISODE_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(pipeline, "DEFAULT_EPISODE_DIR", _local_dir(tmp_path, [ep("s1", 0)]))
    monkeypatch.setattr(pipeline, "SPOOL_DIR", tmp_path / "spool")
    return tmp_path


def test_pull_stage_records_state_and_last_run_survives(isolated_pipeline, monkeypatch):
    server = Server()
    server.add(ep("r1", 0, machine="host-b"))
    with FakeService(server.handler) as svc:
        monkeypatch.setattr(pipeline, "service_settings", lambda: _settings(svc.url))
        code = pipeline.run_pipeline(dry_run=False, max_groups=1, only="pull")
    assert code == 0
    state = json.loads((isolated_pipeline / "pipeline_state.json").read_text(encoding="utf-8"))
    # last_run 是跑完才寫的，不可把階段中寫的 episode_pull 蓋掉
    assert state[pipeline.EPISODE_PULL_KEY]["mode"] == es.MODE_SERVICE
    assert state["last_run"]["results"][0]["stage"] == "pull"
    assert (isolated_pipeline / "cache" / es.MERGED_DIR / es.MERGED_FILE).exists()


def test_distill_stage_reads_the_resolved_dir(isolated_pipeline, monkeypatch):
    calls: list[list[str]] = []

    def fake_run_tool(args, **kw):
        calls.append(args)
        return True, ""

    monkeypatch.setattr(pipeline, "run_tool", fake_run_tool)
    monkeypatch.setattr(pipeline, "service_settings", lambda: _settings(closed_port_url()))
    ctx = {"dry_run": False, "max_groups": 1, "episode_source": "service",
           "on_pull_failure": "local"}
    ok, _ = pipeline.stage_distill(ctx)  # 沒有待蒸餾的組 → 成功結束
    assert ok
    emit = calls[0]
    assert emit[emit.index("--episode-dir") + 1] == str(isolated_pipeline / "cache" / "merged")
    state = json.loads((isolated_pipeline / "pipeline_state.json").read_text(encoding="utf-8"))
    assert state[pipeline.EPISODE_PULL_KEY]["mode"] == es.MODE_FALLBACK


def test_pull_stage_dry_run_touches_nothing(isolated_pipeline, monkeypatch):
    monkeypatch.setattr(pipeline, "service_settings",
                        lambda: pytest.fail("dry-run 不該連服務"))
    ok, summary = pipeline.stage_pull({"dry_run": True})
    assert ok and "全量拉取" in summary
    assert not (isolated_pipeline / "cache").exists()
    assert not (isolated_pipeline / "pipeline_state.json").exists()


def test_fallback_is_visible_to_health_alert(isolated_pipeline, monkeypatch):
    import hook_health_alert

    state_path = isolated_pipeline / "pipeline_state.json"
    state_path.write_text(json.dumps({"episode_pull": {
        "mode": es.MODE_FALLBACK, "at": "2026-09-27T03:31:00+00:00", "reason": "連不上",
        "counts": {"server_missing": 3}}}), encoding="utf-8")
    monkeypatch.setattr(hook_health_alert, "STATE_PATH", state_path)
    alerts = hook_health_alert.collect_alerts()
    assert any("沒從服務拉到 episode" in a for a in alerts)
    assert any("少了 3 筆" in a for a in alerts)


def test_pull_record_keys_agree_with_doctor_and_health_alert():
    """pipeline 寫、doctor 與健康告警讀同一組鍵與值；改名要三處一起改。"""
    import hook_health_alert
    import paths

    from lore_vault.doctor import episode_pull_check as check

    assert check.EPISODE_PULL_KEY == es.EPISODE_PULL_KEY == hook_health_alert.EPISODE_PULL_KEY
    assert check.MODE_SERVICE == es.MODE_SERVICE
    assert check.MODE_FALLBACK == es.MODE_FALLBACK == hook_health_alert.EPISODE_PULL_FALLBACK
    assert check.MODE_FORCED == es.MODE_FORCED == hook_health_alert.EPISODE_PULL_FORCED
    assert check.MODE_FAILED == es.MODE_FAILED == hook_health_alert.EPISODE_PULL_FAILED
    assert check.ERROR_CONSISTENCY == es.ERROR_CONSISTENCY
    assert check.PIPELINE_STATE_NAME == paths.PIPELINE_STATE_PATH.name
    assert paths.EPISODE_CACHE_DIR.parent == paths.WORK_DIR
