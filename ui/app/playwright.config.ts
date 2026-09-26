// E2E：啟動一個臨時的 Lore Vault 服務（暫存資料庫與 blob 目錄、固定測試 token、只開文件 worker），
// 啟動前以 e2e/seed_account.py 在暫存資料庫建立測試專用的 UI 帳號（A23），
// 由它提供 `npm run build` 產出的 dist。不連接也不影響執行中的正式服務。
//
// 暫存目錄：config 在主程序與每個 worker 都會載入，只有主程序建立目錄（經環境變數交給 worker 沿用），
// 主程序結束時刪除；刪不掉的（例如 Windows 上服務還沒放開 lore.db）留到下次執行時清掉。
import { mkdtempSync, readdirSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { defineConfig, devices } from '@playwright/test';

import { E2E_DISPLAY, E2E_PASSWORD, E2E_PORT, E2E_TOKEN, E2E_USER } from './e2e/constants';

const repoRoot = resolve(import.meta.dirname, '../..');
const SCRATCH_ENV = 'LORE_VAULT_E2E_SCRATCH';
const SCRATCH_PREFIX = 'lore-vault-e2e-';
/** 超過這個時間的舊暫存目錄視為上次執行的殘留（避免誤刪另一個正在跑的 E2E） */
const STALE_MS = 10 * 60 * 1000;

function removeDir(dir: string): boolean {
  try {
    rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    return true;
  } catch {
    // 檔案仍被占用（服務尚未完全結束）：留給下次執行清理
    return false;
  }
}

function sweepStale(keep: string): void {
  const root = tmpdir();
  let names: string[];
  try {
    names = readdirSync(root);
  } catch {
    return; // 讀不到系統暫存目錄就不清，不影響測試
  }
  const now = Date.now();
  for (const name of names) {
    if (!name.startsWith(SCRATCH_PREFIX)) continue;
    const dir = join(root, name);
    if (dir === keep) continue;
    try {
      if (now - statSync(dir).mtimeMs < STALE_MS) continue;
    } catch {
      continue; // 目錄剛被別人刪掉
    }
    removeDir(dir);
  }
}

function scratchDir(): string {
  const inherited = process.env[SCRATCH_ENV];
  if (inherited) return inherited; // worker：沿用主程序建立的目錄
  const dir = mkdtempSync(join(tmpdir(), SCRATCH_PREFIX));
  process.env[SCRATCH_ENV] = dir;
  sweepStale(dir);
  process.on('exit', () => {
    removeDir(dir);
  });
  return dir;
}

const scratch = scratchDir();

// 手機 smoke 只在行動裝置 profile 跑；其餘規格只在桌面 Chromium 跑一次。
// 登入失敗計數是全域的（A23，3 次即鎖定、成功不歸零），login.spec 故意錯一次密碼且只能跑一次。
const MOBILE_SPEC = /mobile\.spec\.ts$/;

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: `http://localhost:${E2E_PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] }, testIgnore: MOBILE_SPEC },
    // Pixel 7 為 Chromium 行動 profile（觸控、DPR、UA）；iPhone profile 需要另裝 WebKit
    { name: 'mobile', use: { ...devices['Pixel 7'] }, testMatch: MOBILE_SPEC },
  ],
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
