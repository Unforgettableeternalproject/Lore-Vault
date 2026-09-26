"""資料路徑常數集中（T-10）的對帳測試。

守三件事：
1. 資料根目錄只在 ``paths.py`` 定義一次，其他模組不得再自己拼
   ``~/.claude/agent-memory-spike``——拼了就會在 D5 搬家時漏改。
2. 集中後各模組解析出的路徑與集中前逐一相同（值待 D5 定案才改）。
3. ``LORE_VAULT_SPIKE_HOME`` 覆寫會帶到所有衍生路徑。
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

# 集中前（T-10 動工前）各模組的路徑常數，相對於家目錄。
# 鍵集合也要一致：少了代表某個 monkeypatch 目標消失，多了代表新漏出一個名稱。
BEFORE = {
    "calibrate.DEFAULT_CONCEPT_PATH": ".claude/agent-memory-spike/concepts.json",
    "calibrate.DEFAULT_EPISODE_DIR": ".claude/agent-memory-spike/episodes",
    "calibrate.DEFAULT_INJECTION_PATH": ".claude/agent-memory-spike/injection_tasks.json",
    "calibrate.DEFAULT_PROBE_PATH": ".claude/agent-memory-spike/probe_tasks.json",
    "calibrate.WORK_DIR": ".claude/agent-memory-spike",
    "consolidate.DEFAULT_CONCEPT_PATH": ".claude/agent-memory-spike/concepts.json",
    "consolidate.DEFAULT_PAIR_PATH": ".claude/agent-memory-spike/consolidate_pairs.json",
    "consolidate.WORK_DIR": ".claude/agent-memory-spike",
    "distill.DEFAULT_CONCEPT_PATH": ".claude/agent-memory-spike/concepts.json",
    "distill.DEFAULT_EPISODE_DIR": ".claude/agent-memory-spike/episodes",
    "distill.DEFAULT_TASK_PATH": ".claude/agent-memory-spike/distill_tasks.json",
    "distill.DEFAULT_WATERMARK_PATH": ".claude/agent-memory-spike/distilled.json",
    "distill.WORK_DIR": ".claude/agent-memory-spike",
    "hook_health_alert.EPISODE_DIR": ".claude/agent-memory-spike/episodes",
    "hook_health_alert.INJECTION_LOG": ".claude/agent-memory-spike/injections.jsonl",
    "hook_health_alert.LOG_DIR": ".claude/agent-memory-spike/logs",
    "hook_health_alert.STATE_PATH": ".claude/agent-memory-spike/pipeline_state.json",
    "hook_health_alert.WORK_DIR": ".claude/agent-memory-spike",
    "hook_pretooluse.CONCEPT_PATH": ".claude/agent-memory-spike/concepts.json",
    "hook_pretooluse.INJECTION_LOG": ".claude/agent-memory-spike/injections.jsonl",
    "hook_pretooluse.STATE_DIR": ".claude/agent-memory-spike/inject_state",
    "hook_pretooluse.TOUCH_LOG": ".claude/agent-memory-spike/touches.jsonl",
    "hook_pretooluse.WORK_DIR": ".claude/agent-memory-spike",
    "hook_session_start.CONCEPT_PATH": ".claude/agent-memory-spike/concepts.json",
    "hook_session_start.INJECTION_LOG": ".claude/agent-memory-spike/injections.jsonl",
    "hook_stop.DEFAULT_EPISODE_DIR": ".claude/agent-memory-spike/episodes",
    "hook_userpromptsubmit.CONCEPT_PATH": ".claude/agent-memory-spike/concepts.json",
    "hook_userpromptsubmit.INJECTION_LOG": ".claude/agent-memory-spike/injections.jsonl",
    "pipeline.DEFAULT_EPISODE_DIR": ".claude/agent-memory-spike/episodes",
    "pipeline.LOCK_PATH": ".claude/agent-memory-spike/pipeline.lock",
    "pipeline.STATE_PATH": ".claude/agent-memory-spike/pipeline_state.json",
    "pipeline.WORK_DIR": ".claude/agent-memory-spike",
    # 階段 8 新增（不是集中前就有的常數；新名稱在此登記）
    "hook_pretooluse.CLIENT_ENV_PATH": ".claude/agent-memory-spike/client.env",
    "hook_stop.CLIENT_ENV_PATH": ".claude/agent-memory-spike/client.env",
    "pipeline.CLIENT_ENV_PATH": ".claude/agent-memory-spike/client.env",
    "pipeline.CONCEPT_PATH": ".claude/agent-memory-spike/concepts.json",
    "retrieve.CONTROL_CONCEPT_PATH": ".claude/agent-memory-spike/control_concepts.json",
    "retrieve.DEFAULT_CONCEPT_PATH": ".claude/agent-memory-spike/concepts.json",
    "retrieve.DEFAULT_EPISODE_DIR": ".claude/agent-memory-spike/episodes",
    "retrieve.WORK_DIR": ".claude/agent-memory-spike",
    "transcript.INJECTION_LOG": ".claude/agent-memory-spike/injections.jsonl",
    "transcript.TOUCH_LOG": ".claude/agent-memory-spike/touches.jsonl",
}

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
    """集中前後逐一相同；D5 真的改根目錄時這裡會紅，那是要的訊號。"""
    assert _dump(tmp_path) == BEFORE


def test_env_override_moves_every_derived_path(tmp_path):
    override = tmp_path / "vault-data"
    got = _dump(tmp_path, LORE_VAULT_SPIKE_HOME=str(override))
    # 覆寫根目錄本身就是 WORK_DIR，相對路徑只剩根目錄以下那段
    prefix = ".claude/agent-memory-spike"
    expected = {k: v[len(prefix):].lstrip("/") or "." for k, v in BEFORE.items()}
    assert got == expected


# ---- 「還有模組自己拼資料根目錄」的掃描 ----

SPIKE_DIRNAME = "agent-memory-spike"
_PATH_HINTS = ("Path(", "expanduser", "os.path.join", "Path.home")


def find_hardcoded_spike_paths(source: str) -> list[int]:
    """回傳疑似自行拼出資料根目錄的行號。

    註解與 docstring 不算（tokenize 只看 STRING token，且略過獨立成句的字串）；
    提示訊息、XML 屬性裡出現目錄名也不算——只抓兩種「拿來當路徑」的形狀：
    ``... / "agent-memory-spike"`` 這種路徑片段，以及同一行有 ``Path(``／
    ``Path.home``／``expanduser``／``os.path.join`` 又帶到目錄名的字串。
    """
    hits: list[int] = []
    lines = source.splitlines()
    prev_significant = None
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.STRING and SPIKE_DIRNAME in tok.string:
            is_docstring = prev_significant in (None, tokenize.NEWLINE, tokenize.INDENT,
                                                tokenize.DEDENT)
            value = tok.string.strip("rbuRBUfF").strip("'\"")
            line = lines[tok.start[0] - 1]
            if not is_docstring and (
                value == SPIKE_DIRNAME or any(h in line for h in _PATH_HINTS)
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

