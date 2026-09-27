"""Lore Vault MCP 客戶端安裝程式（只用 Python 標準庫）。

由服務主機的 `scripts/build_remote_kit.py` 放進 kit 資料夾，
與 pm skill 的 SKILL.md（完整殼模式另需 wheel）同目錄。
在要連線的機器上由人類執行（PowerShell 5.1／cmd／Git Bash 皆可）：

    python install.py --base-url https://vault.example.com
                                  互動安裝（詢問模式；服務位址必填）
    python install.py --mode http  HTTP 模式：只登記 `claude mcp add --transport http`
                                  與 pm skill，免裝 Python 套件
    python install.py --mode shell 完整殼模式：本機 venv＋stdio 殼
                                  （含降級快照、可傳本機路徑）
    python install.py --dry-run   只顯示會做什麼，不寫檔、不改設定
    python install.py --update    只重裝 wheel（完整殼模式；服務端新版上線後），
                                  之後 /mcp 重連
    python install.py --yes       非互動；token 由環境變數 LORE_VAULT_API_TOKEN 提供
    python install.py --rollback  還原 .bak-precutover（~/.claude.json、pm skill、
                                  ~/.claude/settings.json）
    python install.py --episodes  一併安裝 episode hook（D13；對話原文會推到服務）
    python install.py --no-episodes
                                  不安裝 episode hook（--yes 時的預設）

服務前面有 Cloudflare Access 時加 `--cf-access-env <檔案>`（含 CF_ACCESS_CLIENT_ID／
CF_ACCESS_CLIENT_SECRET 兩個鍵）；沒有就不需要。

episode hook（兩種模式都適用，與 MCP 傳輸無關）：kit 的 `hooks/` 複製到
`~/.lore-vault/hooks/`、寫 `~/.lore-vault/client.env`
（服務位址、token、選配 CF Access）、在 `~/.claude/settings.json` 合併登記
（以系統 Python 執行）。HTTP 模式只裝 Stop；完整殼另裝 PreToolUse
（讀殼同步的 concept 快照）。
流程與實測坑見 repo 的 docs/guides/REMOTE-INSTALL.md。
token 與 CF Access secret 永不列印、不寫 log、不進報告；
外部指令一律以 list 參數呼叫，不經 shell。重跑安全：已完成的步驟會略過。
"""

# 刻意維持舊版 Python 也能解析（不用 match、PEP 695），讓版本檢查訊息印得出來
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

INSTALLER_VERSION = "3"
MIN_PYTHON = (3, 12)

DEFAULT_TIMEOUT = 15.0
MODES = ("http", "shell")
MCP_PATH = "/mcp"
TOKEN_ENV = "LORE_VAULT_API_TOKEN"
CF_KEYS = ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET")
MCP_NAME = "lore-vault"
OLD_MCP_NAME = "open-notebook"
BACKUP_SUFFIX = ".bak-precutover"
WHEEL_GLOB = "lore_vault-*.whl"
SKILL_FILE = "SKILL.md"

# episode hook（D13）
HOOKS_DIR_NAME = "hooks"
HOOKS_MANIFEST = "VERSION.json"
CLIENT_ENV_NAME = "client.env"
URL_ENV = "LORE_VAULT_URL"
CONCEPT_SNAPSHOT_ENV = "LORE_VAULT_CONCEPT_SNAPSHOT"
# 殼把 concept 快照拉到 snapshot_dir 下的這個檔名（lore_vault.mcp.settings）
CONCEPT_SNAPSHOT_NAME = "concepts.json"
STOP_SCRIPT = "spike/hook_stop.py"
PRETOOLUSE_SCRIPT = "spike/hook_pretooluse.py"
PRETOOLUSE_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"
HOOK_TIMEOUT = 10
INGEST_DISABLED_CODE = "episode_ingest_disabled"
INGEST_DISABLED_HINT = (
    "服務未開啟收料（UI 設定頁可開）；episode 先存在本機 ~/.lore-vault/spool/，"
    "開啟後下次推送會自動補上"
)
# 印出版本、是否 venv 與實際執行檔；前綴用來從雜訊中挑出這一行
HOOK_PYTHON_MARKER = "LV_HOOK_PY "
HOOK_PYTHON_PROBE = (
    "import json, sys; print("
    + repr(HOOK_PYTHON_MARKER)
    + " + json.dumps({'version': list(sys.version_info[:3]),"
    " 'venv': sys.prefix != sys.base_prefix, 'executable': sys.executable}))"
)

_TOKEN_LINE = re.compile(r"^" + TOKEN_ENV + r"=.+$", re.MULTILINE)


class StepFailed(Exception):
    """某一步失敗：`message` 說明原因，`resume` 說明如何續跑。"""

    def __init__(self, message: str, resume: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.resume = resume


class Abort(Exception):
    """使用者取消。"""


# ── 路徑 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def claude_json(self) -> Path:
        return self.home / ".claude.json"

    @property
    def skill(self) -> Path:
        return self.home / ".claude" / "skills" / "pm" / SKILL_FILE

    @property
    def lv_dir(self) -> Path:
        return self.home / ".lore-vault"

    @property
    def venv(self) -> Path:
        return self.lv_dir / "venv"

    @property
    def venv_python(self) -> Path:
        if os.name == "nt":
            return self.venv / "Scripts" / "python.exe"
        return self.venv / "bin" / "python"

    @property
    def mcp_toml(self) -> Path:
        return self.lv_dir / "mcp.toml"

    @property
    def mcp_env(self) -> Path:
        return self.lv_dir / "mcp.env"

    @property
    def snapshot_dir(self) -> Path:
        return self.lv_dir / "snapshot"

    @property
    def concept_snapshot(self) -> Path:
        return self.snapshot_dir / CONCEPT_SNAPSHOT_NAME

    @property
    def settings_json(self) -> Path:
        return self.home / ".claude" / "settings.json"

    @property
    def hooks_dir(self) -> Path:
        return self.lv_dir / HOOKS_DIR_NAME

    @property
    def client_env(self) -> Path:
        return self.lv_dir / CLIENT_ENV_NAME

    @property
    def cf_env(self) -> Path:
        """CF Access 憑證檔的建議位置（選配；只在選用 CF Access 時才讀）。"""
        return self.home / ".cloudflared" / "pm-token.env"


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


# ── 外部指令 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], Result]


def subprocess_runner(argv: Sequence[str]) -> Result:
    """預設 runner：list 參數、不經 shell、以 UTF-8 解碼。"""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return Result(127, "", f"{type(exc).__name__}: {exc}")
    return Result(proc.returncode, proc.stdout or "", proc.stderr or "")


def find_exe(name: str) -> str | None:
    """找可執行檔；Windows 同時有 .exe 與 .cmd 時優先 .exe。

    .cmd 會經 cmd.exe 轉參數，引號規則不同。
    """
    if os.name == "nt":
        exe = shutil.which(name + ".exe")
        if exe:
            return exe
    return shutil.which(name)


# ── 純邏輯：內容與指令組裝 ─────────────────────────────────────────


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_mcp_toml(
    base_url: str,
    *,
    cf_access_env_file: str | None = None,
    snapshot_dir: str = "~/.lore-vault/snapshot",
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """`mcp.toml` 內容（不含任何密鑰；殼的設定載入會拒絕 token／secret 類的鍵）。

    `cf_access_env_file` 為 None 時不寫這個鍵：殼遇到指向不存在檔案的鍵會拒絕啟動。
    """
    lines = ["[mcp]", f"base_url = {_toml_str(base_url.rstrip('/'))}"]
    if cf_access_env_file:
        lines.append(f"cf_access_env_file = {_toml_str(cf_access_env_file)}")
    lines.append(f"snapshot_dir = {_toml_str(snapshot_dir)}")
    lines.append(f"timeout = {float(timeout)!r}")
    return "\n".join(lines) + "\n"


def normalize_base_url(value: str) -> str:
    """服務位址：去掉尾端 `/` 與誤貼的 `/mcp`；只接受 http(s) 且有主機名。"""
    url = (value or "").strip().rstrip("/")
    if url.endswith(MCP_PATH):
        url = url[: -len(MCP_PATH)].rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise StepFailed(
            f"服務位址格式不對：{value!r}",
            "填完整網址，例如 https://vault.example.com 或 http://127.0.0.1:5056",
        )
    return url


def is_plaintext_remote(url: str) -> bool:
    """http:// 且不是本機：token 會以明文經過網路。"""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1")


def mcp_http_url(base_url: str) -> str:
    return base_url.rstrip("/") + MCP_PATH


def render_mcp_env(token: str) -> bytes:
    """`mcp.env` 內容：UTF-8 無 BOM、單行。"""
    validate_token(token)
    return (TOKEN_ENV + "=" + token + "\n").encode("utf-8")


def validate_token(token: str) -> None:
    if not token or not token.strip():
        raise StepFailed("token 是空的")
    if token != token.strip() or any(c in token for c in "\r\n\x00"):
        raise StepFailed("token 含空白或換行，請確認只貼上值本身")


def env_file_has_token(path: Path) -> bool:
    """`mcp.env` 是否已有非空的 token 行（只判斷有無，不回傳值）。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if raw.startswith(b"\xef\xbb\xbf") or raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        # BOM 或 UTF-16：殼讀不到鍵名，視為無效
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return bool(_TOKEN_LINE.search(text.replace("\r\n", "\n")))


def cf_env_keys_present(path: Path) -> dict[str, bool]:
    """`pm-token.env` 內兩個 CF 鍵是否存在（只看鍵名，不保留值）。"""
    found = dict.fromkeys(CF_KEYS, False)
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return found
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        for key in CF_KEYS:
            if stripped.startswith(key + "=") and len(stripped) > len(key) + 1:
                found[key] = True
    return found


def read_cf_values(path: Path) -> dict[str, str]:
    """HTTP 模式要把 CF Access 憑證放進 header，才讀出值；呼叫端負責遮蔽。"""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        for key in CF_KEYS:
            if stripped.startswith(key + "="):
                value = stripped[len(key) + 1 :].strip().strip("'\"")
                if value:
                    values[key] = value
    return values


def cf_headers(values: dict[str, str]) -> dict[str, str]:
    return {
        "CF-Access-Client-Id": values["CF_ACCESS_CLIENT_ID"],
        "CF-Access-Client-Secret": values["CF_ACCESS_CLIENT_SECRET"],
    }


def venv_argv(uv: str, paths: Paths, python: str) -> list[str]:
    return [uv, "venv", str(paths.venv), "--python", python]


def pip_install_argv(uv: str, paths: Paths, wheel: Path) -> list[str]:
    # 版本號未變時必須 --reinstall，否則不會覆蓋
    return [
        uv,
        "pip",
        "install",
        "--reinstall",
        "--python",
        str(paths.venv_python),
        str(wheel),
    ]


def import_check_argv(paths: Paths) -> list[str]:
    return [str(paths.venv_python), "-c", "import lore_vault.mcp; print('ok')"]


def mcp_get_argv(claude: str, name: str) -> list[str]:
    return [claude, "mcp", "get", name]


def mcp_remove_argv(claude: str, name: str, scope: str) -> list[str]:
    return [claude, "mcp", "remove", name, "-s", scope]


def mcp_add_argv(claude: str, paths: Paths) -> list[str]:
    # `--` 之後才是殼的指令；不用 add-json（PS5.1 會吃掉 JSON 雙引號）
    return [
        claude,
        "mcp",
        "add",
        MCP_NAME,
        "-s",
        "user",
        "--",
        str(paths.venv_python),
        "-m",
        "lore_vault.mcp",
        "--config",
        str(paths.mcp_toml),
        "--env-file",
        str(paths.mcp_env),
    ]


def mcp_add_http_argv(claude: str, url: str, headers: dict[str, str]) -> list[str]:
    """HTTP 模式：`claude mcp add --transport http -s user lore-vault <url> -H ...`。

    `--header` 是可變長度選項，會吞掉後面的位置參數，所以一定放在最後。
    token 會存進 ~/.claude.json（Claude Code 的設計），這是 HTTP 模式的取捨。
    """
    argv = [claude, "mcp", "add", "--transport", "http", "-s", "user", MCP_NAME, url]
    for name, value in headers.items():
        argv += ["--header", f"{name}: {value}"]
    return argv


def parse_mcp_scope(get_output: str) -> str | None:
    """從 `claude mcp get` 輸出取 scope：user／local／project；認不出回 None。"""
    for line in get_output.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("scope:"):
            value = stripped.split(":", 1)[1].strip().lower()
            for scope in ("user", "local", "project"):
                if value.startswith(scope):
                    return scope
    return None


def find_mcp_list_entry(list_output: str, name: str = MCP_NAME) -> str | None:
    for line in list_output.splitlines():
        if line.strip().startswith(name + ":"):
            return line.strip()
    return None


def skill_diff_summary(old: str | None, new: str) -> list[str]:
    """SKILL.md 差異摘要（給人確認用；不輸出完整 diff）。"""
    lines: list[str] = []
    if old is None:
        lines.append("目前沒有 pm skill，將新建。")
    elif old == new:
        return ["內容相同，不需覆寫。"]
    else:
        old_lines = old.splitlines()
        new_lines = new.splitlines()
        added = removed = 0
        for d in difflib.ndiff(old_lines, new_lines):
            if d.startswith("+ "):
                added += 1
            elif d.startswith("- "):
                removed += 1
        lines.append(f"新增 {added} 行、刪除 {removed} 行。")
        old_tools = _allowed_tools(old)
        new_tools = _allowed_tools(new)
        if old_tools != new_tools:
            lines.append(f"allowed-tools 舊：{old_tools or '（無）'}")
            lines.append(f"allowed-tools 新：{new_tools or '（無）'}")
        if "mcp__open-notebook__" in old:
            lines.append(
                "舊版仍呼叫 mcp__open-notebook__*，新版改用 mcp__lore-vault__*。"
            )
    return lines


def _allowed_tools(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("allowed-tools:"):
            return line.split(":", 1)[1].strip()
    return ""


def check_skill_content(text: str) -> list[str]:
    """kit 內 SKILL.md 的機器中立性快檢；回傳問題清單（空＝通過）。"""
    problems = []
    tools = _allowed_tools(text)
    if "mcp__lore-vault__" not in tools:
        problems.append("allowed-tools 沒有 mcp__lore-vault__* 工具")
    if "mcp__open-notebook__" in text:
        problems.append("仍引用 mcp__open-notebook__*")
    if re.search(r"[A-Za-z]:[\\/]Users[\\/]", text):
        problems.append("含寫死的使用者路徑")
    return problems


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_wheel(kit_dir: Path) -> Path:
    wheels = sorted(kit_dir.glob(WHEEL_GLOB))
    if not wheels:
        raise StepFailed(
            f"kit 資料夾找不到 {WHEEL_GLOB}：{kit_dir}",
            "確認 install.py 與 wheel 在同一資料夾，或用 --kit-dir 指定",
        )
    if len(wheels) > 1:
        names = "、".join(w.name for w in wheels)
        raise StepFailed(
            f"kit 資料夾有多個 wheel（{names}）", "只留要安裝的那一個再重跑"
        )
    return wheels[0]


# ── episode hook：純邏輯 ─────────────────────────────────────────────


def read_env_token(path: Path) -> str | None:
    """`mcp.env` 的 token 值（完整殼沿用既有 token 時用）；讀不到回 None。"""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(TOKEN_ENV + "="):
            value = stripped[len(TOKEN_ENV) + 1 :].strip()
            return value or None
    return None


def render_client_env(
    base_url: str,
    token: str,
    cf: dict[str, str] | None = None,
    concept_snapshot: Path | None = None,
) -> bytes:
    """hook 端 `client.env`（鍵見 lore_vault.hooks.client_env）：UTF-8 無 BOM。

    CF Access 兩個鍵要一起寫（只有一個時 hook 視為設定不完整、不推送）。
    `concept_snapshot` 只在完整殼模式給：PreToolUse 讀殼同步下來的快照。
    """
    validate_token(token)
    lines = [
        "# Lore Vault episode hook 設定（install.py 產生；含密鑰，勿外傳）",
        f"{URL_ENV}={base_url.rstrip('/')}",
        f"{TOKEN_ENV}={token}",
    ]
    if cf:
        lines += [f"{key}={cf[key]}" for key in CF_KEYS]
    if concept_snapshot is not None:
        lines.append(f"{CONCEPT_SNAPSHOT_ENV}={concept_snapshot.as_posix()}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def hook_specs(mode: str) -> list[tuple[str, str, str]]:
    """要登記的 hook：(事件, matcher, kit hooks/ 內的腳本)。

    HTTP 模式只裝 Stop：PreToolUse 每次編輯都跑、只讀本地 concept 快照，而 HTTP 模式
    沒有殼替它同步快照；讓 hook 自己連網會拖慢每次編輯。完整殼的快照由殼定期拉取。
    """
    specs = [("Stop", "", STOP_SCRIPT)]
    if mode == "shell":
        specs.append(("PreToolUse", PRETOOLUSE_MATCHER, PRETOOLUSE_SCRIPT))
    return specs


def hook_entry(python: str, script: Path) -> dict:
    """與服務主機現行登記同一格式：command＝Python 絕對路徑，args＝腳本。"""
    return {
        "type": "command",
        "command": python,
        "args": [str(script)],
        "timeout": HOOK_TIMEOUT,
    }


def _norm_path_text(text: str) -> str:
    text = text.replace("\\", "/")
    return text.lower() if os.name == "nt" else text


def _hook_texts(hook: dict) -> list[str]:
    texts = [hook.get("command")]
    args = hook.get("args")
    if isinstance(args, list):
        texts += args
    return [t for t in texts if isinstance(t, str)]


def hook_points_under(hook: object, root: Path) -> bool:
    """這筆 hook 的 command／args 是否指向 `root` 底下（＝本安裝器登記的）。"""
    if not isinstance(hook, dict):
        return False
    prefix = _norm_path_text(str(root)).rstrip("/") + "/"
    return any(prefix in _norm_path_text(t) for t in _hook_texts(hook))


def _iter_hooks(hooks: dict):
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                for hook in group["hooks"]:
                    yield event, hook


def foreign_registrations(hooks: dict, root: Path, script_name: str) -> list[str]:
    """`root` 以外、同名腳本的既有登記（例如服務主機本機的 repo 路徑）。

    同一個 hook 登記兩份會讓每輪推兩次、注入加倍，所以遇到就不重複登記。
    """
    found = []
    for event, hook in _iter_hooks(hooks):
        if hook_points_under(hook, root):
            continue
        if isinstance(hook, dict) and any(
            ("/" + script_name) in ("/" + _norm_path_text(t)) for t in _hook_texts(hook)
        ):
            found.append(event)
    return found


def remove_hooks_under(hooks: dict, root: Path) -> dict:
    """移除指向 `root` 的 hook；只含這些 hook 的 matcher 群組與事件一併移除，
    使用者其他 hook 原樣保留。回傳新 dict，不改傳入物件。"""
    result: dict = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            result[event] = groups
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = [h for h in group["hooks"] if not hook_points_under(h, root)]
            if len(kept) == len(group["hooks"]):
                kept_groups.append(group)
            elif kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups or not groups:
            result[event] = kept_groups
    return result


def merge_hooks(
    settings: dict, root: Path, wanted: list[tuple[str, str, dict]]
) -> dict:
    """先移除本安裝器先前的登記再附加 `wanted`。

    重跑不會重複，Python 路徑變了也會更新。"""
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise StepFailed(
            "~/.claude/settings.json 的 hooks 不是物件", "手動修正該檔後重跑"
        )
    merged = remove_hooks_under(hooks, root)
    for event, matcher, entry in wanted:
        groups = merged.setdefault(event, [])
        if not isinstance(groups, list):
            raise StepFailed(
                f"~/.claude/settings.json 的 hooks.{event} 不是陣列",
                "手動修正該檔後重跑",
            )
        groups.append({"matcher": matcher, "hooks": [entry]})
    out = dict(settings)
    if merged:
        out["hooks"] = merged
    else:
        out.pop("hooks", None)
    return out


def load_settings_json(path: Path) -> dict:
    """讀 `~/.claude/settings.json`；不存在回 {}；解析失敗就停，絕不覆寫。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise StepFailed(
            f"讀不懂 {path}（{type(exc).__name__}），不會覆寫",
            "修正成合法 JSON 後重跑，或加 --no-episodes 跳過 hook",
        ) from None
    if not isinstance(data, dict):
        raise StepFailed(f"{path} 不是 JSON 物件，不會覆寫", "手動修正後重跑")
    return data


def dump_settings_json(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def load_hooks_manifest(hooks_dir: Path) -> dict:
    """讀 kit／已安裝 hooks 的 VERSION.json 並逐檔驗 sha256。"""
    path = hooks_dir / HOOKS_MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StepFailed(
            f"讀不到 {path}（{type(exc).__name__}）",
            "向主機重取含 hooks/ 的 kit，或加 --no-episodes 跳過",
        ) from None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files:
        raise StepFailed(f"{path} 格式不符", "向主機重取 kit")
    for rel, digest in files.items():
        parts = Path(rel).parts
        if Path(rel).is_absolute() or ".." in parts:
            raise StepFailed(f"{path} 含不合法路徑：{rel}", "向主機重取 kit")
        target = hooks_dir / rel
        if not target.is_file() or sha256_file(target) != digest:
            raise StepFailed(
                f"hook 檔案缺漏或內容不符：{rel}", "向主機重取 kit（不要手改 hooks/）"
            )
    for script in (STOP_SCRIPT, PRETOOLUSE_SCRIPT):
        if script not in files:
            raise StepFailed(f"kit 的 hooks/ 缺 {script}", "向主機重取 kit")
    return manifest


def parse_hook_python_probe(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        if line.startswith(HOOK_PYTHON_MARKER):
            try:
                data = json.loads(line[len(HOOK_PYTHON_MARKER) :])
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


# ── 本機自檢（在 venv python 內執行，只印一行整理過的 JSON）──────────

SELF_CHECK_CODE = r"""
import asyncio, json, logging, sys
logging.disable(logging.CRITICAL)
out = {"category": None, "message": None}
try:
    from lore_vault.storage.migrate import SCHEMA_VERSION
    out["wheel_schema"] = SCHEMA_VERSION
except Exception:
    out["wheel_schema"] = None
from lore_vault.config import ConfigError
from lore_vault.mcp.client import ServiceClient, ServiceError, ServiceUnreachable
from lore_vault.mcp.settings import load_shell_settings

def classify_unreachable(detail):
    low = detail.lower()
    if "ssl" in low or "certificate" in low or "tls" in low:
        return "tls"
    if "timeout" in low:
        return "timeout"
    if "getaddrinfo" in low or "name or service" in low or "nodename" in low:
        return "dns"
    if "connect" in low:
        return "connect"
    return "unreachable"

async def main():
    try:
        settings = load_shell_settings(config_path=sys.argv[1], env_file=sys.argv[2])
    except ConfigError as exc:
        out.update(category="config", message=str(exc))
        return
    out["cf_access"] = settings.cf_access is not None
    client = ServiceClient(settings)
    try:
        # 無 body 才是純健康檢查；帶 {} 會被服務端要求 space（400 space_required）
        body = await client.post("/v1/status", None)
    except ServiceUnreachable as exc:
        out.update(category=classify_unreachable(exc.detail), message=exc.detail)
        return
    except ServiceError as exc:
        if 300 <= exc.status < 400 or exc.status == 403:
            cat = "cf_access"
        elif exc.status == 401:
            cat = "bearer"
        else:
            cat = "service"
        out.update(category=cat, message=exc.message, http_status=exc.status)
        return
    finally:
        await client.aclose()
    schema = body.get("schema") or {}
    doctor = body.get("doctor") or {}
    fails = [
        c.get("name") for c in doctor.get("checks") or []
        if c.get("status") == "fail"
    ]
    out.update(
        category="ok" if body.get("ok") else "service",
        ok=bool(body.get("ok")),
        schema_version=schema.get("version"),
        schema_expected=schema.get("expected"),
        doctor=doctor.get("summary"),
        doctor_fails=fails,
    )

try:
    asyncio.run(main())
except Exception as exc:
    out.update(category="internal", message=type(exc).__name__)
print(json.dumps(out, ensure_ascii=True))
"""

CATEGORY_HINTS = {
    "ok": "服務正常",
    "config": "設定錯誤：檢查 mcp.toml 與 mcp.env（token 鍵是否存在）",
    "dns": "DNS 解析失敗：檢查網路與 base_url 網域",
    "connect": "連不上服務：檢查網路、防火牆與 base_url",
    "tls": "TLS 錯誤：檢查系統時間、代理或憑證攔截",
    "timeout": "逾時：服務或 tunnel 可能忙碌，稍後重跑自檢",
    "unreachable": (
        "服務不可達（502/503/504/530 等）：主機服務或 tunnel 可能停了，請主機檢查"
    ),
    "cf_access": (
        "被轉址或拒絕（3xx/403）：服務前面有 Cloudflare Access 時，"
        "用 --cf-access-env 指定含兩個 CF 鍵的檔案；沒有的話檢查 base_url"
    ),
    "bearer": ("bearer 認證失敗（401）：token 與服務端不一致，重跑並重新輸入 token"),
    "service": "服務回報錯誤或 doctor 有 fail：把報告交給主機判斷",
    "internal": "自檢程式本身出錯：把報告交給主機",
}


def self_check_argv(paths: Paths) -> list[str]:
    return [
        str(paths.venv_python),
        "-c",
        SELF_CHECK_CODE,
        str(paths.mcp_toml),
        str(paths.mcp_env),
    ]


def parse_self_check(stdout: str) -> dict:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return {"category": "internal", "message": "自檢沒有輸出結果"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟隨轉址：CF Access 會把未授權請求轉到登入頁，跟過去會拿到 200 的 HTML。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _classify_unreachable(detail: str) -> str:
    low = detail.lower()
    if "ssl" in low or "certificate" in low or "tls" in low:
        return "tls"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "getaddrinfo" in low or "name or service" in low or "nodename" in low:
        return "dns"
    if "connect" in low or "refused" in low:
        return "connect"
    return "unreachable"


def _error_message(raw: bytes) -> str:
    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")
    return ""


def http_self_check(
    base_url: str, headers: dict[str, str], timeout: float = DEFAULT_TIMEOUT
) -> dict:
    """HTTP 模式的自檢：直接 POST `/v1/status`（不經殼），分類同殼的自檢。

    送無 body 的 POST：服務端只把無 body 視為純健康檢查，帶 `{}` 會回
    400 `space_required`。回傳內容不含請求 header；呼叫端仍以 UI.redact 遮蔽。
    """
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/status",
        data=None,
        method="POST",
        headers=dict(headers),
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        message = _error_message(exc.read() or b"") or f"HTTP {status}"
        if 300 <= status < 400 or status == 403:
            cat = "cf_access"
        elif status == 401:
            cat = "bearer"
        elif status in (502, 503, 504, 530):
            cat = "unreachable"
        else:
            cat = "service"
        return {"category": cat, "message": message, "http_status": status}
    except urllib.error.URLError as exc:
        detail = f"{type(exc.reason).__name__}: {exc.reason}"
        return {"category": _classify_unreachable(detail), "message": detail}
    except (TimeoutError, OSError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        return {"category": _classify_unreachable(detail), "message": detail}
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {
            "category": "service",
            "message": "回應不是 JSON（base_url 指到別的服務？）",
        }
    if not isinstance(body, dict):
        return {"category": "service", "message": "回應格式不符"}
    schema = body.get("schema") or {}
    doctor = body.get("doctor") or {}
    fails = [
        c.get("name")
        for c in doctor.get("checks") or []
        if isinstance(c, dict) and c.get("status") == "fail"
    ]
    return {
        "category": "ok" if body.get("ok") else "service",
        "ok": bool(body.get("ok")),
        "schema_version": schema.get("version"),
        "schema_expected": schema.get("expected"),
        "doctor": doctor.get("summary"),
        "doctor_fails": fails,
    }


def _error_code(raw: bytes) -> str | None:
    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        code = body["error"].get("code")
        return code if isinstance(code, str) else None
    return None


def episode_ingest_check(
    base_url: str, headers: dict[str, str], timeout: float = DEFAULT_TIMEOUT
) -> dict:
    """服務收料開關檢查：送**空批次** `POST /v1/episodes {"episodes": []}`。

    服務先看開關（關閉＝403 `episode_ingest_disabled`），開啟時空批次回 200、
    各項計數為 0；兩種情況都不寫任何資料，不會在正式服務留下假 episode。
    category：ok／disabled，其餘同 `http_self_check` 的分類。
    """
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/episodes",
        data=json.dumps({"episodes": []}).encode("utf-8"),
        method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read() or b""
        code = _error_code(body)
        if status == 403 and code == INGEST_DISABLED_CODE:
            return {"category": "disabled", "http_status": status}
        message = _error_message(body) or f"HTTP {status}"
        if 300 <= status < 400 or status == 403:
            cat = "cf_access"
        elif status == 401:
            cat = "bearer"
        elif status in (502, 503, 504, 530):
            cat = "unreachable"
        else:
            cat = "service"
        return {"category": cat, "message": message, "http_status": status}
    except urllib.error.URLError as exc:
        detail = f"{type(exc.reason).__name__}: {exc.reason}"
        return {"category": _classify_unreachable(detail), "message": detail}
    except (TimeoutError, OSError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        return {"category": _classify_unreachable(detail), "message": detail}
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        body = None
    if isinstance(body, dict) and "accepted" in body:
        return {"category": "ok"}
    return {"category": "service", "message": "回應格式不符（base_url 指到別的服務？）"}


# ── 互動與輸出 ──────────────────────────────────────────────────────


@dataclass
class StepRecord:
    name: str
    status: str  # ok／skip／fail／dry
    detail: str = ""


class UI:
    """純文字輸出與提問；所有輸出經 redact()，已知密鑰不會出現在畫面上。"""

    def __init__(
        self,
        *,
        assume_yes: bool = False,
        input_fn: Callable[[str], str] = input,
        secret_fn: Callable[[str], str] = getpass.getpass,
        out: Callable[[str], None] | None = None,
    ) -> None:
        self.assume_yes = assume_yes
        self._input = input_fn
        self._secret = secret_fn
        self._out = out or _print
        self._secrets: list[str] = []

    def add_secret(self, value: str) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def say(self, text: str = "") -> None:
        self._out(self.redact(text))

    def header(self, index: int, total: int, title: str) -> None:
        self.say("")
        self.say(f"[{index}/{total}] {title}")

    def confirm(self, question: str, default: bool = True) -> bool:
        if self.assume_yes:
            return default
        suffix = " [Y/n] " if default else " [y/N] "
        while True:
            answer = self._input(question + suffix).strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes", "是"):
                return True
            if answer in ("n", "no", "否"):
                return False

    def choose(
        self, question: str, options: list[tuple[str, str]], default: str
    ) -> str:
        """options：(鍵, 說明)；回傳鍵。"""
        if self.assume_yes:
            return default
        keys = [k for k, _ in options]
        for key, label in options:
            mark = "（預設）" if key == default else ""
            self.say(f"  {key}) {label}{mark}")
        while True:
            answer = self._input(question + " ").strip().lower() or default
            if answer in keys:
                return answer

    def ask(self, question: str, default: str) -> str:
        if self.assume_yes:
            return default
        answer = self._input(f"{question} [{default}] ").strip()
        return answer or default

    def ask_required(self, question: str) -> str:
        """沒有預設值的必填問題；非互動模式回空字串，由呼叫端決定如何處理。"""
        if self.assume_yes:
            return ""
        while True:
            answer = self._input(f"{question}：").strip()
            if answer:
                return answer

    def secret(self, prompt: str) -> str:
        value = self._secret(prompt)
        self.add_secret(value.strip())
        return value.strip()


def _print(text: str) -> None:
    print(text, flush=True)


# ── 安裝流程 ────────────────────────────────────────────────────────


@dataclass
class Env:
    python_ok: bool
    python_version: str
    uv: str | None
    uv_version: str
    claude: str | None
    claude_version: str
    # 只在指定 CF Access 憑證檔時才檢查；未使用 CF Access 時為 False／{}
    cf_env_exists: bool
    cf_keys: dict[str, bool]


@dataclass
class Installer:
    paths: Paths
    kit_dir: Path
    ui: UI
    runner: Runner = subprocess_runner
    dry_run: bool = False
    mask_user: bool = True
    base_url: str | None = None
    # http：免殼，只登記 `claude mcp add --transport http`；shell：本機 venv＋stdio 殼；
    # auto：互動時詢問，非互動時有既有 mcp.toml 就沿用 shell，否則 http
    mode: str = "shell"
    # CF Access 憑證檔（選配）；None 且互動時會詢問是否使用
    cf_env_file: Path | None = None
    http_check: Callable[[str, dict[str, str], float], dict] = http_self_check
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    python_exe: str = sys.executable
    python_version: tuple[int, ...] = tuple(sys.version_info[:3])
    which: Callable[[str], str | None] = find_exe
    records: list[StepRecord] = field(default_factory=list)
    env: Env | None = None
    wheel: Path | None = None
    wheel_sha256: str | None = None
    status: dict | None = None
    mcp_entry: str | None = None
    report_path: Path | None = None
    # 是否已決定 CF Access（避免同一次安裝重複詢問）
    cf_decided: bool = False
    # HTTP 模式的請求 header（含 token；只在記憶體）
    http_headers: dict[str, str] | None = None
    # 本次取得的 token（只在記憶體；寫 client.env 用）
    token: str | None = None
    # episode hook：True 安裝、False 不裝、None＝互動時詢問（--yes 時不裝）
    episodes: bool | None = None
    # 指定 hook 用的 Python（--hook-python）；None＝自動找系統 Python
    hook_python: str | None = None
    # 執行本安裝器的 Python 是否在 venv 裡（venv 的 Python 不當 hook 用）
    python_in_venv: bool = sys.prefix != sys.base_prefix
    # venv 背後的基底 Python（Windows 有 sys._base_executable）
    base_python: str | None = getattr(sys, "_base_executable", None)
    episode_check: Callable[[str, dict[str, str], float], dict] = episode_ingest_check
    hook_python_resolved: str | None = None
    hook_python_version: str = ""
    hooks_manifest: dict | None = None
    hook_events: list[str] = field(default_factory=list)
    episode_status: dict | None = None

    # ── 共用 ──

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.records.append(StepRecord(name, status, detail))
        mark = {"ok": "[OK]", "skip": "[SKIP]", "fail": "[FAIL]", "dry": "[DRY]"}[
            status
        ]
        self.ui.say(f"  {mark} {name}" + (f"：{detail}" if detail else ""))

    def run(self, argv: Sequence[str], what: str) -> Result:
        result = self.runner(argv)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
            raise StepFailed(
                f"{what} 失敗（exit {result.returncode}）"
                + ("：\n    " + "\n    ".join(tail) if tail else ""),
            )
        return result

    def mask(self, text: str) -> str:
        if not self.mask_user:
            return text
        home = str(self.paths.home)
        for variant in {home, home.replace("\\", "/")}:
            text = text.replace(variant, "~")
        return text

    def write_bytes(self, path: Path, data: bytes, *, private: bool = False) -> None:
        """原子寫入。`private`：含密鑰的檔案，POSIX 上建立時就是 0600
        （Windows 沿用家目錄的 ACL）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        if private:
            tmp.unlink(missing_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            if os.name != "nt":
                os.chmod(tmp, 0o600)
        else:
            tmp.write_bytes(data)
        os.replace(tmp, path)

    # ── 步驟 ──

    def detect(self) -> Env:
        py_ok = tuple(self.python_version[:2]) >= MIN_PYTHON
        uv = self.which("uv")
        claude = self.which("claude")
        uv_version = claude_version = ""
        if uv:
            r = self.runner([uv, "--version"])
            uv_version = r.stdout.strip() if r.returncode == 0 else "（無法取得版本）"
        if claude:
            r = self.runner([claude, "--version"])
            claude_version = (
                r.stdout.strip() if r.returncode == 0 else "（無法取得版本）"
            )
        cf_file = self.cf_env_file
        cf_exists = cf_file is not None and cf_file.is_file()
        env = Env(
            python_ok=py_ok,
            python_version=".".join(str(p) for p in self.python_version),
            uv=uv,
            uv_version=uv_version,
            claude=claude,
            claude_version=claude_version,
            cf_env_exists=cf_exists,
            cf_keys=cf_env_keys_present(cf_file) if cf_file and cf_exists else {},
        )
        self.env = env
        return env

    def show_env(self, env: Env) -> list[str]:
        """印出偵測結果；回傳阻擋安裝的問題。"""
        problems = []
        ok = "[OK]  "
        ng = "[FAIL]"
        self.ui.say(f"  {ok if env.python_ok else ng} Python {env.python_version}")
        if not env.python_ok:
            problems.append("需要 Python ≥ 3.12")
        if self.mode == "shell":
            self.ui.say(f"  {ok if env.uv else ng} uv {env.uv_version or '（找不到）'}")
            if not env.uv:
                problems.append(
                    "找不到 uv（https://docs.astral.sh/uv/ 安裝後重開終端機）"
                )
        self.ui.say(
            f"  {ok if env.claude else ng} claude CLI "
            + (env.claude_version or "（找不到）")
        )
        if not env.claude:
            problems.append("找不到 claude CLI（Claude Code）")
        if self.cf_env_file is None:
            self.ui.say("  [--]   Cloudflare Access：未指定（選配）")
            return problems
        cf_name = self.mask(str(self.cf_env_file))
        cf_ok = env.cf_env_exists and all(env.cf_keys.values())
        self.ui.say(
            f"  {ok if cf_ok else ng} Cloudflare Access：{cf_name}"
            + ("" if env.cf_env_exists else "（不存在）")
        )
        problems += self._cf_problems(env.cf_env_exists, env.cf_keys)
        return problems

    def _cf_problems(self, exists: bool, keys: dict[str, bool]) -> list[str]:
        name = self.mask(str(self.cf_env_file))
        if not exists:
            return [f"CF Access 憑證檔不存在：{name}"]
        missing = [k for k, v in keys.items() if not v]
        if missing:
            return [f"{name} 缺少鍵：" + "、".join(missing)]
        return []

    def plan(self, full: bool) -> list[str]:
        p = self.paths
        lines = [f"kit：{self.kit_dir}"]
        if full and self.mode == "http":
            target = self.base_url or "（安裝時詢問）"
            lines += [
                "模式：HTTP（免殼，不安裝 Python 套件）",
                f"1. 備份 {p.claude_json} 與 {p.skill}"
                f"（已有 {BACKUP_SUFFIX} 就不覆蓋）",
                f"2. 服務位址 {target}、token（不回顯輸入）、Cloudflare Access（選配）",
                "3. 自檢：直接呼叫一次 /v1/status（bearer＋CF Access）；失敗就停下",
                f"4. claude mcp：移除 {OLD_MCP_NAME}（若有）、以 --transport http "
                f"重新登記 {MCP_NAME}（user scope，token 存於 ~/.claude.json）",
                f"5. 覆寫 {p.skill}（先顯示差異摘要）",
                "6. 產生驗證報告（不含密鑰）",
            ]
            return lines + self._episode_plan()
        lines.append(f"wheel：{self.wheel.name if self.wheel else '？'}")
        if full:
            lines.append("模式：完整殼（本機 venv＋stdio 殼）")
            lines += [
                f"1. 備份 {p.claude_json} 與 {p.skill}"
                f"（已有 {BACKUP_SUFFIX} 就不覆蓋）",
                f"2. 建 venv {p.venv}（已存在就沿用）",
                "3. uv pip install --reinstall 安裝 kit 的 wheel",
                f"4. 寫 {p.mcp_toml}（已存在會詢問）",
                f"5. 寫 {p.mcp_env}（token 以不回顯方式輸入；已存在可保留）",
                f"6. claude mcp：移除 {OLD_MCP_NAME}（若有）、"
                f"重新登記 {MCP_NAME}（user scope）",
                f"7. 覆寫 {p.skill}（先顯示差異摘要）",
                "8. 本機自檢：經殼（bearer＋CF Access，若有）呼叫一次 status",
                "9. 產生驗證報告（不含密鑰）",
            ]
            lines += self._episode_plan()
        else:
            lines += [
                "1. uv pip install --reinstall 重裝 wheel（venv 必須已存在）",
                "2. 本機自檢",
                "3. 產生驗證報告；之後在 Claude Code 用 /mcp 重連 lore-vault",
            ]
        return lines

    def _episode_plan(self) -> list[str]:
        if not self.episodes:
            return ["＋ episode hook：不安裝（加 --episodes 可安裝）"]
        p = self.paths
        events = "、".join(event for event, _, _ in hook_specs(self.mode))
        return [
            f"＋ episode hook：複製到 {p.hooks_dir}、寫 {p.client_env}（含 token）",
            f"  合併登記 {events} 到 {p.settings_json}（先備份 {BACKUP_SUFFIX}，"
            "不動其他 hook）",
            "  以空批次檢查服務收料開關（不寫入任何資料）",
        ]

    def step_backup(self) -> None:
        for path in (self.paths.claude_json, self.paths.skill):
            bak = backup_path(path)
            if bak.exists():
                self.record(f"備份 {path.name}", "skip", f"{bak.name} 已存在，沿用")
            elif not path.exists():
                self.record(f"備份 {path.name}", "skip", "原檔不存在，無需備份")
            elif self.dry_run:
                self.record(f"備份 {path.name}", "dry", f"會複製為 {bak.name}")
            else:
                shutil.copy2(path, bak)
                self.record(f"備份 {path.name}", "ok", str(bak))

    def step_venv(self) -> None:
        if self.paths.venv_python.exists():
            self.record("建 venv", "skip", "已存在，沿用")
            return
        argv = venv_argv(self._uv(), self.paths, self.python_exe)
        if self.dry_run:
            self.record("建 venv", "dry", " ".join(argv))
            return
        self.run(argv, "uv venv")
        self.record("建 venv", "ok", str(self.paths.venv))

    def step_install_wheel(self) -> None:
        assert self.wheel is not None
        argv = pip_install_argv(self._uv(), self.paths, self.wheel)
        if self.dry_run:
            self.record("安裝 wheel", "dry", " ".join(argv))
            return
        if not self.paths.venv_python.exists():
            raise StepFailed(
                f"venv 不存在：{self.paths.venv}",
                "--update 只適用完整殼模式：先跑完整安裝（不加 --update）；"
                "HTTP 模式不需要更新客戶端，服務端升級後在 /mcp 重連即可",
            )
        self.run(argv, "uv pip install")
        r = self.runner(import_check_argv(self.paths))
        if r.returncode != 0 or "ok" not in r.stdout:
            raise StepFailed("安裝後 import lore_vault.mcp 失敗", "檢查 uv 輸出後重跑")
        self.record("安裝 wheel", "ok", self.wheel.name)

    def step_mcp_toml(self) -> None:
        path = self.paths.mcp_toml
        if path.exists() and self.base_url is None:
            keep = self.ui.choose(
                f"{path} 已存在：",
                [("k", "保留現有設定"), ("o", "重新輸入服務位址並覆寫")],
                default="k",
            )
            if keep == "k":
                self.record("寫 mcp.toml", "skip", "保留現有檔案")
                return
        base_url = self._resolve_base_url()
        cf_file = self._resolve_cf()
        content = render_mcp_toml(base_url, **self._toml_paths(cf_file))
        if self.dry_run:
            self.record("寫 mcp.toml", "dry", f"會寫入 {path}（base_url={base_url}）")
            return
        self.paths.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.write_bytes(path, content.encode("utf-8"))
        self.record(
            "寫 mcp.toml",
            "ok",
            f"base_url={base_url}" + ("；CF Access" if cf_file else ""),
        )

    def _resolve_base_url(self) -> str:
        """服務位址必填：--base-url，否則互動詢問；dry-run 沒給時用占位字串。"""
        if self.base_url:
            url = normalize_base_url(self.base_url)
        elif self.dry_run:
            return "<服務位址>"
        else:
            answer = self.ui.ask_required(
                "服務位址（例如 https://vault.example.com，不含 /mcp）"
            )
            if not answer:
                raise StepFailed(
                    "沒有服務位址（--yes 模式不會詢問）",
                    "加上 --base-url https://<你的服務位址> 後重跑",
                )
            url = normalize_base_url(answer)
        self.base_url = url
        if is_plaintext_remote(url):
            self.ui.say(
                "  注意：這是非本機的 http:// 位址，token 會以明文經過網路；"
                "對外請改用 https（tunnel 或 TLS 反向代理）。"
            )
        return url

    def _resolve_cf(self) -> Path | None:
        """Cloudflare Access 為選配：有 --cf-access-env 就用；互動時才詢問。"""
        if not self.cf_decided:
            self.cf_decided = True
            if self.cf_env_file is None and not self.ui.assume_yes:
                if self.ui.confirm(
                    "服務前面有 Cloudflare Access（需要 service token）？",
                    default=False,
                ):
                    answer = self.ui.ask(
                        "CF Access 憑證檔（含 CF_ACCESS_CLIENT_ID／SECRET 兩個鍵）",
                        str(self.paths.cf_env),
                    )
                    self.cf_env_file = Path(answer).expanduser()
        if self.cf_env_file is None:
            return None
        exists = self.cf_env_file.is_file()
        keys = cf_env_keys_present(self.cf_env_file) if exists else {}
        problems = self._cf_problems(exists, keys)
        if problems:
            raise StepFailed("；".join(problems), "補齊憑證檔後重跑")
        return self.cf_env_file

    def _toml_paths(self, cf_file: Path | None = None) -> dict[str, str]:
        """真實家目錄寫 `~`（同手冊）；--home 導向別處時寫絕對路徑，殼才不讀真實 ~。"""
        out: dict[str, str] = {}
        real_home = self.paths.home.resolve() == Path.home().resolve()
        if cf_file is not None:
            out["cf_access_env_file"] = (
                "~/.cloudflared/pm-token.env"
                if real_home and cf_file == self.paths.cf_env
                else cf_file.as_posix()
            )
        if not real_home:
            out["snapshot_dir"] = self.paths.snapshot_dir.as_posix()
        return out

    def _token_from_env(self) -> str | None:
        value = self.environ.get(TOKEN_ENV, "").strip()
        if value:
            self.ui.add_secret(value)
            return value
        return None

    def step_mcp_env(self) -> None:
        path = self.paths.mcp_env
        env_token = self._token_from_env()
        if env_file_has_token(path):
            if env_token is None:
                if self.ui.assume_yes or self.ui.confirm(
                    f"{path} 已有 token，保留？", default=True
                ):
                    self.record("寫 mcp.env", "skip", "保留現有 token")
                    return
            elif not self.ui.confirm(
                f"{path} 已有 token；以環境變數 {TOKEN_ENV} 的值覆寫？", default=True
            ):
                self.record("寫 mcp.env", "skip", "保留現有 token")
                return
        elif path.exists():
            self.ui.say(
                f"  {path} 存在但讀不到 token 行（可能有 BOM 或 UTF-16），將重寫。"
            )

        if self.dry_run:
            self.record("寫 mcp.env", "dry", f"會提示輸入 token 並寫入 {path}")
            return
        token = self._ask_token(env_token)
        validate_token(token)
        self.write_bytes(path, render_mcp_env(token), private=True)
        if not env_file_has_token(path):
            raise StepFailed("寫入後讀不到 token 行", "重跑此程式")
        self.record("寫 mcp.env", "ok", "UTF-8 無 BOM")

    def _ask_token(self, env_token: str | None) -> str:
        """取得 token：環境變數優先（互動時確認），否則以不回顯方式輸入。"""
        token = env_token
        if token is not None and not self.ui.assume_yes:
            if not self.ui.confirm(
                f"使用環境變數 {TOKEN_ENV} 的 token？", default=True
            ):
                token = None
        if token is None:
            if self.ui.assume_yes:
                raise StepFailed(
                    f"--yes 模式需要環境變數 {TOKEN_ENV}",
                    f"設定 {TOKEN_ENV} 後重跑，或不加 --yes 互動輸入",
                )
            if not _stdin_is_console():
                raise StepFailed(
                    "目前的終端機無法隱藏輸入（例如 Git Bash 的 mintty）",
                    f"改用 PowerShell／cmd 執行、`winpty python install.py`，"
                    f"或先設環境變數 {TOKEN_ENV}",
                )
            token = self.ui.secret(
                f"貼上 {TOKEN_ENV}（服務端 .env 或 /data/secrets/api-token 的值，"
                "不會顯示）："
            )
        validate_token(token)
        self.token = token
        return token

    # ── HTTP 模式 ──

    def step_http_connect(self) -> None:
        """決定服務位址、CF Access 與 token；只放在記憶體，由 claude mcp add 寫入。"""
        base_url = self._resolve_base_url()
        cf_file = self._resolve_cf()
        if self.dry_run:
            self.record(
                "連線設定",
                "dry",
                f"會詢問 token；MCP 位址 {mcp_http_url(base_url)}"
                + ("；CF Access" if cf_file else ""),
            )
            return
        token = self._ask_token(self._token_from_env())
        headers = {"Authorization": f"Bearer {token}"}
        if cf_file is not None:
            values = read_cf_values(cf_file)
            for value in values.values():
                self.ui.add_secret(value)
            if len(values) != len(CF_KEYS):
                raise StepFailed("CF Access 憑證檔的值是空的", "補齊兩個鍵的值後重跑")
            headers.update(cf_headers(values))
        self.http_headers = headers
        self.record(
            "連線設定",
            "ok",
            f"MCP 位址 {mcp_http_url(base_url)}" + ("；CF Access" if cf_file else ""),
        )

    def step_http_check(self) -> None:
        if self.dry_run:
            self.record("自檢", "dry", "會直接呼叫一次 /v1/status")
            return
        assert self.base_url is not None and self.http_headers is not None
        data = self.http_check(self.base_url, self.http_headers, DEFAULT_TIMEOUT)
        self.status = data
        cat = data.get("category") or "internal"
        if cat == "ok":
            self.record("自檢", "ok", self._status_line(data))
            return
        detail = CATEGORY_HINTS.get(cat, "")
        if data.get("message"):
            detail += f"（{data['message']}）"
        if cat == "service":
            # 服務連得上，只是回報錯誤或 doctor 有 fail：照樣登記，交給服務端處理
            self.record("自檢", "fail", detail)
            return
        raise StepFailed(detail, "修正後重跑；尚未登記 MCP，不會留下壞掉的條目")

    def _add_argv(self, claude: str, *, for_display: bool = False) -> list[str]:
        if self.mode != "http":
            return mcp_add_argv(claude, self.paths)
        url = mcp_http_url(self.base_url or "<服務位址>")
        if for_display or self.http_headers is None:
            headers = {"Authorization": "Bearer ***"}
            if self.cf_env_file is not None:
                headers.update(
                    {"CF-Access-Client-Id": "***", "CF-Access-Client-Secret": "***"}
                )
        else:
            headers = self.http_headers
        return mcp_add_http_argv(claude, url, headers)

    def step_mcp_register(self) -> None:
        claude = self._claude()
        if self.dry_run:
            self.record(
                "claude mcp",
                "dry",
                f"會移除 {OLD_MCP_NAME}（若有），再執行："
                + " ".join(self._add_argv(claude, for_display=True)),
            )
            return
        old = self.runner(mcp_get_argv(claude, OLD_MCP_NAME))
        if old.returncode == 0:
            scope = parse_mcp_scope(old.stdout)
            if scope in ("user", "local"):
                self.run(
                    mcp_remove_argv(claude, OLD_MCP_NAME, scope), "移除 open-notebook"
                )
                self.record(f"移除 {OLD_MCP_NAME}", "ok", f"scope={scope}")
            else:
                self.record(
                    f"移除 {OLD_MCP_NAME}",
                    "skip",
                    f"scope={scope or '不明'}，不自動移除；請在該專案手動 "
                    f"`claude mcp remove {OLD_MCP_NAME} -s <scope>`",
                )
        else:
            self.record(f"移除 {OLD_MCP_NAME}", "skip", "不存在")

        cur = self.runner(mcp_get_argv(claude, MCP_NAME))
        if cur.returncode == 0 and parse_mcp_scope(cur.stdout) == "user":
            self.run(
                mcp_remove_argv(claude, MCP_NAME, "user"), "移除舊 lore-vault 條目"
            )
        self.run(self._add_argv(claude), "claude mcp add")
        transport = "http" if self.mode == "http" else "stdio"
        self.record(f"登記 {MCP_NAME}", "ok", f"scope=user；transport={transport}")

    def step_skill(self) -> None:
        src = self.kit_dir / SKILL_FILE
        if not src.is_file():
            raise StepFailed(f"kit 缺 {SKILL_FILE}", "向主機重取 kit")
        new = src.read_text(encoding="utf-8")
        problems = check_skill_content(new)
        if problems:
            raise StepFailed(
                "kit 的 SKILL.md 未通過機器中立檢查：" + "；".join(problems),
                "回報主機，不要自行改寫",
            )
        dest = self.paths.skill
        old = dest.read_text(encoding="utf-8") if dest.exists() else None
        if old == new:
            self.record("pm skill", "skip", "內容相同")
            return
        for line in skill_diff_summary(old, new):
            self.ui.say("    " + line)
        if self.dry_run:
            self.record("pm skill", "dry", f"會覆寫 {dest}")
            return
        if not self.ui.confirm("覆寫 pm skill？", default=True):
            self.record("pm skill", "skip", "使用者選擇不覆寫")
            return
        self.write_bytes(dest, new.encode("utf-8"))
        self.record("pm skill", "ok", str(dest))

    def step_self_check(self) -> None:
        if self.dry_run:
            self.record("本機自檢", "dry", "會以 venv python 呼叫一次 /v1/status")
            return
        r = self.runner(self_check_argv(self.paths))
        data = parse_self_check(r.stdout)
        self.status = data
        cat = data.get("category") or "internal"
        hint = CATEGORY_HINTS.get(cat, "")
        if cat == "ok":
            self.record("本機自檢", "ok", self._status_line(data))
        else:
            detail = hint
            if data.get("message"):
                detail += f"（{data['message']}）"
            self.record("本機自檢", "fail", detail)

    # ── episode hook（D13）──

    def resolve_episodes(self) -> bool:
        """決定是否安裝 episode hook：旗標優先；--yes／dry-run 預設不裝；互動時詢問。

        預設不裝的理由：episode 是對話原文，D13 的服務端收料也預設關閉——
        兩端都要明確選擇，才不會意外把對話集中到服務。
        """
        if self.episodes is None:
            if self.ui.assume_yes:
                self.episodes = False
            else:
                self.ui.say("  episode hook（選配）：")
                self.ui.say(
                    "    每輪對話結束時把該輪 episode 推到服務，供夜間管線蒸餾。"
                )
                self.ui.say(
                    "    隱私：episode 是對話原文（你的輸入與 agent 回覆），"
                    "可能含機敏內容；"
                )
                self.ui.say(
                    "    本機也會留一份（~/.lore-vault/episodes/ 與 spool/）。"
                    "服務端要開啟收料才會收下。"
                )
                self.episodes = self.ui.confirm("安裝 episode hook？", default=False)
        return self.episodes

    def hook_python_candidates(self) -> list[str]:
        if self.hook_python:
            return [self.hook_python]
        candidates: list[str] = []
        if not self.python_in_venv:
            candidates.append(self.python_exe)
        if self.base_python:
            candidates.append(self.base_python)
        for name in ("python3", "python"):
            found = self.which(name)
            if found:
                candidates.append(found)
        unique: list[str] = []
        for c in candidates:
            if c not in unique:
                unique.append(c)
        return unique

    def detect_hook_python(self) -> str | None:
        """找 hook 用的系統 Python：≥ MIN_PYTHON、不是 venv。

        `--hook-python` 指定時只驗版本。

        hook 由 Claude Code 直接以這支 Python 執行，只用標準庫；逐一實跑探測，
        不信任路徑名稱（Windows 的 WindowsApps 捷徑可能只是商店導向）。
        """
        explicit = self.hook_python is not None
        for candidate in self.hook_python_candidates():
            r = self.runner([candidate, "-c", HOOK_PYTHON_PROBE])
            data = parse_hook_python_probe(r.stdout) if r.returncode == 0 else None
            if not data:
                continue
            version = tuple(data.get("version") or ())
            if version[:2] < MIN_PYTHON:
                continue
            if data.get("venv") and not explicit:
                continue
            self.hook_python_resolved = str(data.get("executable") or candidate)
            self.hook_python_version = ".".join(str(v) for v in version)
            return self.hook_python_resolved
        return None

    def _episode_problems(self) -> list[str]:
        """prepare 階段（任何寫入之前）：kit 有 hooks/、找得到系統 Python。"""
        problems: list[str] = []
        kit_hooks = self.kit_dir / HOOKS_DIR_NAME
        try:
            self.hooks_manifest = load_hooks_manifest(kit_hooks)
            self.ui.say(
                f"  [OK]   episode hook：{len(self.hooks_manifest['files'])} 個檔案"
                f"（{self.hooks_manifest.get('version')}，"
                f"{self.hooks_manifest.get('commit')}）"
            )
        except StepFailed as exc:
            self.ui.say(f"  [FAIL] episode hook：{exc.message}")
            problems.append(exc.message)
        try:
            load_settings_json(self.paths.settings_json)
        except StepFailed as exc:
            self.ui.say(f"  [FAIL] {exc.message}")
            problems.append(exc.message)
        python = self.detect_hook_python()
        if python:
            self.ui.say(
                f"  [OK]   hook 用系統 Python {self.hook_python_version}："
                f"{self.mask(python)}"
            )
        else:
            self.ui.say("  [FAIL] hook 用系統 Python：找不到")
            problems.append(
                "找不到合格的系統 Python"
                f"（≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]}、不是 venv）給 episode hook 用："
                "hook 由 Claude Code 直接呼叫系統 Python。安裝 Python 後重跑、"
                "以 --hook-python <路徑> 指定，或加 --no-episodes 跳過"
            )
        return problems

    def _toml_value(self, key: str) -> str | None:
        """完整殼保留既有 mcp.toml 時，服務位址與 CF 檔只存在那裡。"""
        try:
            import tomllib

            data = tomllib.loads(self.paths.mcp_toml.read_text(encoding="utf-8"))
        except (OSError, ValueError, ImportError):
            return None
        value = (data.get("mcp") or {}).get(key)
        return value if isinstance(value, str) and value else None

    def _hook_connection(self) -> tuple[str, str, dict[str, str] | None]:
        """(服務位址, token, CF Access 值或 None)。token 優先用本次輸入的，
        完整殼沿用既有 mcp.env 時從檔案讀（不列印）。"""
        raw_url = self.base_url or self._toml_value("base_url")
        if not raw_url:
            raise StepFailed("不知道服務位址", "加上 --base-url <服務位址> 後重跑")
        base_url = normalize_base_url(raw_url)
        token = self.token or read_env_token(self.paths.mcp_env)
        if not token:
            raise StepFailed(
                "取不到 token，無法寫 client.env",
                f"設定環境變數 {TOKEN_ENV} 或互動輸入後重跑",
            )
        self.ui.add_secret(token)
        cf_file = self.cf_env_file
        if cf_file is None and self.mode == "shell":
            configured = self._toml_value("cf_access_env_file")
            cf_file = Path(configured).expanduser() if configured else None
        cf = None
        if cf_file is not None:
            cf = read_cf_values(cf_file)
            for value in cf.values():
                self.ui.add_secret(value)
            if len(cf) != len(CF_KEYS):
                raise StepFailed("CF Access 憑證檔的值不完整", "補齊兩個鍵的值後重跑")
        return base_url, token, cf

    def step_hook_files(self) -> None:
        """kit 的 hooks/ → ~/.lore-vault/hooks/：先寫到暫存目錄驗證，再整目錄換上。"""
        src = self.kit_dir / HOOKS_DIR_NAME
        manifest = self.hooks_manifest or load_hooks_manifest(src)
        dest = self.paths.hooks_dir
        label = f"{manifest.get('version')}（{manifest.get('commit')}）"
        if self.dry_run:
            self.record("episode hook 檔案", "dry", f"會複製到 {dest}，版本 {label}")
            return
        try:
            installed = load_hooks_manifest(dest)
        except StepFailed:
            installed = None
        if installed == manifest:
            self.record("episode hook 檔案", "skip", f"已是 {label}")
            return
        staging = dest.with_name(dest.name + ".new")
        old = dest.with_name(dest.name + ".old")
        for leftover in (staging, old):
            if leftover.exists():
                shutil.rmtree(leftover)
        for rel in manifest["files"]:
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src / rel, target)
        shutil.copyfile(src / HOOKS_MANIFEST, staging / HOOKS_MANIFEST)
        load_hooks_manifest(staging)
        if dest.exists():
            os.replace(dest, old)
        os.replace(staging, dest)
        shutil.rmtree(old, ignore_errors=True)
        self.record(
            "episode hook 檔案", "ok", f"{len(manifest['files'])} 個檔案，版本 {label}"
        )

    def step_client_env(self) -> None:
        path = self.paths.client_env
        concept = self.paths.concept_snapshot if self.mode == "shell" else None
        extra = "；PreToolUse 讀殼的 concept 快照" if concept else ""
        if self.dry_run:
            self.record(
                "寫 client.env", "dry", f"會寫入 {path}（服務位址、token{extra}）"
            )
            return
        base_url, token, cf = self._hook_connection()
        data = render_client_env(base_url, token, cf, concept)
        detail = "UTF-8 無 BOM" + ("；CF Access" if cf else "") + extra
        if path.is_file() and path.read_bytes() == data:
            self.record("寫 client.env", "skip", "內容相同")
            return
        self.write_bytes(path, data, private=True)
        self.record("寫 client.env", "ok", detail)

    def step_register_hooks(self) -> None:
        """合併登記到 ~/.claude/settings.json。

        只動本安裝器的 hook，其他 hook 原樣保留。"""
        path = self.paths.settings_json
        root = self.paths.hooks_dir
        python = self.hook_python_resolved or "<系統 Python>"
        current = load_settings_json(path)
        hooks = current.get("hooks", {})
        hooks = hooks if isinstance(hooks, dict) else {}
        wanted: list[tuple[str, str, dict]] = []
        skipped: list[str] = []
        for event, matcher, script in hook_specs(self.mode):
            if foreign_registrations(hooks, root, Path(script).name):
                skipped.append(event)
                continue
            wanted.append((event, matcher, hook_entry(python, root / script)))
        self.hook_events = [event for event, _, _ in wanted]
        new = merge_hooks(current, root, wanted)
        events = "、".join(self.hook_events) or "（無）"
        if skipped:
            self.record(
                "登記 hook",
                "skip",
                "已有其他位置的 " + "、".join(skipped) + " hook 登記"
                "（可能是服務主機本機），不重複登記",
            )
        if new == current:
            self.record("登記 hook", "skip", f"已登記（{events}）")
            return
        if self.dry_run:
            self.record("登記 hook", "dry", f"會合併登記 {events} 到 {path}")
            return
        bak = backup_path(path)
        if path.exists() and not bak.exists():
            shutil.copy2(path, bak)
            self.record("備份 settings.json", "ok", str(bak))
        self.write_bytes(path, dump_settings_json(new))
        self.record("登記 hook", "ok", f"{events}（{self.mask(python)}）")

    def step_episode_check(self) -> None:
        if self.dry_run:
            self.record(
                "收料檢查",
                "dry",
                "會送空批次 POST /v1/episodes 檢查收料開關（不寫入任何資料）",
            )
            return
        base_url, token, cf = self._hook_connection()
        headers = {"Authorization": f"Bearer {token}"}
        if cf:
            headers.update(cf_headers(cf))
        data = self.episode_check(base_url, headers, DEFAULT_TIMEOUT)
        self.episode_status = data
        cat = data.get("category") or "internal"
        if cat == "ok":
            self.record("收料檢查", "ok", "服務已開啟收料；每輪結束時推送")
        elif cat == "disabled":
            # 不是失敗：hook 照裝，紀錄留在本機 spool，服務開啟後自動補推
            self.record("收料檢查", "skip", INGEST_DISABLED_HINT)
        else:
            detail = CATEGORY_HINTS.get(cat, "")
            if data.get("message"):
                detail += f"（{data['message']}）"
            self.record("收料檢查", "fail", detail)

    def step_episodes_skipped(self) -> None:
        self.record("episode hook", "skip", "未安裝（加 --episodes 可安裝）")

    def _episode_steps(self) -> list[tuple[str, Callable[[], None]]]:
        if not self.episodes:
            return [("episode hook", self.step_episodes_skipped)]
        return [
            ("episode hook 檔案", self.step_hook_files),
            ("寫 client.env", self.step_client_env),
            ("登記 hook", self.step_register_hooks),
            ("收料檢查", self.step_episode_check),
        ]

    def _episode_ok(self) -> bool:
        if self.dry_run or self.episode_status is None:
            return True
        return self.episode_status.get("category") in ("ok", "disabled")

    def _status_line(self, data: dict) -> str:
        doctor = data.get("doctor") or {}
        counts = "／".join(
            f"{k} {doctor.get(k, 0)}" for k in ("pass", "fail", "warn", "skipped")
        )
        wheel = f"、wheel {data.get('wheel_schema')}" if "wheel_schema" in data else ""
        return (
            f"ok={data.get('ok')} schema={data.get('schema_version')}"
            f"（服務預期 {data.get('schema_expected')}{wheel}）"
            f" doctor {counts}"
        )

    def collect_mcp_entry(self) -> None:
        if self.dry_run or not self.env or not self.env.claude:
            return
        r = self.runner([self.env.claude, "mcp", "list"])
        self.mcp_entry = find_mcp_list_entry(r.stdout) if r.returncode == 0 else None

    def _uv(self) -> str:
        if not self.env or not self.env.uv:
            raise StepFailed("找不到 uv")
        return self.env.uv

    def _claude(self) -> str:
        if not self.env or not self.env.claude:
            raise StepFailed("找不到 claude CLI")
        return self.env.claude

    # ── 報告 ──

    def report(self, mode: str) -> str:
        lines = [
            "===== Lore Vault MCP 安裝報告 =====",
            f"機器：{platform.node()}",
            f"時間：{_dt.datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"模式：{mode}（{self._mode_label()}）"
            f"{'（dry-run，未實際變更）' if self.dry_run else ''}",
            f"安裝程式版本：{INSTALLER_VERSION}",
            f"kit：{self.kit_dir.name}",
        ]
        if self.wheel is not None:
            lines.append(f"wheel：{self.wheel.name}")
            lines.append(f"wheel sha256：{self.wheel_sha256}")
        if self.env is not None:
            lines.append(
                f"環境：Python {self.env.python_version}；"
                f"{self.env.uv_version or 'uv ?'}；"
                f"claude {self.env.claude_version or '?'}"
            )
        lines.append("步驟：")
        for rec in self.records:
            lines.append(
                f"  [{rec.status}] {rec.name}"
                + (f"：{rec.detail}" if rec.detail else "")
            )
        if self.status is not None:
            cat = self.status.get("category")
            if cat == "ok":
                lines.append("status：" + self._status_line(self.status))
                if self.status.get("doctor_fails"):
                    lines.append(
                        "doctor fail：" + "、".join(self.status["doctor_fails"])
                    )
            else:
                lines.append(f"status：失敗（{cat}）{CATEGORY_HINTS.get(cat, '')}")
        if self.episodes is not None and mode == "install":
            if not self.episodes:
                lines.append("episode hook：未安裝")
            else:
                manifest = self.hooks_manifest or {}
                lines.append(
                    "episode hook："
                    + ("、".join(self.hook_events) or "未登記")
                    + f"；hook 版本 {manifest.get('version')}"
                    + f"（{manifest.get('commit')}）"
                    + f"；系統 Python {self.hook_python_version or '?'}"
                )
                cat = (self.episode_status or {}).get("category")
                if cat == "ok":
                    lines.append("收料：服務已開啟")
                elif cat == "disabled":
                    lines.append("收料：" + INGEST_DISABLED_HINT)
                elif cat:
                    lines.append(
                        f"收料：檢查失敗（{cat}）{CATEGORY_HINTS.get(cat, '')}"
                    )
        if self.mcp_entry:
            lines.append(f"claude mcp list：{self.mcp_entry}")
        elif not self.dry_run:
            lines.append("claude mcp list：找不到 lore-vault 條目")
        lines += [
            "",
            "下一步：請重開 Claude Code（--update 時可在 /mcp 重連 lore-vault），",
            "再請本機 agent 驗 status／recall／ask。",
            "======================================",
        ]
        return self.ui.redact(self.mask("\n".join(lines)))

    def emit_report(self, mode: str) -> None:
        text = self.report(mode)
        self.ui.say("")
        self.ui.say(text)
        if self.dry_run:
            return
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.paths.lv_dir / f"install-report-{stamp}.txt"
        self.write_bytes(path, (text + "\n").encode("utf-8"))
        self.report_path = path
        self.ui.say(f"報告已存到 {path}，請整段貼到聊天室給主機核對。")

    # ── 入口 ──

    def _mode_label(self) -> str:
        return "HTTP" if self.mode == "http" else "完整殼"

    def resolve_mode(self) -> str:
        """`auto`：互動時詢問（預設 HTTP；已有殼設定時預設殼）；非互動時依既有設定。"""
        if self.mode in MODES:
            return self.mode
        has_shell = self.paths.mcp_toml.exists()
        default = "s" if has_shell else "h"
        if self.ui.assume_yes:
            choice = default
        else:
            self.ui.say("  連線模式：")
            choice = self.ui.choose(
                "選擇：",
                [
                    ("h", "HTTP 模式：免裝套件，直接連服務的 /mcp（建議）"),
                    ("s", "完整殼：本機 venv＋stdio 殼，服務斷線時有唯讀快照"),
                ],
                default=default,
            )
        self.mode = "shell" if choice == "s" else "http"
        return self.mode

    def prepare(self, full: bool) -> bool:
        """偵測環境、找 wheel、顯示計畫並確認；回傳是否繼續。"""
        total = 3
        if full:
            self.resolve_mode()
            self.resolve_episodes()
        else:
            self.mode = "shell"
        self.ui.header(1, total, "偵測環境")
        env = self.detect()
        problems = self.show_env(env)
        if full and self.episodes:
            # 任何寫入之前就確認 hook 裝得起來，避免 MCP 裝好了才卡在 hook
            problems += self._episode_problems()
        if not full:
            # --update 不需要 claude CLI 與 CF 檔也能重裝，只有 Python 與 uv 是必要
            problems = [
                p for p in problems if p.startswith(("需要 Python", "找不到 uv"))
            ]
        self.ui.header(2, total, "確認 kit")
        if self.mode == "shell":
            self.wheel = find_wheel(self.kit_dir)
            self.wheel_sha256 = sha256_file(self.wheel)
            self.ui.say(f"  wheel：{self.wheel.name}")
            self.ui.say(f"  sha256：{self.wheel_sha256}")
        if full and not (self.kit_dir / SKILL_FILE).is_file():
            problems.append(f"kit 資料夾缺 {SKILL_FILE}：{self.kit_dir}")
        if problems:
            for p in problems:
                self.ui.say(f"  [FAIL] {p}")
            raise StepFailed("環境檢查未通過", "補齊上述項目後重跑")
        self.ui.header(3, total, "將要執行")
        for line in self.plan(full):
            self.ui.say("  " + line)
        if full and not self.dry_run:
            self.ui.say("  建議先完全結束 Claude Code，避免它同時改寫 ~/.claude.json。")
        if self.dry_run:
            self.ui.say("  （dry-run：以下只列出會做的事，不寫檔、不改設定）")
            return True
        return self.ui.confirm("開始？", default=True)

    def install(self) -> int:
        if not self.prepare(full=True):
            raise Abort()
        if self.mode == "http":
            return self.install_http()
        steps = [
            ("備份", self.step_backup),
            ("建 venv", self.step_venv),
            ("安裝 wheel", self.step_install_wheel),
            ("寫 mcp.toml", self.step_mcp_toml),
            ("寫 mcp.env", self.step_mcp_env),
            ("登記 MCP", self.step_mcp_register),
            ("pm skill", self.step_skill),
            ("本機自檢", self.step_self_check),
            *self._episode_steps(),
        ]
        self._run_steps(steps)
        self.collect_mcp_entry()
        self.emit_report("install")
        return 0 if self._self_check_ok() and self._episode_ok() else 1

    def install_http(self) -> int:
        steps = [
            ("備份", self.step_backup),
            ("連線設定", self.step_http_connect),
            ("自檢", self.step_http_check),
            ("登記 MCP", self.step_mcp_register),
            ("pm skill", self.step_skill),
            *self._episode_steps(),
        ]
        self._run_steps(steps)
        self.collect_mcp_entry()
        self.emit_report("install")
        return 0 if self._self_check_ok() and self._episode_ok() else 1

    def update(self) -> int:
        if not self.prepare(full=False):
            raise Abort()
        steps = [("安裝 wheel", self.step_install_wheel)]
        if self.paths.mcp_toml.exists() and self.paths.mcp_env.exists():
            steps.append(("本機自檢", self.step_self_check))
        self._run_steps(steps)
        self.collect_mcp_entry()
        self.emit_report("update")
        self.ui.say("在 Claude Code 執行 /mcp 重連 lore-vault 即生效，不必重開。")
        return 0 if self._self_check_ok() else 1

    def _self_check_ok(self) -> bool:
        return (
            self.dry_run or self.status is None or self.status.get("category") == "ok"
        )

    def _run_steps(self, steps: list[tuple[str, Callable[[], None]]]) -> None:
        total = len(steps)
        for i, (title, fn) in enumerate(steps, 1):
            self.ui.header(i, total, title)
            try:
                fn()
            except StepFailed as exc:
                self.record(title, "fail", exc.message)
                raise

    def rollback(self) -> int:
        settings = self.paths.settings_json
        pairs = [
            (backup_path(p), p)
            for p in (self.paths.claude_json, self.paths.skill, settings)
        ]
        found = [(b, p) for b, p in pairs if b.exists()]
        # 安裝前沒有 settings.json（因此沒有備份）時，改為只移除本安裝器登記的 hook
        strip_hooks = False
        if not backup_path(settings).exists() and settings.exists():
            current = load_settings_json(settings)
            hooks = current.get("hooks")
            strip_hooks = isinstance(hooks, dict) and any(
                hook_points_under(h, self.paths.hooks_dir)
                for _, h in _iter_hooks(hooks)
            )
        if not found and not strip_hooks:
            self.ui.say(f"找不到任何 {BACKUP_SUFFIX} 備份，無法還原。")
            return 1
        for bak, dest in found:
            self.ui.say(f"  {bak} → {dest}")
        if strip_hooks:
            self.ui.say(f"  {settings}：移除指向 {self.paths.hooks_dir} 的 hook")
        self.ui.say(
            "注意：~/.claude.json 會整份還原到備份時的狀態，"
            "備份之後新增的其他 MCP 條目或設定也會一併消失。"
        )
        if any(dest == settings for _, dest in found):
            self.ui.say(
                "注意：~/.claude/settings.json 也會整份還原，"
                "備份之後新增的其他 hook 或設定會一併消失。"
            )
        if self.dry_run:
            for _, dest in found:
                self.record(f"還原 {dest.name}", "dry")
            if strip_hooks:
                self.record("移除 episode hook 登記", "dry")
            return 0
        # --yes 代表已同意還原；互動時仍預設否
        if not self.ui.assume_yes and not self.ui.confirm("確定還原？", default=False):
            raise Abort()
        for bak, dest in found:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bak, dest)
            self.record(f"還原 {dest.name}", "ok")
        if strip_hooks:
            current = load_settings_json(settings)
            self.write_bytes(
                settings,
                dump_settings_json(merge_hooks(current, self.paths.hooks_dir, [])),
            )
            self.record("移除 episode hook 登記", "ok", str(settings))
        self.ui.say(
            "已還原。請完全結束並重開 Claude Code。"
            "~/.lore-vault/ 可留著，不影響舊設定；"
            "其中 client.env 含 token，"
            "不再使用 episode hook 可刪除 client.env 與 hooks/。"
        )
        return 0


def _stdin_is_console() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python install.py",
        description="安裝 Lore Vault MCP 客戶端（HTTP 模式或完整殼）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help="只重裝 wheel")
    mode.add_argument("--rollback", action="store_true", help="還原 .bak-precutover")
    parser.add_argument("--dry-run", action="store_true", help="只顯示會做什麼")
    parser.add_argument(
        "--yes", action="store_true", help=f"非互動；token 走環境變數 {TOKEN_ENV}"
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        help="http＝免殼直連 /mcp；shell＝本機 venv＋stdio 殼（未指定時詢問）",
    )
    parser.add_argument(
        "--base-url",
        help="服務位址，如 https://vault.example.com（必填；未給時互動詢問）",
    )
    parser.add_argument(
        "--cf-access-env",
        type=Path,
        help="選配：服務前面有 Cloudflare Access 時，含 CF_ACCESS_CLIENT_ID／"
        "CF_ACCESS_CLIENT_SECRET 的檔案",
    )
    episodes = parser.add_mutually_exclusive_group()
    episodes.add_argument(
        "--episodes",
        dest="episodes",
        action="store_true",
        default=None,
        help="安裝 episode hook（對話原文推到服務；--yes 時預設不裝）",
    )
    episodes.add_argument(
        "--no-episodes",
        dest="episodes",
        action="store_false",
        help="不安裝 episode hook（不詢問）",
    )
    parser.add_argument(
        "--hook-python",
        help="episode hook 用的 Python 路徑（預設自動找系統 Python ≥ 3.12）",
    )
    parser.add_argument(
        "--kit-dir", type=Path, help="kit 資料夾（預設為 install.py 所在目錄）"
    )
    parser.add_argument(
        "--no-mask", action="store_true", help="報告中不把家目錄路徑遮成 ~"
    )
    # 測試與 dry-run 驗證用：把所有家目錄路徑導向別處
    parser.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = subprocess_runner,
    ui: UI | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    # dry-run 不提問：一律以預設答案走完，只列出會做的事
    ui = ui or UI(assume_yes=args.yes or args.dry_run)
    if sys.version_info[:2] < MIN_PYTHON:
        ui.say(
            f"需要 Python ≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]}，"
            f"目前是 {platform.python_version()}"
        )
        return 2
    home = (args.home or Path.home()).expanduser()
    kit_dir = (args.kit_dir or Path(__file__).resolve().parent).expanduser()
    installer = Installer(
        paths=Paths(home),
        kit_dir=kit_dir,
        ui=ui,
        runner=runner,
        dry_run=args.dry_run,
        mask_user=not args.no_mask,
        base_url=args.base_url,
        mode=args.mode or ("shell" if args.update else "auto"),
        cf_env_file=args.cf_access_env.expanduser() if args.cf_access_env else None,
        environ=dict(os.environ if environ is None else environ),
        episodes=args.episodes,
        hook_python=args.hook_python,
    )
    ui.say("Lore Vault MCP 安裝程式" + ("（dry-run）" if args.dry_run else ""))
    try:
        if args.rollback:
            return installer.rollback()
        if args.update:
            return installer.update()
        return installer.install()
    except StepFailed as exc:
        ui.say("")
        ui.say(f"[停止] {exc.message}")
        if exc.resume:
            ui.say(f"  處理方式：{exc.resume}")
        ui.say("  修正後重跑同一個指令即可；已完成的步驟會自動略過。")
        return 1
    except Abort:
        ui.say("已取消，未做任何後續變更。")
        return 1
    except (KeyboardInterrupt, EOFError):
        ui.say("")
        ui.say("已中斷。重跑同一個指令即可從頭檢查並續做。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
