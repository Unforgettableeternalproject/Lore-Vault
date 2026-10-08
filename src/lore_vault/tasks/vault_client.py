"""任務層對 Lore Vault 的 HTTP 薄層：沿用 hook 端客戶端（`hooks.service`、
`hooks.client_env`），不另立設定、不碰 DB。

服務位址與認證照 hook 的讀法：`LORE_VAULT_CLIENT_ENV` 指定的檔案，否則
`~/.lore-vault/client.env`（同 spike `paths.CLIENT_ENV_PATH`）；同名環境變數優先。
token 只透過 `Secret` 傳遞，例外訊息與輸出都不含密鑰。
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from lore_vault.hooks.client_env import (
    CLIENT_ENV_VAR,
    ClientSettings,
    load_client_settings,
)
from lore_vault.hooks.service import (
    ServiceError,
    ServiceRejected,
    ServiceUnavailable,
    request_json,
)

DEFAULT_CLIENT_ENV = Path.home() / ".lore-vault" / "client.env"
DEFAULT_TIMEOUT = 30.0
_LIST_LIMIT = 50

__all__ = [
    "DEFAULT_CLIENT_ENV",
    "ServiceError",
    "ServiceRejected",
    "ServiceUnavailable",
    "VaultClient",
    "load_settings",
]


def load_settings(
    env_file: str | None = None, environ: Mapping[str, str] | None = None
) -> ClientSettings:
    env = os.environ if environ is None else environ
    if not env_file:
        return load_client_settings(DEFAULT_CLIENT_ENV, env)
    # 明確指定的檔案優先於 LORE_VAULT_CLIENT_ENV
    env = {k: v for k, v in env.items() if k != CLIENT_ENV_VAR}
    return load_client_settings(Path(env_file).expanduser(), env)


class VaultClient:
    def __init__(self, settings: ClientSettings, *, timeout: float = DEFAULT_TIMEOUT):
        self.settings = settings
        self.timeout = timeout

    def describe(self) -> str:
        return self.settings.describe()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        if not self.settings.push_configured:
            raise ServiceRejected(self.settings.describe())
        return request_json(self.settings, "POST", path, body, timeout=self.timeout)

    def resolve_vault(self, key: str, space: str) -> str:
        data = self._post("/v1/vault_resolve", {"key": key, "space": space})
        return str(data["key"])

    def write(
        self,
        vault: str,
        space: str,
        *,
        title: str,
        body: str,
        topics: Sequence[str],
        links: Sequence[str] = (),
        supersedes: str | None = None,
        author: str | None = None,
    ) -> str:
        data = self._post(
            "/v1/write",
            {
                "vault": vault,
                "space": space,
                "title": title,
                "body": body,
                "topics": list(topics),
                "links": list(links),
                "supersedes": supersedes,
                "author": author,
            },
        )
        return str(data["id"])

    def list_topic(self, vault: str, space: str, topic: str) -> list[dict[str, Any]]:
        """列出帶某 topic 的全部 note（翻頁到底）。"""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "vault": vault,
                "space": space,
                "topics": [topic],
                "kinds": ["note"],
                "limit": _LIST_LIMIT,
            }
            if cursor:
                body["cursor"] = cursor
            data = self._post("/v1/list", body)
            # 服務端 topics 是「含任一」；這裡再過濾一次，不依賴服務端語意
            items.extend(
                i for i in data.get("items", []) if topic in (i.get("topics") or [])
            )
            cursor = data.get("next_cursor")
            if not cursor:
                return items

    def get_meta(
        self, vault: str, space: str, ids: Sequence[str]
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """回傳 ({id: metadata}, missing ids)。"""
        if not ids:
            return {}, []
        data = self._post(
            "/v1/get",
            {"vault": vault, "space": space, "ids": list(ids), "fields": "meta"},
        )
        found = {i["id"]: i for i in data.get("items", []) if "id" in i}
        return found, [str(m) for m in data.get("missing", [])]

    def put_blob(
        self, vault: str, space: str, key: str, content: bytes, *, mime: str
    ) -> str:
        """覆寫服務端側載（`/v1/blob_put`）；回傳服務端的 `updated`。"""
        data = self._post(
            "/v1/blob_put",
            {
                "vault": vault,
                "space": space,
                "key": key,
                "mime": mime,
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        )
        return str(data["updated"])

    def get_blob(self, vault: str, space: str, key: str) -> dict[str, Any] | None:
        """讀回側載（`/v1/blob_get`）；尚未推送過（404 `not_found`）回 None。

        回傳 `{mime, content（bytes）, updated}`。"""
        try:
            data = self._post(
                "/v1/blob_get", {"vault": vault, "space": space, "key": key}
            )
        except ServiceRejected as exc:
            body = exc.body if isinstance(exc.body, dict) else {}
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            if exc.status == 404 and error.get("code") == "not_found":
                return None
            raise
        return {
            "mime": data.get("mime"),
            "content": base64.b64decode(str(data["content_base64"]), validate=True),
            "updated": data.get("updated"),
        }
