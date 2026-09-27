# 交接（2026-09-27）

**Open Notebook 正式棄用（2026-09-27）**：所有 agent 的專案記憶改由 Lore Vault 提供；舊 PM 容器（5055）只留作觀察期退路，不再寫入。

先讀 [DECISIONS.md](DECISIONS.md)（A1～A23、D5、D11、D12）、[DEVELOPMENT.md](DEVELOPMENT.md)、[guides/REMOTE-INSTALL.md](guides/REMOTE-INSTALL.md)、[design/UI-HANDOFF-NOTES.md](design/UI-HANDOFF-NOTES.md)。

## 現況

| 項目 | 狀態 |
|---|---|
| 分支 | 工作都已併回 `develop`；`main` 只有初始文件 commit |
| 服務 | `lore-vault` 容器（127.0.0.1:5056，schema v13），資料在 named volume `lore-vault-data`；note 1455＋，concept 1608 |
| 切換 | 完成：本機與 Clockwork-Community 的 MCP、hook、pm skill、全域與各專案 CLAUDE.md 皆指 Lore Vault；`pm`／`pm-api` 皆轉 5056 |
| 排程 | `AgentMemoryPipeline` 03:30 由本 repo 執行（09-27 首輪成功、成功後自動 `--push-concepts`）；`LoreVaultBackup` 04:30 |
| ask() | D11 已上線（note 範圍）；同輪修 FTS 中文整句片語 bug、`RRF_K` 20，59 題回歸 top-10 72.5%→85% |
| UI | 三輪修正已部署；第四輪起派新代理，從 UI-HANDOFF-NOTES 開始 |
| 其他機器 | 自用安裝器：主機 `scripts/build_remote_kit.py` 打 kit，目標機人類跑 `install.py`，報告貼回主機驗證（尚未實戰） |

## 觀察期（到 2026-09-29 前後）

1. 每天看 03:30 管線 log、`concept_push`、spool、concept_snapshot 的 doctor 是否全綠
2. 觀察期結束：停舊 PM 容器（5055）；停排程 `\PM Cache Sync`（本機）、`PM Cache Sync`／`PM Proxy`（Clockwork-Community）；封存 `~/.claude/pm/`、`pm-kit/`、`pm-cache/`；刪 `~/.claude.json`、`settings.json`、pm skill、weekly-compliance 的 `.bak-precutover`
3. TestSeperateMemorySystem 另開 PR 移除 spike

## D12 對外自架發佈（`feature/self-host`，已實作、未部署）

- 內容：服務內建 HTTP MCP `/mcp`、principal 可設定（`LORE_VAULT_PRINCIPAL`，預設 `owner`）、首次啟動自動產生 token／管理員（`/data/secrets/`）、compose profile `ollama`／`tunnel` 與 `LORE_VAULT_BIND`／`PORT`、安裝器通用化（HTTP 免殼模式）；指南見 `docs/guides/SELF-HOST.md`
- **重新部署前**：本機 `.env` 已補 `LORE_VAULT_PRINCIPAL=UEPBernie`（2026-09-27）；缺了新 note 會記成 `owner`，doctor `notes.principal_agreement` 會 warn
- 未實測：docker 實際啟動、Linux 的 secrets 權限、ollama profile 拉模型閘門、cloudflared 連線、`claude mcp add --transport http` 實連
- 合併 `main` 前待辦：LICENSE（授權未定）；README（英／繁中）依 Chatroom 格式重寫但仍不追蹤，開頭對話為草稿待艾斯維爾改寫；沒有預建映像（CI 未做），目前是 `up -d --build`

## 下一輪

- 文件段落（chunk）的 ask 評估：需要時由艾斯維爾提供測試語料
- 召回剩餘弱點：向量端判別力不足的個案（診斷見 D11 紀錄）

## 待裁決

- D5 spike 資料目錄改名、實驗中間產物去留
- UI 為 WCAG AA 覆寫的色票是否回改設計系統
- 摘要長度（改短需重算，有 API 費用）
- 遠端機器是否也收 episode
- Dystopia 的 CLAUDE.md 仍以 mempal 當長期記憶，與全域規則衝突
- JSAI-API／JSAI-Functions／JSAI-Web／U.E.P's Mind Reflourished 的 CLAUDE.md 有進版控，記憶段改動尚未 commit

## 已知問題

- 全套 pytest 偶有 1 項計時測試失敗（重跑即過）
- `enrich.backlog` 等待時間以 note 的 `updated` 起算，匯入的舊 note 會顯示極大值
- 登入鎖定是全域的（解鎖：`docker exec lore-vault python -m lore_vault.cli.admin ui-unlock --yes`）；Eternity 後門待做
- 刪除保留全文快照，需定期 `cli.admin purge-tombstones`
- `ask` 連續快速發問會撞 OpenAI TPM 限流（回 429 `ask_rate_limited`）
