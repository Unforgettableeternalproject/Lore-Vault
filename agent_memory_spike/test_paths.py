"""資料路徑常數集中（T-10）的對帳測試。

守三件事：
1. 資料根目錄只在 ``paths.py`` 定義一次，其他模組不得再自己拼
   ``~/.lore-vault``（或 D5 前的 ``~/.claude/agent-memory-spike``）。
2. 各模組解析出的路徑逐一符合登記表（D5 後根目錄為 ``~/.lore-vault``）。
3. ``LORE_VAULT_SPIKE_HOME`` 覆寫會帶到所有衍生路徑。
4. D5 過渡 fallback 以整根目錄切換：只有新位置沒有 ``episodes/``、舊位置有時才用舊位置。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tokenize
from pathlib import Path

HERE = Path(__file__).parent

# 各模組的路徑常數，相對於家目錄。T-10 集中時逐一對過集中前的值；
# D5 只把根目錄從 .claude/agent-memory-spike 換成 .lore-vault，相對結構不變。
# 鍵集合也要一致：少了代表某個 monkeypatch 目標消失，多了代表新漏出一個名稱。
BEFORE = {
    "calibrate.DEFAULT_CONCEPT_PATH": ".lore-vault/concepts.json",
    "calibrate.DEFAULT_EPISODE_DIR": ".lore-vault/episodes",
    "calibrate.DEFAULT_INJECTION_PATH": ".lore-vault/injection_tasks.json",
    "calibrate.DEFAULT_PROBE_PATH": ".lore-vault/probe_tasks.json",
    "calibrate.WORK_DIR": ".lore-vault",
    "consolidate.DEFAULT_CONCEPT_PATH": ".lore-vault/concepts.json",
    "consolidate.DEFAULT_PAIR_PATH": ".lore-vault/consolidate_pairs.json",
    "consolidate.WORK_DIR": ".lore-vault",
    "distill.DEFAULT_CONCEPT_PATH": ".lore-vault/concepts.json",
    "distill.DEFAULT_EPISODE_DIR": ".lore-vault/episodes",
    "distill.DEFAULT_TASK_PATH": ".lore-vault/distill_tasks.json",
    "distill.DEFAULT_WATERMARK_PATH": ".lore-vault/distilled.json",
    "distill.WORK_DIR": ".lore-vault",
    "hook_health_alert.EPISODE_DIR": ".lore-vault/episodes",
    "hook_health_alert.INJECTION_LOG": ".lore-vault/injections.jsonl",
    "hook_health_alert.LOG_DIR": ".lore-vault/logs",
    "hook_health_alert.STATE_PATH": ".lore-vault/pipeline_state.json",
    "hook_health_alert.WORK_DIR": ".lore-vault",
    "hook_pretooluse.CONCEPT_PATH": ".lore-vault/concepts.json",
    "hook_pretooluse.INJECTION_LOG": ".lore-vault/injections.jsonl",
    "hook_pretooluse.STATE_DIR": ".lore-vault/inject_state",
    "hook_pretooluse.TOUCH_LOG": ".lore-vault/touches.jsonl",
    "hook_pretooluse.WORK_DIR": ".lore-vault",
    "hook_session_start.CONCEPT_PATH": ".lore-vault/concepts.json",
    "hook_session_start.INJECTION_LOG": ".lore-vault/injections.jsonl",
    "hook_stop.DEFAULT_EPISODE_DIR": ".lore-vault/episodes",
    "hook_userpromptsubmit.CONCEPT_PATH": ".lore-vault/concepts.json",
    "hook_userpromptsubmit.INJECTION_LOG": ".lore-vault/injections.jsonl",
    "pipeline.DEFAULT_EPISODE_DIR": ".lore-vault/episodes",
    "pipeline.LOCK_PATH": ".lore-vault/pipeline.lock",
    "pipeline.STATE_PATH": ".lore-vault/pipeline_state.json",
    "pipeline.WORK_DIR": ".lore-vault",
    # 階段 8 新增（不是集中前就有的常數；新名稱在此登記）
    "hook_pretooluse.CLIENT_ENV_PATH": ".lore-vault/client.env",
    "hook_stop.CLIENT_ENV_PATH": ".lore-vault/client.env",
    "pipeline.CLIENT_ENV_PATH": ".lore-vault/client.env",
    "pipeline.CONCEPT_PATH": ".lore-vault/concepts.json",
    # D13 新增：服務 episode 快取與 spool（同 hook_stop.spool_dir_for）
    "pipeline.EPISODE_CACHE_DIR": ".lore-vault/episode_cache",
    "pipeline.SPOOL_DIR": ".lore-vault/spool",
    "retrieve.CONTROL_CONCEPT_PATH": ".lore-vault/control_concepts.json",
    "retrieve.DEFAULT_CONCEPT_PATH": ".lore-vault/concepts.json",
    "retrieve.DEFAULT_EPISODE_DIR": ".lore-vault/episodes",
    "retrieve.WORK_DIR": ".lore-vault",
    "transcript.INJECTION_LOG": ".lore-vault/injections.jsonl",
    "transcript.TOUCH_LOG": ".lore-vault/touches.jsonl",
    # D5 新增：健康告警對帳舊位置用（覆寫根目錄時不在根目錄底下，不列入覆寫比對）
    "hook_health_alert.LEGACY_WORK_DIR": ".claude/agent-memory-spike",
}
LEGACY_ONLY = {"hook_health_alert.LEGACY_WORK_DIR"}

MODULES = sorted({key.split(".")[0] for key in BEFORE})

# 在子程序裡 import 全部模組、把家目錄底下的 Path 常數吐成 JSON。
# 用子程序是為了隔離：這些常數在 import 時就定值，同程序 reload 會讓
# 其他已 import 的模組留著舊綁定，而且會汙染同一輪的其他測試。
_DUMP = """
import importlib, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
root = Path(sys.argv[2])
out = {}
for name in sys.argv[3:]:
    mod = importlib.import_module(name)
    for key, value in vars(mod).items():
        if key.isupper() and isinstance(value, Path):
            try:
                rel = value.relative_to(root)
            except ValueError:
                continue
            out[f"{name}.{key}"] = rel.as_posix()
print(json.dumps(out))
"""


def _dump(tmp_path: Path, **extra_env: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "LORE_VAULT_SPIKE_HOME"}
    env.update(HOME=str(home), USERPROFILE=str(home), **extra_env)
    root = Path(extra_env.get("LORE_VAULT_SPIKE_HOME", home))
    proc = subprocess.run(
        [sys.executable, "-c", _DUMP, str(HERE), str(root), *MODULES],
        env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return json.loads(proc.stdout)


def test_every_module_resolves_the_same_paths_as_before(tmp_path):
    """逐一符合登記表；根目錄再變動時這裡會紅，那是要的訊號。"""
    assert _dump(tmp_path) == BEFORE


def test_env_override_moves_every_derived_path(tmp_path):
    override = tmp_path / "vault-data"
    got = _dump(tmp_path, LORE_VAULT_SPIKE_HOME=str(override))
    # 覆寫根目錄本身就是 WORK_DIR，相對路徑只剩根目錄以下那段
    prefix = ".lore-vault"
    expected = {
        k: v[len(prefix):].lstrip("/") or "."
        for k, v in BEFORE.items()
        if k not in LEGACY_ONLY
    }
    assert got == expected


# ---- 「還有模組自己拼資料根目錄」的掃描 ----

SPIKE_DIRNAME = "agent-memory-spike"
DATA_DIRNAMES = (SPIKE_DIRNAME, ".lore-vault")
_PATH_HINTS = ("Path(", "expanduser", "os.path.join", "Path.home")


def find_hardcoded_spike_paths(source: str) -> list[int]:
    """回傳疑似自行拼出資料根目錄的行號。

    註解與 docstring 不算（tokenize 只看 STRING token，且略過獨立成句的字串）；
    提示訊息、XML 屬性裡出現目錄名也不算——只抓兩種「拿來當路徑」的形狀：
    ``... / "agent-memory-spike"`` 這種路徑片段，以及同一行有 ``Path(``／
    ``Path.home``／``expanduser``／``os.path.join`` 又帶到目錄名的字串。
    新舊目錄名（``DATA_DIRNAMES``）都抓：舊名防回退，新名防另起爐灶。
    """
    hits: list[int] = []
    lines = source.splitlines()
    prev_significant = None
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        name = next((d for d in DATA_DIRNAMES if d in tok.string), None)
        if tok.type == tokenize.STRING and name:
            is_docstring = prev_significant in (None, tokenize.NEWLINE, tokenize.INDENT,
                                                tokenize.DEDENT)
            value = tok.string.strip("rbuRBUfF").strip("'\"")
            line = lines[tok.start[0] - 1]
            if not is_docstring and (
                value == name or any(h in line for h in _PATH_HINTS)
            ):
                hits.append(tok.start[0])
        if tok.type not in (tokenize.COMMENT, tokenize.NL):
            prev_significant = tok.type
    return hits


def test_scanner_flags_the_pre_t10_hardcoding():
    """證明掃描真的抓得到：拿集中前的原始寫法餵進去必須被標出。"""
    legacy = (
        "from pathlib import Path\n"
        'INJECTION_LOG = Path.home() / ".claude" / "agent-memory-spike" / "injections.jsonl"\n'
        'X = Path("~/.claude/agent-memory-spike/logs").expanduser()\n'
    )
    assert find_hardcoded_spike_paths(legacy) == [2, 3]


def test_scanner_flags_hardcoding_the_new_root():
    """D5 後的新目錄名一樣只能在 paths.py 出現。"""
    source = (
        "from pathlib import Path\n"
        'A = Path.home() / ".lore-vault" / "episodes"\n'
        'B = Path("~/.lore-vault/logs").expanduser()\n'
        'HINT = "查法：`~/.lore-vault/logs/`"\n'
    )
    assert find_hardcoded_spike_paths(source) == [2, 3]


def test_scanner_ignores_docs_and_display_strings():
    benign = (
        '"""放在 ~/.claude/agent-memory-spike/ 底下。"""\n'
        "# 註解提到 agent-memory-spike 沒關係\n"
        'TAG = \'<recalled-memory source="agent-memory-spike">\'\n'
        'HINT = "查法：`~/.claude/agent-memory-spike/logs/`"\n'
    )
    assert find_hardcoded_spike_paths(benign) == []


def test_only_paths_module_hardcodes_the_data_root():
    offenders = {}
    for py in sorted(HERE.glob("*.py")):
        if py.name in ("paths.py", Path(__file__).name):
            continue
        hits = find_hardcoded_spike_paths(py.read_text(encoding="utf-8"))
        if hits:
            offenders[py.name] = hits
    assert offenders == {}, f"資料根目錄應只在 paths.py 定義：{offenders}"


def test_run_pipeline_ps1_takes_log_dir_from_paths():
    """排程腳本不在 Python 掃描範圍內，另外守：log 目錄要從 paths.LOG_DIR 取，不寫死。"""
    text = (HERE / "run_pipeline.ps1").read_text(encoding="utf-8-sig")
    assert "paths.LOG_DIR" in text
    assert SPIKE_DIRNAME not in text


# ---- D5 過渡 fallback：整根目錄切換 ----

_WORK_DIR = """
import sys
sys.path.insert(0, sys.argv[1])
import paths
print(paths.WORK_DIR)
"""


def _work_dir(home: Path, *mkdirs: str, **extra_env: str) -> Path:
    home.mkdir(exist_ok=True)
    for rel in mkdirs:
        (home / rel).mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k != "LORE_VAULT_SPIKE_HOME"}
    env.update(HOME=str(home), USERPROFILE=str(home), **extra_env)
    proc = subprocess.run(
        [sys.executable, "-S", "-c", _WORK_DIR, str(HERE)],
        env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return Path(proc.stdout.strip())


def test_fresh_machine_uses_new_root(tmp_path):
    home = tmp_path / "home"
    assert _work_dir(home) == home / ".lore-vault"


def test_unmigrated_legacy_data_keeps_the_old_root(tmp_path):
    """舊位置有語料、新位置沒有：整根留在舊位置，Stop hook 寫入與管線讀取不分裂。"""
    home = tmp_path / "home"
    got = _work_dir(home, ".claude/agent-memory-spike/episodes", ".lore-vault/snapshot")
    assert got == home / ".claude" / "agent-memory-spike"


def test_new_root_wins_once_episodes_land(tmp_path):
    """episodes/ 搬到新位置就整根切過去，舊位置殘留也不會拉回去。"""
    home = tmp_path / "home"
    got = _work_dir(home, ".claude/agent-memory-spike/episodes", ".lore-vault/episodes")
    assert got == home / ".lore-vault"


def test_env_override_skips_fallback(tmp_path):
    home = tmp_path / "home"
    override = tmp_path / "vault-data"
    got = _work_dir(home, ".claude/agent-memory-spike/episodes",
                    LORE_VAULT_SPIKE_HOME=str(override))
    assert got == override
