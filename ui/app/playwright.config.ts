// E2E：啟動一個臨時的 Lore Vault 服務（暫存資料庫、固定測試 token、不跑背景 worker），
// 由它提供 `npm run build` 產出的 dist。不連接也不影響執行中的正式服務。
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { defineConfig, devices } from '@playwright/test';

import { E2E_PORT, E2E_TOKEN } from './e2e/constants';

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
    command: `uv run uvicorn --factory lore_vault.api.app:create_app --host 127.0.0.1 --port ${E2E_PORT}`,
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
      LORE_VAULT_DATABASE_PATH: join(scratch, 'lore.db'),
      LORE_VAULT_UI_STATIC_DIR: resolve(import.meta.dirname, 'dist'),
      LORE_VAULT_API_ENRICH_WORKER: 'false',
      LORE_VAULT_API_EMBEDDING_WARMUP: 'false',
      LORE_VAULT_API_DOCUMENT_WORKER: 'false',
    },
  },
});
