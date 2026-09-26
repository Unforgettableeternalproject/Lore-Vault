// 登入 smoke：登入頁可載入 → 錯誤金鑰被拒 → 登入 → App Shell → 重整仍登入 → 登出。
// 同時斷言嚴格 CSP 下沒有任何違規（script／style／font 全部同源）。
import { expect, test } from '@playwright/test';

import { E2E_TOKEN } from './constants';

test('登入、App Shell 與登出', async ({ page, context }) => {
  await page.addInitScript(() => {
    const w = window as unknown as { __csp: string[] };
    w.__csp = [];
    document.addEventListener('securitypolicyviolation', (e: SecurityPolicyViolationEvent) => {
      w.__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  const consoleErrors: string[] = [];
  page.on('console', (msg) => {
    // 未登入時 session 檢查與錯誤金鑰的 401 是預期的資源錯誤
    if (msg.type() === 'error' && !/status of 401/.test(msg.text())) consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => consoleErrors.push(String(err)));

  const resp = await page.goto('/ui/');
  expect(resp?.headers()['content-security-policy']).toContain("script-src 'self'");
  await expect(page.getByRole('heading', { name: '登入' })).toBeVisible();

  const key = page.getByLabel('存取金鑰');
  await key.fill('definitely-not-the-token');
  await page.getByRole('button', { name: '登入' }).click();
  await expect(page.getByRole('alert')).toHaveText('存取金鑰不正確');

  await key.fill(E2E_TOKEN);
  await page.getByRole('button', { name: '登入' }).click();
  await expect(page.getByRole('navigation', { name: '主導覽' })).toBeVisible();
  await expect(page.getByRole('button', { name: /DEV · 專案開發/ })).toBeVisible();

  const cookies = await context.cookies();
  const session = cookies.find((c) => c.name.endsWith('lv_session'));
  expect(session).toBeDefined();
  expect(session!.httpOnly).toBe(true);
  expect(session!.sameSite).toBe('Strict');
  expect(session!.value).not.toContain(E2E_TOKEN);

  // 重整後 session 仍有效；深層網址走 SPA fallback
  await page.goto('/ui/health');
  await expect(page.getByRole('heading', { name: '系統健康' })).toBeVisible();

  // 深淺色與 space 切換
  await page.getByRole('button', { name: '切換為淺色' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.getByRole('button', { name: /DEV · 專案開發/ }).click();
  await page.getByRole('menuitemradio', { name: /LORE · 世界觀/ }).click();
  await expect(page.getByRole('button', { name: /LORE · 世界觀/ })).toBeVisible();
  await expect(page.locator('.lv-app')).toHaveAttribute('data-zone', 'history');

  // 登出後重整仍在登入頁
  await page.getByRole('button', { name: '登出' }).click();
  await expect(page.getByRole('heading', { name: '登入' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: '登入' })).toBeVisible();

  const csp = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);
  expect(csp).toEqual([]);
  expect(consoleErrors).toEqual([]);
});
