"""版本化側載的假服務（MCP-T1 的 `/v1/blob_put` 樂觀鎖語意），給 doctor 服務端
對帳與 migrate 的測試用。

沿用 `conftest.FakeVault`（真的 HTTP、記憶體內 note），只換掉側載兩個端點：
- `blob_put` 版本遞增，回 `{updated, version}`；`expected_version` 不符回 409
  `version_conflict`（`error.current` 為目前內容或 null，形狀同 `api/errors.py`）
- `remote_sync = False` 時 `task-` 開頭的 `blob_put` 回 403
  `tasks_remote_sync_disabled`（同 `api/routes.py`）
"""

from __future__ import annotations

import base64
import json
from typing import Any

from lore_vault.tasks import remote_store as rs

from .conftest import VAULT, FakeVault


class VersionedVault(FakeVault):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.remote_sync = True

    def handle(self, method: str, path: str, headers, body):
        if path == "/v1/blob_put":
            return self._blob_put(body)
        return super().handle(method, path, headers, body)

    def _blob_put(self, body: dict[str, Any]):
        key = (body["vault"], body["key"])
        if body["key"].startswith("task-") and not self.remote_sync:
            return (
                403,
                {"error": {"code": "tasks_remote_sync_disabled", "message": "關閉"}},
                {},
            )
        current = self.blobs.get(key)
        version = current["version"] if current else 0
        expected = body.get("expected_version")
        if expected is not None and expected != version:
            return (
                409,
                {
                    "error": {
                        "code": "version_conflict",
                        "message": "版本衝突",
                        "expected": expected,
                        "current": None
                        if current is None
                        else {"vault": key[0], "key": key[1], **current},
                    }
                },
                {},
            )
        self.blob_puts += 1
        updated = f"2026-10-08T12:00:{self.blob_puts:02d}.000Z"
        self.blobs[key] = {
            "mime": body.get("mime") or "application/octet-stream",
            "content_base64": body["content_base64"],
            "updated": updated,
            "version": version + 1,
        }
        return 200, {"updated": updated, "version": version + 1}, {}

    # 測試直接擺放／讀取服務端內容

    def put_json(self, key: str, data: Any, vault: str = VAULT) -> int:
        current = self.blobs.get((vault, key))
        version = (current["version"] if current else 0) + 1
        self.blobs[(vault, key)] = {
            "mime": rs.MIME,
            "content_base64": base64.b64encode(
                json.dumps(data, ensure_ascii=False).encode("utf-8")
            ).decode("ascii"),
            "updated": "2026-10-08T12:00:00.000Z",
            "version": version,
        }
        return version

    def get_json(self, key: str, vault: str = VAULT) -> Any:
        hit = self.blobs.get((vault, key))
        if hit is None:
            return None
        return json.loads(base64.b64decode(hit["content_base64"]).decode("utf-8"))

    def version(self, key: str, vault: str = VAULT) -> int:
        hit = self.blobs.get((vault, key))
        return hit["version"] if hit else 0
