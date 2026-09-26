// E2E 共用：CSP 違規收集、以測試帳號登入、以 bearer 直接建立測試用 vault。
import { expect, type APIRequestContext, type Page } from '@playwright/test';

import { E2E_PASSWORD, E2E_TOKEN, E2E_USER } from './constants';

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

export async function login(page: Page) {
  await page.goto('/ui/');
  await page.getByLabel('帳號').fill(E2E_USER);
  await page.getByLabel('密碼').fill(E2E_PASSWORD);
  await page.getByRole('button', { name: '登入' }).click();
  await expect(page.getByRole('navigation', { name: '主導覽' })).toBeVisible();
}

export async function createVault(request: APIRequestContext, key: string, display: string, space = 'dev') {
  const resp = await request.post('/v1/vaults', {
    headers: { Authorization: `Bearer ${E2E_TOKEN}` },
    data: { space, key, display },
  });
  expect([201, 409]).toContain(resp.status());
}
