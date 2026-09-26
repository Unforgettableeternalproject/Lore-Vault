// E2E 共用：CSP 違規收集、以測試帳號登入、以 bearer 直接建立測試資料、axe 無障礙掃描。
import AxeBuilder from '@axe-core/playwright';
import { expect, type APIRequestContext, type Page } from '@playwright/test';

import { E2E_PASSWORD, E2E_TOKEN, E2E_USER } from './constants';

const BEARER = { Authorization: `Bearer ${E2E_TOKEN}` };

export async function watchPage(page: Page) {
  await page.addInitScript(() => {
    const w = window as unknown as { __csp: string[] };
    w.__csp = [];
    document.addEventListener('securitypolicyviolation', (e: SecurityPolicyViolationEvent) => {
      w.__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  const consoleErrors: string[] = [];
  page.on('console', (msg) => {
    // 未登入時 session 檢查的 401 是預期的資源錯誤
    if (msg.type() === 'error' && !/status of 401/.test(msg.text())) consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => consoleErrors.push(String(err)));
  return {
    async assertClean() {
      const csp = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);
      expect(csp).toEqual([]);
      expect(consoleErrors).toEqual([]);
    },
  };
}

/** 以測試帳號登入。登入成功的判準是主內容出現（手機版側欄收在抽屜裡、預設看不到導覽）。 */
export async function login(page: Page) {
  await page.goto('/ui/');
  await page.getByLabel('帳號').fill(E2E_USER);
  await page.getByLabel('密碼').fill(E2E_PASSWORD);
  await page.getByRole('button', { name: '登入' }).click();
  await expect(page.getByRole('main')).toBeVisible();
}

/** 在頁面載入前設定主題偏好（prefs.ts 的 localStorage key）。 */
export async function presetTheme(page: Page, theme: 'dark' | 'light') {
  await page.addInitScript((t) => {
    try {
      window.localStorage.setItem('lore-vault.theme', t);
    } catch {
      // 儲存被封鎖時沿用預設（深色）；掃描結果會標出實際主題
    }
  }, theme);
}

export async function createVault(request: APIRequestContext, key: string, display: string, space = 'dev') {
  const resp = await request.post('/v1/vaults', { headers: BEARER, data: { space, key, display } });
  expect([201, 409]).toContain(resp.status());
}

export async function writeNote(
  request: APIRequestContext,
  data: { vault: string; title: string; body: string; topics?: string[]; supersedes?: string; space?: string },
): Promise<string> {
  const resp = await request.post('/v1/write', {
    headers: BEARER,
    data: { space: 'dev', author: 'e2e', ...data },
  });
  expect(resp.status()).toBe(201);
  return ((await resp.json()) as { id: string }).id;
}

export async function uploadDocument(
  request: APIRequestContext,
  vault: string,
  filename: string,
  text: string,
  space = 'dev',
): Promise<string> {
  const resp = await request.post('/v1/documents', {
    headers: BEARER,
    multipart: { vault, space, file: { name: filename, mimeType: 'text/markdown', buffer: Buffer.from(text, 'utf-8') } },
  });
  expect([200, 201]).toContain(resp.status());
  return ((await resp.json()) as { document_id: string }).document_id;
}

/** WCAG 2.1 A／AA 規則掃描；回傳違規（含節點選擇器）以便斷言失敗時直接看到問題。 */
export async function axeViolations(page: Page, context: string) {
  const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  return result.violations.map((v) => ({
    context,
    id: v.id,
    impact: v.impact,
    help: v.help,
    nodes: v.nodes.slice(0, 5).map((n) => `${n.target.join(' ')} :: ${n.failureSummary?.split('\n').slice(0, 3).join(' | ') ?? ''}`),
  }));
}
