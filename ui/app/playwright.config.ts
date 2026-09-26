// E2E：啟動一個臨時的 Lore Vault 服務（暫存資料庫與 blob 目錄、固定測試 token、只開文件 worker），
// 啟動前以 e2e/seed_account.py 在暫存資料庫建立測試專用的 UI 帳號（A23），
// 由它提供 `npm run build` 產出的 dist。不連接也不影響執行中的正式服務。
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { defineConfig, devices } from '@playwright/test';

import { E2E_DISPLAY, E2E_PASSWORD, E2E_PORT, E2E_TOKEN, E2E_USER } from './e2e/constants';

const repoRoot = resolve(import.meta.dirname, '../..');
const scratch = mkdtempSync(join(tmpdir(), 'lore-vault-e2e-'));

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: `http://localhost:${E2E_PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `uv run python ui/app/e2e/seed_account.py && uv run uvicorn --factory lore_vault.api.app:create_app --host 127.0.0.1 --port ${E2E_PORT}`,
    cwd: repoRoot,
    url: `http://127.0.0.1:${E2E_PORT}/healthz`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      // 不讀使用者的設定檔與家目錄
      LORE_VAULT_CONFIG: '',
      HOME: scratch,
      USERPROFILE: scratch,
      LORE_VAULT_API_TOKEN: E2E_TOKEN,
      // 只給 seed_account.py 用（服務本身不讀這些）
      E2E_UI_USER: E2E_USER,
      E2E_UI_PASSWORD: E2E_PASSWORD,
      E2E_UI_DISPLAY: E2E_DISPLAY,
      LORE_VAULT_DATABASE_PATH: join(scratch, 'lore.db'),
      LORE_VAULT_UI_STATIC_DIR: resolve(import.meta.dirname, 'dist'),
      LORE_VAULT_API_ENRICH_WORKER: 'false',
      LORE_VAULT_API_EMBEDDING_WARMUP: 'false',
      // 文件流程要真的抽取：開文件 worker、blob 放暫存目錄
      LORE_VAULT_API_DOCUMENT_WORKER: 'true',
      LORE_VAULT_DOCUMENTS_BLOB_DIR: join(scratch, 'blobs'),
      // 語意模型指向不存在的位址：不論這台機器有沒有跑 Ollama，recall 都穩定走降級（lexical）
      LORE_VAULT_EMBEDDING_BASE_URL: 'http://127.0.0.1:9',
      LORE_VAULT_EMBEDDING_QUERY_TIMEOUT: '0.5',
      LORE_VAULT_EMBEDDING_TIMEOUT: '1',
    },
  },
});
