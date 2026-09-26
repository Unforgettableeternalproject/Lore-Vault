# 交接（2026-09-26）

下一輪主題：**正式切換（第 9 階段）** 與 **UI／UX 修正**。先讀 [DECISIONS.md](DECISIONS.md)（A1～A23）、[TASKS.md](TASKS.md)、[MIGRATION.md](MIGRATION.md)、[DEVELOPMENT.md](DEVELOPMENT.md)。

## 現況

| 項目 | 狀態 |
|---|---|
| 分支 | `feature/bootstrap`、`feature/ui` 皆已併回 `develop`；`main` 只有初始文件 commit；下一輪切換與 UI 修正各自開 `feature/*` |
| 服務 | `lore-vault` 容器執行中（`127.0.0.1:5056`，schema v13），資料在 named volume `lore-vault-data` |
| 正式資料 | 舊 PM 1454 則（1452 + 2 則孤兒）已匯入 dev space，摘要與向量全數補齊，principal 為 `UEPBernie` |
| UI | `https://pm.unforgettableeternalproject.com/ui/` 可用（list 摘要已改公平分配，實測 50 則零省略）；本地帳密登入（`UEPBernie`），全域失敗 3 次鎖定、需人工解鎖 |
| Cloudflare | `pm` → 5056（Access 改為 Bypass）；`pm-api` → 5055（**仍是舊 PM**，service token 保護）；登入端點的 WAF 速率限制未設（免費方案規則已用在 Eternity） |
| 備份 | Windows 排程 `LoreVaultBackup` 每日 04:30（`scripts/backup.ps1`），備份在 `E:\ProgramFiles\Lore Vault\backups` |
| 舊 PM | Open Notebook 容器仍在 5055 執行，所有 agent 仍透過它讀寫記憶 |
| spike | 程式已併入本 repo 並改為 spool 推送／讀快照，但**全域 hook 與排程仍指向舊 repo**（TestSeperateMemorySystem） |

## 切換（第 9 階段）

每一步都需要艾斯維爾授權；Claude Code 自動模式會擋下改 `C:\ProgramData\cloudflared`、Windows 排程等操作，這類步驟由他本人執行。建議順序：

1. **補匯入差量**：舊 PM 在 2026-09-26 之後新寫的 note。`import_on export` → `map`（用 scratchpad 的定案 mapping，或重跑 `map` 後套用 A16～A17 的 key 規則）→ 容器內 `import`（冪等；需帶同一份 `--orphans-map`）。對帳須為綠。
2. **本機 MCP**：`~/.claude.json` 的 `open-notebook` 換成 `python -m lore_vault.mcp`（`.venv`，連 `http://127.0.0.1:5056`），範例見 DEVELOPMENT.md「MCP 殼」。
3. **記憶協定**：改寫 `~/.claude/skills/pm/SKILL.md`、全域 CLAUDE.md 的記憶段落、各專案 CLAUDE.md 的 `[PM] <repo>` 綁定敘述（Chatroom、Echo-Stream、Eternity、Lore-Vault、TestSeperateMemorySystem、U.E.P-s-Core）、claude-codex-pipeline plugin 寫死的 `mcp__open-notebook__*`（repo 與 `~/.claude/plugins/cache/uep-pipeline/` 兩處）、`~/.claude/pm-kit` 去留。
4. **hook**：`~/.claude/settings.json` 的 `SessionStart`（health alert）、`Stop`、`PreToolUse` 一次整批換成本 repo 路徑，舊路徑完全移除（並存會雙倍注入）；建立 `client.env`（服務 URL、token、快照路徑，且 `LORE_VAULT_CONCEPT_SNAPSHOT` 與 MCP 的 `concept_snapshot_path` 指向同一檔——doctor 有檢查）。
5. **concept 重新編號**：`agent_memory_spike/renumber_concepts.py` 對現行 `concepts.json`／`injections.jsonl` 產生新檔（乾跑已驗證：265 筆改號、75 筆注入標 `ambiguous_ids`），再以 `pipeline.py --push-concepts` 寫入服務。
6. **排程**：`AgentMemoryPipeline` 改指向本 repo 的 `run_pipeline.ps1`（腳本已改用本 repo `.venv`，`--run` 成功後接 `--push-concepts`；裁決者 allowlist 在 `.claude/settings.local.json`，須先於排程改指向存在）。
7. **`pm-api` 切到 5056**：改 `C:\ProgramData\cloudflared\config.yml`（不是 `~/.cloudflared`）後 `Restart-Service cloudflared`。與第 2、8 步同時做。
8. **其他機器**：裝 MCP 殼（帶 bearer 與 CF Access service token，`pm-proxy.py` 退役）。
9. **觀察 1–2 天**後：舊 PM 容器轉唯讀備援、之後停用；TestSeperateMemorySystem 另開 PR 移除 spike。

回退：ingress 改回 5055、`~/.claude.json` 換回 `open-notebook` 即可；舊容器在觀察期內不要停。

## 待裁決

- **D5** spike 資料目錄是否從 `~/.claude/agent-memory-spike/` 改名、實驗中間產物去留（MCP 殼的 `snapshot_dir` 預設值也卡在這裡）。
- UI 為過 WCAG AA 覆寫了設計系統的幾組色票（`app.css` 的 `--lv-*`），是否回頭改設計系統本身。
- UI 的快捷鍵字母對應、1100／900px 兩個自訂斷點、手機把深淺色與登出移進抽屜。
- 摘要平均 206 字，比 D4 原意（1–2 句）長；要更短需調 prompt 並重算（會產生 API 費用）。

## 已知問題

- 全套 pytest 偶有 1 項失敗（重跑即過），推測為子行程抽取的計時測試；耗時已增至約 2 分鐘。
- `enrich.backlog` 的等待時間以 note 的 `updated` 起算，匯入的舊 note 會顯示極大值；應改以入列時間計。
- UI：抽屜沒有焦點陷阱、主內容沒設 `inert`；列表用 `<a role="row">` 蓋掉連結語意；輸入框焦點框對比未人工確認；Cinzel 字型未載入。
- 刪除改為保留全文快照（A22），商業原文會留在墓碑與備份中，需定期 `cli.admin purge-tombstones`。
- 登入鎖定是全域的：外人連猜 3 次即可把 UI 鎖住（Bearer／MCP 不受影響），解鎖用 `docker exec lore-vault python -m lore_vault.cli.admin ui-unlock --yes`。Eternity 後門（查看登入紀錄與解鎖）待做，服務層函式已備好。

## UI／UX 下一輪

艾斯維爾已在實際使用中發現問題，下一輪由他提出清單。設計稿在 `ui/design-source/`，實作計畫在 `docs/design/UI-IMPLEMENTATION-PLAN.md`，一鍵檢查 `cd ui/app && npm run check`。
