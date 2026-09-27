# 自架指南

用 docker compose 架一套 Lore Vault 服務。快速版見 [README](../../README.zh-tw.md#自架快速開始)；本文補齊設定、profile、對外公開、備份還原與疑難排解。
客戶端（Claude Code）怎麼連見 [REMOTE-INSTALL.md](REMOTE-INSTALL.md)。

## 需求

- Docker Engine 或 Docker Desktop，Compose v2.24 以上（`docker compose version`）
- 磁碟：ollama profile 另需數 GB（Ollama 映像與約 1.2 GB 的 `bge-m3` 模型）
- 選配：OpenAI API key（`ask` 與摘要）、Cloudflare 帳號（tunnel）

## 組成

| 服務 | profile | 說明 |
|---|---|---|
| `lore-vault` | （一律啟動） | HTTP API、MCP 端點 `/mcp`、Web UI `/ui`；容器內 8000，主機 `LORE_VAULT_BIND:LORE_VAULT_PORT`（預設 `127.0.0.1:5056`） |
| `ollama` | `ollama` | embedding 服務，不對外發布埠；模型存於 volume `lore-vault-ollama` |
| `ollama-pull` | `ollama` | 一次性：拉 `LORE_VAULT_EMBEDDING_MODEL`（預設 `bge-m3`）後結束；`lore-vault` 等它成功才啟動 |
| `cloudflared` | `tunnel` | Cloudflare Tunnel 連接器，使用 `TUNNEL_TOKEN` |

資料：

- 資料庫、上傳文件原檔、自動產生的密鑰都在 named volume `lore-vault-data`（容器內 `/data`）
- 備份在主機目錄 `LORE_VAULT_HOST_BACKUP_DIR`（預設 compose 檔旁的 `./backups`，容器內 `/backups`）
- 資料庫不要改成 bind mount 到 Windows 磁碟（NTFS 上的 SQLite WAL 不可靠）

## 設定

複製 `.env.example` 成 `.env` 修改，每個變數的說明都在範本裡。重點：

| 變數 | 預設 | 說明 |
|---|---|---|
| `COMPOSE_PROFILES` | （無） | 範本示範 `ollama`；要 tunnel 就 `ollama,tunnel` |
| `LORE_VAULT_BIND`／`LORE_VAULT_PORT` | `127.0.0.1`／`5056` | 主機發布位址 |
| `LORE_VAULT_API_TOKEN` | 自動產生 | bearer token；未設時首次啟動產生，存 `/data/secrets/api-token` |
| `LORE_VAULT_PRINCIPAL` | `owner` | 單一使用者的名稱，寫入紀錄的擁有者；啟用後不要再改 |
| `LORE_VAULT_ADMIN_USER`／`LORE_VAULT_ADMIN_PASSWORD` | 同 principal／自動產生 | UI 管理員；只在還沒有任何 UI 帳號時建立 |
| `OPENAI_API_KEY` | （無） | `ask` 與摘要；未設時這兩項不可用 |
| `LORE_VAULT_EMBEDDING_BASE_URL` | `http://host.docker.internal:11434` | 用 ollama profile 時**必須**設成 `http://ollama:11434` |
| `LORE_VAULT_HOST_BACKUP_DIR` | `./backups` | 主機備份目錄 |
| `TUNNEL_TOKEN` | （無） | tunnel profile 用 |
| `LORE_VAULT_UI_TRUSTED_PROXIES` | `172.16.0.0/12` | 採信 `CF-Connecting-IP` 的來源網段 |

其他服務設定可用 `LORE_VAULT_<區段>_<項目>` 環境變數覆寫映像內的 `docker/config.toml`（例如 `LORE_VAULT_ASK_MODEL`），完整清單見 [config.example.toml](../../config.example.toml)。

### Embedding：用哪個 Ollama

- **ollama profile**（自架建議）：`COMPOSE_PROFILES=ollama` ＋ `LORE_VAULT_EMBEDDING_BASE_URL=http://ollama:11434`。
  只啟用 profile 而沒改位址時，lore-vault 仍會去連主機的 Ollama。
- **主機上已有 Ollama**：不啟用 profile，保持預設位址，先在主機 `ollama pull bge-m3`。Linux 上 compose 已加 `host.docker.internal:host-gateway`，但 Ollama 要監聽容器連得到的介面（`OLLAMA_HOST=0.0.0.0`）。
- GPU：ollama 容器預設用 CPU，`bge-m3` 在 CPU 上也夠用。要用 GPU 請依 Ollama 官方文件另寫 compose override。

## 首次啟動

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps            # lore-vault 應為 healthy；ollama-pull 為 exited (0)
```

token 與管理員密碼只在首次啟動產生，log 印一次，同時存在 volume 內：

```bash
docker compose exec lore-vault cat /data/secrets/api-token
docker compose exec lore-vault cat /data/secrets/initial-admin-password
```

- `.env` 有設 `LORE_VAULT_API_TOKEN` 時以它為準，不產生檔案
- 之後才在 `.env` 設 token，會取代先前產生的 token：已連線的客戶端要用新 token 重新登記
- 管理員登入 UI 後請改密碼；`initial-admin-password` 之後不再有效用，可自行刪除

## 對外公開

### Cloudflare Tunnel（建議）

1. Cloudflare Zero Trust → Networks → Tunnels 建立 tunnel（Cloudflared 類型），複製 token
2. 在該 tunnel 加公開主機名（例如 `vault.example.com`），服務類型 HTTP、URL 填 `lore-vault:8000`
3. `.env`：`TUNNEL_TOKEN=<token>`、`COMPOSE_PROFILES=ollama,tunnel`
4. `docker compose up -d`；`docker compose logs cloudflared` 應出現已註冊的連線

token 型 tunnel 的路由在 Cloudflare 後台設定，本機不需要 `config.yml`。
cloudflared 與 lore-vault 在同一個 compose 網路，不經主機的發布埠；`LORE_VAULT_BIND` 可維持 `127.0.0.1`。

**Cloudflare Access（選配）**：可在公開主機名前加 Access 應用程式，並為客戶端建 service token。
客戶端安裝時用 `--cf-access-env` 指定含 `CF_ACCESS_CLIENT_ID`／`CF_ACCESS_CLIENT_SECRET` 的檔案，見 [REMOTE-INSTALL.md](REMOTE-INSTALL.md)。
UI 走瀏覽器登入，要讓 UI 經過 Access 的話另設允許你帳號的政策。

**登入紀錄的來源 IP**：服務只在直接來源屬於 `trusted_proxies` 時採信 `CF-Connecting-IP`。預設 `172.16.0.0/12` 涵蓋 Docker 預設網段；
若 `docker network inspect lore-vault_default` 顯示子網是 `192.168.x.x`，把 `LORE_VAULT_UI_TRUSTED_PROXIES` 設成該子網。
不要把整個區網加進去：直連對外埠時，區網內的人就能偽造來源 IP。

### 直接綁對外 IP

`.env` 設 `LORE_VAULT_BIND=0.0.0.0`（或特定網卡 IP）與 `LORE_VAULT_PORT`。**發布的是沒有 TLS 的純 HTTP**：

- bearer token 以明文傳送，任何能看到流量的人都能取得完整存取權
- UI 登入 cookie 預設帶 `Secure`，瀏覽器在純 http（localhost 除外）下不會送回，UI 無法登入

所以一定要在前面放 TLS 反向代理，客戶端一律連 `https://`。以 Caddy 為例：

```text
vault.example.com {
    reverse_proxy 127.0.0.1:5056
}
```

反向代理跑在主機上時 `LORE_VAULT_BIND` 維持 `127.0.0.1` 即可，不必綁 `0.0.0.0`。
`LORE_VAULT_UI_COOKIE_SECURE=false` 只適合完全信任的本機測試，不要用在對外部署。

## 備份

```bash
docker compose exec lore-vault python -m lore_vault.storage.backup
```

`VACUUM INTO` 到 `/backups`、驗證完整性後才落地，保留最近幾份（`LORE_VAULT_BACKUP_KEEP`）。
用 cron（Linux）或工作排程器（Windows）每天跑一次；doctor 的 `backup.recent` 會在最近一次備份過舊時變紅。

Linux：`./backups` 若由 Docker 自動建立會是 root 擁有，容器使用者（uid 10001）寫不進去。先建立並改擁有者：

```bash
mkdir -p backups && sudo chown 10001:10001 backups
```

`./backups` 在 repo 目錄內，不要提交進版控。

## 還原

1. 停服務：`docker compose stop lore-vault`
2. 用服務映像把備份複製回 volume（以容器使用者執行，擁有者正確）：

   ```bash
   docker compose run --rm --no-deps --entrypoint sh lore-vault -c \
     'cp /backups/<備份檔名> /data/lore.db && rm -f /data/lore.db-wal /data/lore.db-shm'
   ```

3. `docker compose up -d`，確認 `status` 的 doctor 無 fail

## 升級

```bash
docker compose exec lore-vault python -m lore_vault.storage.backup   # 先備份
git pull
docker compose up -d --build
```

schema 遷移在啟動時自動執行。ollama／cloudflared 映像要更新時另跑 `docker compose pull`。

## 疑難排解

| 症狀 | 檢查 |
|---|---|
| lore-vault 一直沒啟動、`ollama-pull` 失敗 | `docker compose logs ollama-pull`；多半是網路拉不到模型，修好後 `docker compose up -d` 重跑 |
| recall 只有關鍵字結果、doctor 的 embedding 項目 fail | `LORE_VAULT_EMBEDDING_BASE_URL` 是否指到正確的 Ollama；ollama profile 要設 `http://ollama:11434` |
| 客戶端 401 | token 不一致；`.env` 設了新 token 後要重新登記客戶端 |
| 客戶端 403 或被轉址 | 前面有 Cloudflare Access，客戶端需要 service token |
| UI 無法登入（純 http 對外） | 見「直接綁對外 IP」，需要 TLS |
| 備份失敗、權限不足 | Linux 上 `./backups` 的擁有者，見「備份」 |
| 忘了管理員密碼或 token | token：`docker compose exec lore-vault cat /data/secrets/api-token`（或 `.env`）；管理員密碼只在首次建立時產生 |
