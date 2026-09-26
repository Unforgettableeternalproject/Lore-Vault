"""階段 8（spike 接入服務）的 hook 端測試：Stop hook spool／推送、PreToolUse 讀快照、管線轉接骨架。

- Stop hook：新輪次寫進 spool（machine／vault 凍結）、jsonl 照寫；服務不可達時
  額外延遲有硬上限（量測 p95）、退避期間幾乎零成本
- PreToolUse：同一組（檔案 + 符號錨點）輸入，讀服務快照與讀舊 concepts.json
  選出相同的 concept 序列（scorer 與選取邏輯不動）；快照缺失／損毀時降級不拋例外
- 管線：GET /v1/episodes 分頁、POST /v1/concepts 拆批與刪除差集（fake 服務）

假服務是真的 HTTP（``tests/fake_service.py``），因為客戶端是 ``urllib``。

執行：``python -m pytest agent_memory_spike/test_service_bridge.py -q``
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tests"))

import hook_pretooluse  # noqa: E402
import hook_stop  # noqa: E402
import pipeline  # noqa: E402
from fake_service import BlackHole, FakeService, closed_port_url  # noqa: E402
from lore_vault.hooks import concept_snapshot, spool  # noqa: E402
from lore_vault.hooks.client_env import KNOWN_KEYS, CLIENT_ENV_VAR, ClientSettings, Secret  # noqa: E402

TOKEN = "tok-" + "t" * 32


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """行程環境裡的同名鍵會蓋過 client.env：一律清掉。"""
    for key in (*KNOWN_KEYS, CLIENT_ENV_VAR):
        monkeypatch.delenv(key, raising=False)


def _client_env(tmp_path: Path, **values: str) -> Path:
    path = tmp_path / "client.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


# --- Stop hook：spool ---------------------------------------------------------

def _user(pid, cwd):
    return {"type": "user", "promptId": pid, "cwd": cwd, "gitBranch": "main",
            "sessionId": "sess", "version": "2.1.216",
            "timestamp": "2026-07-25T00:00:00.000Z", "origin": {"kind": "human"},
            "message": {"role": "user", "content": f"問題 {pid}"}}


def _assistant(cwd, target):
    return {"type": "assistant", "cwd": cwd, "gitBranch": "main", "sessionId": "sess",
            "version": "2.1.216", "timestamp": "2026-07-25T00:00:01.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": target}},
                {"type": "text", "text": "好"}]}}


def _transcript(tmp_path: Path, repo: Path, turns: int) -> Path:
    records = []
    for i in range(turns):
        records.append(_user(f"p{i}", str(repo)))
        records.append(_assistant(str(repo), str(repo / "src" / "a.py")))
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_sync_spools_new_turns_with_frozen_machine_and_vault(tmp_path, monkeypatch):
    repo = tmp_path / "Demo-Repo"
    (repo / ".git").mkdir(parents=True)
    ep_dir = tmp_path / "data" / "episodes"
    monkeypatch.setattr(hook_stop, "current_machine", lambda: "desk-a")

    written, _ = hook_stop.sync(_transcript(tmp_path, repo, 3), ep_dir, "sess")
    assert written == 2

    # 原本的 jsonl 照寫、格式不變（不多 machine／vault）
    lines = hook_stop.episode_path(ep_dir, "sess").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and "machine" not in json.loads(lines[0])

    spool_dir = hook_stop.spool_dir_for(ep_dir)
    assert spool_dir == tmp_path / "data" / "spool"
    records = [json.loads(p.read_text(encoding="utf-8"))
               for p in sorted((spool_dir / spool.PENDING).glob("*.json"))]
    assert len(records) == 2
    for rec in records:
        ep = rec["episode"]
        assert ep["machine"] == "desk-a"
        # 假 .git 沒有 remote → binding 退回資料夾名
        assert ep["vault"] == "folder/demo-repo"
        assert ep["repo"] == "Demo-Repo"

    # 重跑：沒有新輪次 → spool 不變
    hook_stop.sync(_transcript(tmp_path, repo, 3), ep_dir, "sess")
    assert len(list((spool_dir / spool.PENDING).glob("*.json"))) == 2


def test_dry_run_and_spool_false_do_not_spool(tmp_path):
    repo = tmp_path / "Demo"
    (repo / ".git").mkdir(parents=True)
    ep_dir = tmp_path / "eps"
    hook_stop.sync(_transcript(tmp_path, repo, 3), ep_dir, "sess", dry_run=True)
    hook_stop.sync(_transcript(tmp_path, repo, 3), ep_dir, "sess2", spool=False)
    assert not (hook_stop.spool_dir_for(ep_dir) / spool.PENDING).exists()


def test_spool_failure_never_breaks_the_corpus_write(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "Demo"
    (repo / ".git").mkdir(parents=True)
    ep_dir = tmp_path / "eps"

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(spool, "spool_episodes", boom)
    written, _ = hook_stop.sync(_transcript(tmp_path, repo, 3), ep_dir, "sess")
    assert written == 2
    assert hook_stop.episode_path(ep_dir, "sess").exists()
    assert "spool 寫入失敗" in capsys.readouterr().err


# --- Stop hook：推送延遲 -------------------------------------------------------

PUSH_TIMEOUT = 0.5
GRACE = 0.5


def _p95(samples):
    ordered = sorted(samples)
    return ordered[max(0, int(round(0.95 * len(ordered))) - 1)]


def _measure_push(tmp_path, monkeypatch, url, runs=10, *, reset_backoff=True):
    env = _client_env(tmp_path, LORE_VAULT_URL=url, LORE_VAULT_API_TOKEN=TOKEN,
                      LORE_VAULT_PUSH_TIMEOUT=str(PUSH_TIMEOUT))
    monkeypatch.setattr(hook_stop, "CLIENT_ENV_PATH", env)
    spool_dir = tmp_path / "spool"
    spool.write_pending(spool_dir, {"session_id": "s", "prompt_id": "p", "turn_index": 0,
                                    "machine": "m", "vault": "folder/x"})
    samples, messages = [], []
    for _ in range(runs):
        if reset_backoff:
            (spool_dir / spool.STATE_FILE).unlink(missing_ok=True)
        started = time.perf_counter()
        messages.append(hook_stop.push_after_stop(spool_dir, grace=GRACE))
        samples.append(time.perf_counter() - started)
    assert spool.spool_stats(spool_dir).pending == 1  # 一筆都沒丟
    return samples, messages


@pytest.mark.parametrize("kind", ["refused", "blackhole"])
def test_stop_push_latency_is_bounded_when_service_unreachable(tmp_path, monkeypatch, kind):
    """服務不可達時 Stop hook 的額外延遲 p95 必須在「推送逾時 + grace」內（外加排程誤差）。"""
    if kind == "refused":
        samples, messages = _measure_push(tmp_path, monkeypatch, closed_port_url())
    else:
        with BlackHole() as hole:
            samples, messages = _measure_push(tmp_path, monkeypatch, hole.url)
    p95 = _p95(samples)
    print(f"\n[latency] Stop 推送（{kind}）median {statistics.median(samples) * 1000:.0f} ms、"
          f"p95 {p95 * 1000:.0f} ms、上限 {(PUSH_TIMEOUT + GRACE) * 1000:.0f} ms")
    assert p95 <= PUSH_TIMEOUT + GRACE + 0.25
    assert all(("連線失敗" in m) or ("逾時" in m) for m in messages), messages


def test_backoff_makes_repeat_stops_nearly_free(tmp_path, monkeypatch):
    samples, messages = _measure_push(tmp_path, monkeypatch, closed_port_url(), runs=10,
                                      reset_backoff=False)
    tail = samples[1:]  # 第一次真的去連；之後都在退避期內
    print(f"\n[latency] 退避期間 Stop 推送 median {statistics.median(tail) * 1000:.1f} ms、"
          f"max {max(tail) * 1000:.1f} ms")
    assert statistics.median(tail) < 0.02 and max(tail) < PUSH_TIMEOUT
    assert all("退避" in m for m in messages[1:])


def test_unconfigured_stop_push_only_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_stop, "CLIENT_ENV_PATH", tmp_path / "none.env")
    assert hook_stop.push_after_stop(tmp_path / "spool").startswith("未推送：推送未設定")


@pytest.fixture
def _spool_with_one(tmp_path):
    spool_dir = tmp_path / "spool"
    spool.write_pending(spool_dir, {"session_id": "s", "prompt_id": "p", "turn_index": 0,
                                    "machine": "m", "vault": "folder/x"})
    return spool_dir


def test_stop_push_delivers(tmp_path, monkeypatch, _spool_with_one):
    with FakeService() as svc:
        env = _client_env(tmp_path, LORE_VAULT_URL=svc.url, LORE_VAULT_API_TOKEN=TOKEN)
        monkeypatch.setattr(hook_stop, "CLIENT_ENV_PATH", env)
        message = hook_stop.push_after_stop(_spool_with_one)
    assert "accepted 1" in message
    assert spool.spool_stats(_spool_with_one).pending == 0


def test_push_command_exit_codes(tmp_path, monkeypatch, capsys):
    ep_dir = tmp_path / "episodes"
    spool.write_pending(hook_stop.spool_dir_for(ep_dir),
                        {"session_id": "s", "prompt_id": "p", "turn_index": 0})
    monkeypatch.setattr(hook_stop, "CLIENT_ENV_PATH",
                        _client_env(tmp_path, LORE_VAULT_URL=closed_port_url(),
                                    LORE_VAULT_API_TOKEN=TOKEN,
                                    LORE_VAULT_PUSH_TIMEOUT="0.5"))
    assert hook_stop.push_command(ep_dir, dry_run=True) == 0
    assert hook_stop.push_command(ep_dir, dry_run=False) == 1
    err = capsys.readouterr().err
    assert "待推送 1" in err and TOKEN not in err


# --- PreToolUse：快照來源 -----------------------------------------------------

def _concept(cid, anchors, surprisal=1.0, scope="proj"):
    return {"id": cid, "statement": f"statement {cid}", "anchors": anchors,
            "surprisal": surprisal, "scope": scope, "kind": "project-fact"}


POOL = [
    _concept("c-01", ["src/api/tracking.ts", "fetchTracking"]),
    _concept("c-02", ["src/api/tracking.ts", "fetchTracking"], surprisal=0.5),  # 未過門檻
    _concept("c-03", ["tracking.ts", "retryPolicy"], scope=None),
    _concept("c-04", ["src/api/tracking.ts", "fetchTracking"], scope="other"),
    _concept("c-05", ["src/ui/Cart.tsx", "CartItem", "useCart"]),
    _concept("c-06", ["Cart.tsx", "useCart"], scope="global"),
    _concept("c-07", ["src/api/tracking.ts", "fetchTracking", "retryPolicy"]),
    _concept("c-08", ["src/api/tracking.ts", "fetchTracking"], surprisal=None),
    _concept("c-09", ["README.md", "install"]),
    _concept("c-10", ["src/ui/Cart.tsx", "CartItem"], surprisal=0.8),
    _concept("c-11", ["src/api/tracking.ts", "fetchTracking"], surprisal=0.95),
]

# 一段連續的編輯：同一輪累積、換輪重置、同 session 不重複注入都會被走到
EDITS = [
    ("p1", "src/api/tracking.ts", "function fetchTracking() {}"),
    ("p1", "src/api/tracking.ts", "const retryPolicy = 3"),
    ("p1", "src/ui/Cart.tsx", "export function CartItem() { useCart() }"),
    ("p2", "src/ui/Cart.tsx", "CartItem"),
    ("p2", "README.md", "install"),
    ("p3", "src/api/tracking.ts", "fetchTracking retryPolicy"),
    ("p3", "docs/none.md", "nothing"),
]


def _prepare(tmp_path, monkeypatch, *, snapshot: bool, name: str):
    """回傳一個設定好來源的隔離目錄。兩種來源放同一份 POOL，寫法各照其來源。"""
    root = tmp_path / name
    root.mkdir()
    repo = root / "proj"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(hook_pretooluse, "STATE_DIR", root / "state")
    monkeypatch.setattr(hook_pretooluse, "INJECTION_LOG", root / "injections.jsonl")
    monkeypatch.setattr(hook_pretooluse, "TOUCH_LOG", root / "touches.jsonl")
    legacy = root / "concepts.json"
    if snapshot:
        # 服務端 render_export 的序列化（ensure_ascii=False, indent=2），經殼端安裝流程寫入
        body = json.dumps(POOL, ensure_ascii=False, indent=2).encode("utf-8")
        snap = root / "snapshot" / "concepts.json"
        concept_snapshot.install(snap, body)
        env = _client_env(root, LORE_VAULT_CONCEPT_SNAPSHOT=str(snap))
        monkeypatch.setattr(hook_pretooluse, "CONCEPT_PATH", root / "absent.json")
    else:
        legacy.write_text(json.dumps(POOL), encoding="utf-8")
        env = root / "no-client.env"
        monkeypatch.setattr(hook_pretooluse, "CONCEPT_PATH", legacy)
    monkeypatch.setattr(hook_pretooluse, "CLIENT_ENV_PATH", env)
    return repo


def _run_edits(repo, edits=EDITS, *, dry_run=False):
    picked = []
    for prompt_id, rel, text in edits:
        target = repo / rel
        result = hook_pretooluse.run({
            "session_id": "s1", "prompt_id": prompt_id, "tool_name": "Edit", "cwd": str(repo),
            "tool_input": {"file_path": str(target), "new_string": text},
        }, dry_run=dry_run)
        context = result["hookSpecificOutput"]["additionalContext"] if result else ""
        ids = [line[len("- statement "):] for line in context.splitlines()
               if line.startswith("- statement ")]
        picked.append((ids, context.replace(str(repo), "<repo>")))
    return picked


def test_snapshot_and_legacy_select_identical_sequences(tmp_path, monkeypatch):
    """等價測試：讀服務快照與讀舊 concepts.json，逐次選出相同的 concept（含注入文字）。"""
    legacy_repo = _prepare(tmp_path, monkeypatch, snapshot=False, name="legacy")
    legacy = _run_edits(legacy_repo)
    snap_repo = _prepare(tmp_path, monkeypatch, snapshot=True, name="snap")
    assert hook_pretooluse.concept_source()[1] is True  # 真的走快照來源
    snapshot = _run_edits(snap_repo)

    assert [ids for ids, _ in snapshot] == [ids for ids, _ in legacy]
    assert [text for _, text in snapshot] == [text for _, text in legacy]
    injected = [ids for ids, _ in legacy if ids]
    assert len(injected) >= 3, legacy  # 這組輸入確實有觸發注入，不是兩邊都空


def test_unconfigured_source_is_legacy_concepts_json(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, snapshot=False, name="legacy")
    path, is_snapshot = hook_pretooluse.concept_source()
    assert path == hook_pretooluse.CONCEPT_PATH and not is_snapshot


@pytest.mark.parametrize("damage", ["missing", "not-json", "not-a-list", "unreadable-items"])
def test_broken_snapshot_degrades_without_raising(tmp_path, monkeypatch, capsys, damage):
    repo = _prepare(tmp_path, monkeypatch, snapshot=True, name="snap")
    snap = tmp_path / "snap" / "snapshot" / "concepts.json"
    if damage == "missing":
        snap.unlink()
    elif damage == "not-json":
        snap.write_text("{half", encoding="utf-8")
    elif damage == "not-a-list":
        snap.write_text('{"c-01": {}}', encoding="utf-8")
    else:
        snap.write_text("[1, 2, 3]", encoding="utf-8")
    picked = _run_edits(repo, EDITS[:2])
    assert all(ids == [] for ids, _ in picked)
    assert "[inject] 降級" in capsys.readouterr().err
    # 降級時 touched 照樣累積（下次快照恢復就能接上）
    state = json.loads((tmp_path / "snap" / "state" / "s1.json").read_text(encoding="utf-8"))
    assert state["symbols"]


def _time_runs(repo, n=60):
    samples = []
    for i in range(n):
        started = time.perf_counter()
        hook_pretooluse.run({
            "session_id": f"lat-{i}", "prompt_id": "p1", "tool_name": "Edit", "cwd": str(repo),
            "tool_input": {"file_path": str(repo / "src/api/tracking.ts"),
                           "new_string": "fetchTracking retryPolicy"},
        }, dry_run=True)
        samples.append(time.perf_counter() - started)
    return samples


def test_pretooluse_latency_does_not_regress(tmp_path, monkeypatch):
    """快照來源不走網路：延遲與讀舊 concepts.json 同一量級（絕對上限，數字另行回報）。"""
    legacy = _time_runs(_prepare(tmp_path, monkeypatch, snapshot=False, name="legacy"))
    snapshot = _time_runs(_prepare(tmp_path, monkeypatch, snapshot=True, name="snap"))
    print(f"\n[latency] PreToolUse run() median legacy {statistics.median(legacy) * 1000:.2f} ms"
          f" / snapshot {statistics.median(snapshot) * 1000:.2f} ms；"
          f"p95 {_p95(legacy) * 1000:.2f} / {_p95(snapshot) * 1000:.2f} ms")
    assert _p95(snapshot) < 0.05


# --- 管線轉接骨架 --------------------------------------------------------------

def _svc_settings(url):
    return ClientSettings(env_file=None, url=url, token=Secret(TOKEN))


def test_fetch_episodes_follows_cursor():
    pages = {None: (["e1", "e2"], "c1"), "c1": (["e3"], None)}

    def handler(method, path, headers, body):
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(path).query)
        assert query["vault"] == ["*"] and query["since"] == ["2026-09-01T00:00:00Z"]
        items, nxt = pages[(query.get("cursor") or [None])[0]]
        return 200, {"items": [{"id": i} for i in items], "next_cursor": nxt}, {}

    with FakeService(handler) as svc:
        items = pipeline.fetch_episodes(_svc_settings(svc.url), since="2026-09-01T00:00:00Z")
    assert [i["id"] for i in items] == ["e1", "e2", "e3"]
    assert all(r["path"].startswith("/v1/episodes?") for r in svc.requests)


def test_push_concept_changes_batches_and_deletes():
    def handler(method, path, headers, body):
        assert (method, path) == ("POST", "/v1/concepts")
        assert body["vault"] == "*" and body["mode"] == "upsert"
        return 200, {"applied": True, "results": []}, {}

    concepts = [{"id": f"c{i}"} for i in range(5)]
    delete = pipeline.diff_concept_ids(["c0", "gone-1", "gone-2"], concepts)
    assert delete == ["gone-1", "gone-2"]
    with FakeService(handler) as svc:
        responses = pipeline.push_concept_changes(_svc_settings(svc.url), concepts, delete,
                                                  batch_size=3)
    assert len(responses) == 3
    sent = [(len(r["body"]["concepts"]), len(r["body"]["delete"])) for r in svc.requests]
    assert sent == [(3, 0), (2, 1), (0, 1)]


def _rejected_handler(method, path, headers, body):
    """模擬服務端 A17 拒收：第二筆 vault_ambiguous、第三筆 vault_unresolved。"""
    assert body["vault"] == "*"
    assert all("vault" not in c for c in body["concepts"])  # 每筆不帶 vault
    results = [
        {"index": 0, "id": "c0", "vault": "github.com/o/a", "resolved_by": "scope_match",
         "status": "created"},
        {"index": 1, "id": "c1", "status": "invalid", "code": "vault_ambiguous",
         "candidates": ["github.com/org-a/tool", "github.com/org-b/tool"],
         "error": "scope 'Tool' 同時符合多個 vault；可把 scope 寫成 'org/repo'"},
        {"index": 2, "id": "c2", "status": "invalid", "code": "vault_unresolved",
         "error": "欄位值 機密陳述 c2 不合法"},  # 模擬錯誤訊息夾帶 statement
    ]
    return 400, {"error": {"code": "batch_rejected", "message": "2 筆 invalid",
                           "results": results, "delete_results": []}}, {}


def test_push_concepts_prints_per_item_reasons_and_fails(tmp_path, monkeypatch, capsys):
    from lore_vault.hooks.service import ServiceRejected

    concepts = [{"id": f"c{i}", "statement": f"機密陳述 c{i}", "scope": "Tool"}
                for i in range(3)]
    path = tmp_path / "concepts.json"
    path.write_text(json.dumps(concepts, ensure_ascii=False), encoding="utf-8")
    saved = []
    monkeypatch.setattr(pipeline, "load_state", lambda: {})
    monkeypatch.setattr(pipeline, "save_state", saved.append)
    with FakeService(_rejected_handler) as svc:
        with pytest.raises(ServiceRejected) as info:
            pipeline.push_concept_changes(_svc_settings(svc.url), concepts, [])
        assert info.value.status == 400
        assert info.value.body["error"]["results"][1]["code"] == "vault_ambiguous"

        monkeypatch.setattr(pipeline, "service_settings", lambda: _svc_settings(svc.url))
        assert pipeline.push_concepts_command(dry_run=False, concept_path=path) == 1
    err = capsys.readouterr().err
    assert "vault_ambiguous" in err and "vault_unresolved" in err
    assert "github.com/org-a/tool" in err and "id='c1'" in err
    assert "id='c0'" not in err  # 成功的那筆不列
    assert "機密陳述" not in err  # 不印 statement 原文
    assert saved == []  # 被拒就不更新已推送 id


def test_push_concepts_other_rejections_still_raise(tmp_path, monkeypatch):
    from lore_vault.hooks.service import ServiceRejected

    path = tmp_path / "concepts.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(pipeline, "load_state", lambda: {"service_pushed_concept_ids": ["x"]})
    monkeypatch.setattr(pipeline, "save_state", lambda state: None)
    with FakeService(lambda *a: (401, {"error": {"code": "unauthorized"}}, {})) as svc:
        monkeypatch.setattr(pipeline, "service_settings", lambda: _svc_settings(svc.url))
        with pytest.raises(ServiceRejected):
            pipeline.push_concepts_command(dry_run=False, concept_path=path)


def test_pipeline_stages_are_untouched():
    """轉接層預設關閉：三個判卷階段與順序不變。"""
    assert [name for name, _, _ in pipeline.STAGES] == [
        "collect", "health", "distill", "consolidate", "calibrate"]
