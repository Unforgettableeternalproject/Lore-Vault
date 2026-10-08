# Lore Vault — Coding Agent 自架記憶服務 v0.1.0

### 本專案提供多語系 README

[![Static Badge](https://img.shields.io/badge/lang-en-red)](./README.md) [![Static Badge](https://img.shields.io/badge/lang-zh--tw-yellow)](./README.zh-tw.md)

「喔?! 這是Bernie一直想做的那個? щ(ʘ╻ʘ)щ」
「他把以前的PM系統跟著一起重做了的樣子，神奇。」

「 o(\*°▽°\*)o」
「幹嘛?」

「好棒，這樣之後我的故事也能放在這裡嗎? 我有好多有的沒的正愁著沒地方放呢!╰(\*°▽°\*)╯」

「...」

「唉呦，表現出一點興奮的感覺嘛。( •̀ ω •́ )✧」
「我倒是沒有你那種奢侈的煩惱，還是先看看這東西能夠做到甚麼程度吧。 」

---

給 coding agent（Claude Code 等 MCP 客戶端）的自架記憶服務。agent 把架構決策、踩過的坑、
交接事項寫成 note，之後在同一個專案裡用關鍵字＋語意混合檢索找回來。

每一次讀寫都限定在專案範圍（vault）內；查詢先回標題與摘要，需要時才取全文，
不會一次灌爆上下文。

一個 `docker compose` 就能跑起來：HTTP API、MCP 端點、Web UI 與備份都在同一個容器裡。

## 功能

- **專案範圍的記憶**：vault 由 git remote 自動對應，範圍在儲存層強制過濾；跨專案查詢必須明示
- **混合檢索**：SQLite FTS5 關鍵字＋embedding 向量（預設 Ollama `bge-m3`），先回索引、再取全文
- **ask**：把檢索到的 note 交給模型整理成附來源的逐點回答（選配，需要 OpenAI API key）
- **文件**：上傳文件後在背景抽取段落，與 note 一起檢索
- **分區**：`dev`（開發）、`lore`（世界觀）、`personal`（私人）三個 space 互不混雜
- **MCP**：服務內建 Streamable HTTP 端點 `/mcp`，`claude mcp add --transport http` 直接連；
  另有本機 stdio 殼作為完整客戶端
- **Web UI**：搜尋、瀏覽與編輯 note，文件、vault 管理，健康檢查與維護
- **維運**：doctor 對帳檢查、`VACUUM INTO` 驗證式備份、啟動時自動遷移 schema
- **任務層（選用附加層）**：以 `python -m lore_vault.tasks` 或 MCP `tasks` 工具管理 OpenSpec 格式的 change 與 spec delta，
  archive 時把結論寫成 note；核心不 import 它，可整個不用，見
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#任務層選用附加層)

## 結構

```
src/lore_vault/       服務本體——HTTP API、MCP（HTTP 與 stdio 殼）、儲存、檢索、doctor
ui/app/               Web UI（Vite + TypeScript），由服務在 /ui 提供
docker/               容器設定（打包進映像的 config.toml）
integrations/claude/  給 Claude Code 的 pm skill
integrations/remote/  客戶端安裝程式（install.py），隨 kit 一起發放
scripts/              kit 打包、備份與排程工具
docs/                 架構說明與 guides/
tests/                測試
agent_memory_spike/   本專案的前身：記憶層實驗
```

## 自架快速開始

需要 Docker（含 Compose v2.24 以上）。

```bash
git clone <本 repo 網址> lore-vault
cd lore-vault
cp .env.example .env          # 全部選配；預設啟用 ollama profile
docker compose up -d --build
```

首次啟動會拉 `bge-m3`（約 1.2 GB），模型就緒後 lore-vault 才啟動。

**Profile**（在 `.env` 的 `COMPOSE_PROFILES` 設定，逗號分隔）：

| Profile    | 附帶                                       | 說明                                                                                                                |
| ---------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| `ollama` | Ollama 容器，首次啟動自動拉 embedding 模型 | 還需要`LORE_VAULT_EMBEDDING_BASE_URL=http://ollama:11434`（範本已設好）；沒設的話 embedding 會去連主機上的 Ollama |
| `tunnel` | cloudflared，使用`TUNNEL_TOKEN`          | 在 Cloudflare 把 tunnel 的公開主機名指向`http://lore-vault:8000`；不必開任何對外埠                                |

⚠️ 預設只綁 `127.0.0.1:5056`。設 `LORE_VAULT_BIND=0.0.0.0` 發布的是**沒有 TLS 的純 HTTP**：
bearer token 以明文傳送，UI 的 `Secure` 登入 cookie 也無法運作。
前面要自接 TLS 反向代理（Caddy、nginx 等），或改用 `tunnel` profile。

**token 與管理員密碼**——只在首次啟動產生，並在 log 印一次。`.env` 有設的值一律優先，
不會另外產生。

```bash
docker compose logs lore-vault          # 首次啟動的 log 會印出一次
# 或直接讀 volume 內的檔案
docker compose exec lore-vault cat /data/secrets/api-token
docker compose exec lore-vault cat /data/secrets/initial-admin-password
```

瀏覽器開 [http://127.0.0.1:5056/ui](http://127.0.0.1:5056/ui)，用管理員帳號登入（帳號名預設同 `LORE_VAULT_PRINCIPAL`，
即 `owner`），登入後改密碼。

> 備份寫到主機的 `./backups`（`LORE_VAULT_HOST_BACKUP_DIR`）；資料庫本身在 named volume
> `lore-vault-data`。**不要用 `docker compose down -v`**——`-v` 會刪掉這個 volume。

設定、對外公開、備份還原、升級與疑難排解見[自架指南](docs/guides/SELF-HOST.md)。

## 連接 agent

有兩種接法，兩者共用同一份工具定義：

|                   | HTTP 模式（建議）                                                    | 完整殼模式                                          |
| ----------------- | -------------------------------------------------------------------- | --------------------------------------------------- |
| 連線              | Claude Code 直連`<服務位址>/mcp`（Streamable HTTP）                | 本機 venv 跑 stdio 殼，殼再呼叫服務的 HTTP API      |
| 客戶端需求        | Claude Code CLI                                                      | Claude Code CLI、Python ≥ 3.12、uv、kit 內的 wheel |
| token 存放        | `~/.claude.json`                                                   | `~/.lore-vault/mcp.env`，不進 `~/.claude.json`  |
| `vault_resolve` | 由 agent 傳`remote_url`（`git remote get-url origin`）或 `key` | 殼用工作目錄自動推算                                |
| `upload`        | 檔名＋base64 內容                                                    | 本機路徑                                            |
| 服務斷線時        | 工具直接失敗                                                         | 唯讀的本地快照（`degraded=true`）                 |
| 更新              | 服務端升級即可，客戶端`/mcp` 重連                                  | 客戶端重裝 wheel（`install.py --update`）         |

最短路徑——登記 HTTP 端點：

```bash
claude mcp add --transport http -s user lore-vault http://127.0.0.1:5056/mcp \
  --header "Authorization: Bearer <token>"
```

`--header` 會吞掉後面的位置參數，**一定放在名稱與網址之後**。
之後在 Claude Code 裡呼叫 `mcp__lore-vault__status`，`ok: true` 即完成。

其他機器、Cloudflare Access、完整殼模式，或要一併裝 pm skill 時，打包 kit 用安裝程式，
見[客戶端安裝](docs/guides/REMOTE-INSTALL.md)。

## 從原始碼開發

以下是開發者用的路徑。**只是要架 Lore Vault 的話走上面的 Docker 快速開始**。

```bash
# 環境（由 uv 管理；.python-version 釘在 3.14）
uv sync

# 跑測試（tests/ 與 agent_memory_spike/ 既有測試）
uv run pytest

# lint 與格式檢查
uv run ruff check .
uv run ruff format --check .

# 在本機啟動服務
uv run uvicorn --factory lore_vault.api.app:create_app --host 127.0.0.1 --port 8000
```

> hook 腳本會在每次編輯時由系統 Python 直接執行，所以 hook 路徑上只能用標準庫。

## 文件

- [docs/guides/SELF-HOST.md](docs/guides/SELF-HOST.md)：自架指南（設定、profile、對外公開、備份還原、疑難排解）
- [docs/guides/REMOTE-INSTALL.md](docs/guides/REMOTE-INSTALL.md)：客戶端安裝（HTTP 模式／完整殼、安裝程式）
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：分層、資料模型、MCP 介面

Lore Vault 單一使用者自架；多使用者共享尚未支援。不依賴任何其他專案的程式或環境，可獨立部署。
