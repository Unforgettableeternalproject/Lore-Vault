"""hook 端客戶端設定：只用標準庫讀一個 env 檔（KEY=VALUE）。

hook 由系統 Python 直接執行，不能 import `lore_vault.config`（它屬於服務端設定、
可能拉進第三方套件）。這裡只解析 hook 需要的幾個鍵：

| 鍵 | 說明 |
|---|---|
| `LORE_VAULT_URL` | 服務位址（`http://` 或 `https://`） |
| `LORE_VAULT_API_TOKEN` | bearer token（密鑰） |
| `CF_ACCESS_CLIENT_ID`／`CF_ACCESS_CLIENT_SECRET` | 選用；兩個都有才帶 header（密鑰） |
| `LORE_VAULT_PUSH_TIMEOUT` | 推送逾時秒數，預設 2 |
| `LORE_VAULT_PUSH_BATCH` | 單次最多推幾筆，預設 20 |
| `LORE_VAULT_CONCEPT_SNAPSHOT` | PreToolUse 讀的快照；未設＝現行 concepts.json |

檔案位置：環境變數 `LORE_VAULT_CLIENT_ENV` 指定，否則用呼叫端給的預設
（spike 為 `paths.CLIENT_ENV_PATH`）。環境變數中同名的鍵優先於檔案。

密鑰包在 `Secret`，repr 不含值；設定問題只描述「哪個鍵」，不帶值。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CLIENT_ENV_VAR = "LORE_VAULT_CLIENT_ENV"
URL_KEY = "LORE_VAULT_URL"
TOKEN_KEY = "LORE_VAULT_API_TOKEN"
CF_ID_KEY = "CF_ACCESS_CLIENT_ID"
CF_SECRET_KEY = "CF_ACCESS_CLIENT_SECRET"
PUSH_TIMEOUT_KEY = "LORE_VAULT_PUSH_TIMEOUT"
PUSH_BATCH_KEY = "LORE_VAULT_PUSH_BATCH"
CONCEPT_SNAPSHOT_KEY = "LORE_VAULT_CONCEPT_SNAPSHOT"

KNOWN_KEYS = frozenset(
    {
        URL_KEY,
        TOKEN_KEY,
        CF_ID_KEY,
        CF_SECRET_KEY,
        PUSH_TIMEOUT_KEY,
        PUSH_BATCH_KEY,
        CONCEPT_SNAPSHOT_KEY,
    }
)

DEFAULT_PUSH_TIMEOUT = 2.0
DEFAULT_PUSH_BATCH = 20


class Secret:
    """密鑰包裝：repr／str 不含值，只有 `reveal()` 取得原文。"""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__


@dataclass(frozen=True)
class ClientSettings:
    """hook 端設定。`problems` 非空或缺 URL／token 時 `push_configured` 為 False。"""

    env_file: Path | None
    url: str | None = None
    token: Secret | None = None
    cf_access: tuple[Secret, Secret] | None = None
    concept_snapshot: Path | None = None
    push_timeout: float = DEFAULT_PUSH_TIMEOUT
    push_batch: int = DEFAULT_PUSH_BATCH
    problems: tuple[str, ...] = ()

    @property
    def push_configured(self) -> bool:
        return bool(self.url and self.token is not None and not self.problems)

    def describe(self) -> str:
        """給 log／doctor 的一行狀態，不含任何密鑰。"""
        if self.push_configured:
            return f"推送已設定（{self.url}）"
        if self.problems:
            return "推送未設定：" + "；".join(self.problems)
        missing = [
            k for k, v in ((URL_KEY, self.url), (TOKEN_KEY, self.token)) if not v
        ]
        return "推送未設定：缺少 " + "、".join(missing)


def env_file_path(
    default: Path | None, environ: Mapping[str, str] | None = None
) -> Path | None:
    env = os.environ if environ is None else environ
    override = env.get(CLIENT_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return default


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE；檔案不存在回空 dict。支援 `#` 註解、`export ` 前綴、成對引號。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_client_settings(
    default_env_file: Path | None,
    environ: Mapping[str, str] | None = None,
) -> ClientSettings:
    """讀設定；任何問題都收進 `problems`，不拋例外（hook 不能因設定壞掉而中斷）。"""
    env = os.environ if environ is None else environ
    path = env_file_path(default_env_file, env)
    problems: list[str] = []
    merged: dict[str, str] = {}
    if path is not None:
        try:
            merged.update(parse_env_file(path))
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"讀不到設定檔（{type(exc).__name__}）")
    for key in KNOWN_KEYS:
        value = env.get(key)
        if value is not None and value.strip():
            merged[key] = value

    def get(key: str) -> str:
        return merged.get(key, "").strip()

    url = get(URL_KEY).rstrip("/") or None
    if url and not url.startswith(("http://", "https://")):
        problems.append(f"{URL_KEY} 必須以 http:// 或 https:// 開頭")
    token = Secret(get(TOKEN_KEY)) if get(TOKEN_KEY) else None

    cf_id, cf_secret = get(CF_ID_KEY), get(CF_SECRET_KEY)
    cf_access = None
    if cf_id and cf_secret:
        cf_access = (Secret(cf_id), Secret(cf_secret))
    elif cf_id or cf_secret:
        missing = CF_ID_KEY if not cf_id else CF_SECRET_KEY
        problems.append(f"Cloudflare Access 設定不完整：缺少 {missing}")

    push_timeout = DEFAULT_PUSH_TIMEOUT
    if get(PUSH_TIMEOUT_KEY):
        try:
            push_timeout = float(get(PUSH_TIMEOUT_KEY))
            if push_timeout <= 0:
                raise ValueError
        except ValueError:
            problems.append(f"{PUSH_TIMEOUT_KEY} 必須是正數")
            push_timeout = DEFAULT_PUSH_TIMEOUT

    push_batch = DEFAULT_PUSH_BATCH
    if get(PUSH_BATCH_KEY):
        try:
            push_batch = int(get(PUSH_BATCH_KEY))
            if push_batch <= 0:
                raise ValueError
        except ValueError:
            problems.append(f"{PUSH_BATCH_KEY} 必須是正整數")
            push_batch = DEFAULT_PUSH_BATCH

    snapshot = get(CONCEPT_SNAPSHOT_KEY)
    return ClientSettings(
        env_file=path,
        url=url,
        token=token,
        cf_access=cf_access,
        concept_snapshot=Path(snapshot).expanduser() if snapshot else None,
        push_timeout=push_timeout,
        push_batch=push_batch,
        problems=tuple(problems),
    )
