// 登入 smoke：登入頁可載入 → 錯誤密碼被拒（顯示剩餘次數）→ 登入 → App Shell → 重整仍登入 → 登出。
// 失敗計數是全域的（A23，3 次即鎖定）：整個 E2E 只能在這裡錯 1 次，否則後面的 spec 會被鎖在外面。
// 同時斷言嚴格 CSP 下沒有任何違規（script／style／font 全部同源）。
import { expect, test } from '@playwright/test';

import { E2E_DISPLAY, E2E_PASSWORD, E2E_USER } from './constants';

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
    // 未登入時 session 檢查與錯誤密碼的 401 是預期的資源錯誤
    if (msg.type() === 'error' && !/status of 401/.test(msg.text())) consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => consoleErrors.push(String(err)));

  const resp = await page.goto('/ui/');
  expect(resp?.headers()['content-security-policy']).toContain("script-src 'self'");
  await expect(page.getByRole('heading', { name: '登入' })).toBeVisible();

  await expect(page.getByText(/目前剩餘 3 次/)).toBeVisible();
  const user = page.getByLabel('帳號');
  const password = page.getByLabel('密碼');
  await user.fill(E2E_USER);
  await password.fill('definitely-not-the-password');
  await page.getByRole('button', { name: '登入' }).click();
  await expect(page.getByRole('alert')).toHaveText('帳號或密碼錯誤，剩餘 2 次；失敗 3 次將鎖定，需人工解鎖');

  // 帳號比對不分大小寫
  await user.fill(E2E_USER.toLowerCase());
  await password.fill(E2E_PASSWORD);
  await page.getByRole('button', { name: '登入' }).click();
  await expect(page.getByRole('navigation', { name: '主導覽' })).toBeVisible();
  await expect(page.getByRole('button', { name: /DEV · 專案開發/ })).toBeVisible();

  const cookies = await context.cookies();
  const session = cookies.find((c) => c.name.endsWith('lv_session'));
  expect(session).toBeDefined();
  expect(session!.httpOnly).toBe(true);
  expect(session!.sameSite).toBe('Strict');
  expect(session!.value).not.toContain(E2E_PASSWORD);

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

  // 連線設定：帳號（principal）與顯示名稱、測試連線（無 body 的 /v1/status）、署名用顯示名稱
  await page.getByRole('link', { name: '連線設定' }).click();
  await expect(page.getByTestId('settings-principal')).toHaveText(E2E_USER);
  await expect(page.getByTestId('settings-display')).toHaveText(E2E_DISPLAY);
  await page.getByRole('button', { name: '測試連線' }).click();
  await expect(page.getByTestId('settings-test')).toContainText('可連線');

  // 登出後重整仍在登入頁
  await page.getByRole('button', { name: '登出', exact: true }).first().click();
  await expect(page.getByRole('heading', { name: '登入' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: '登入' })).toBeVisible();

  const csp = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);
  expect(csp).toEqual([]);
  expect(consoleErrors).toEqual([]);
});
