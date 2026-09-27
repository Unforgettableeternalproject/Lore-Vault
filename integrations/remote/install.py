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
    python install.py --rollback  還原兩份 .bak-precutover

服務前面有 Cloudflare Access 時加 `--cf-access-env <檔案>`（含 CF_ACCESS_CLIENT_ID／
CF_ACCESS_CLIENT_SECRET 兩個鍵）；沒有就不需要。
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

INSTALLER_VERSION = "2"
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

    def write_bytes(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
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
            return lines
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
        else:
            lines += [
                "1. uv pip install --reinstall 重裝 wheel（venv 必須已存在）",
                "2. 本機自檢",
                "3. 產生驗證報告；之後在 Claude Code 用 /mcp 重連 lore-vault",
            ]
        return lines

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
        self.write_bytes(path, render_mcp_env(token))
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
        else:
            self.mode = "shell"
        self.ui.header(1, total, "偵測環境")
        env = self.detect()
        problems = self.show_env(env)
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
        ]
        self._run_steps(steps)
        self.collect_mcp_entry()
        self.emit_report("install")
        return 0 if self._self_check_ok() else 1

    def install_http(self) -> int:
        steps = [
            ("備份", self.step_backup),
            ("連線設定", self.step_http_connect),
            ("自檢", self.step_http_check),
            ("登記 MCP", self.step_mcp_register),
            ("pm skill", self.step_skill),
        ]
        self._run_steps(steps)
        self.collect_mcp_entry()
        self.emit_report("install")
        return 0 if self._self_check_ok() else 1

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
        pairs = [
            (backup_path(p), p) for p in (self.paths.claude_json, self.paths.skill)
        ]
        found = [(b, p) for b, p in pairs if b.exists()]
        if not found:
            self.ui.say(f"找不到任何 {BACKUP_SUFFIX} 備份，無法還原。")
            return 1
        for bak, dest in found:
            self.ui.say(f"  {bak} → {dest}")
        self.ui.say(
            "注意：~/.claude.json 會整份還原到備份時的狀態，"
            "備份之後新增的其他 MCP 條目或設定也會一併消失。"
        )
        if self.dry_run:
            for _, dest in found:
                self.record(f"還原 {dest.name}", "dry")
            return 0
        # --yes 代表已同意還原；互動時仍預設否
        if not self.ui.assume_yes and not self.ui.confirm("確定還原？", default=False):
            raise Abort()
        for bak, dest in found:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bak, dest)
            self.record(f"還原 {dest.name}", "ok")
        self.ui.say(
            "已還原。請完全結束並重開 Claude Code。"
            "~/.lore-vault/ 可留著，不影響舊設定。"
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
