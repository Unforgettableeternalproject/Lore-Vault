// Vault 管理流程（T-82）：建立 lore vault → 新增別名 → 移到 personal → 刪除 → 墓碑列表。
// 刪除前以 bearer 寫一則筆記，刪除後才有墓碑可斷言（空 vault 刪除不留墓碑）。
import { expect, test } from '@playwright/test';

import { E2E_TOKEN } from './constants';
import { login, watchPage } from './helpers';

const KEY = 'lore/e2e-maint';
const NOTE_TITLE = 'e2e 維護流程的筆記';

test('建立 lore vault → 新增別名 → 移到 personal → 刪除 → 墓碑列表', async ({ page, request }) => {
  const watch = await watchPage(page);
  await login(page);

  // 切到 LORE
  await page.getByRole('button', { name: /DEV · 專案開發/ }).click();
  await page.getByRole('menuitemradio', { name: /LORE · 世界觀/ }).click();
  await expect(page.locator('.lv-app')).toHaveAttribute('data-zone', 'history');

  // 建立 vault（key 預填 lore/ 前綴）
  await page.getByRole('link', { name: 'Vault', exact: true }).click();
  await page.getByRole('button', { name: '+ 建立 vault' }).click();
  const form = page.getByRole('form', { name: '建立 vault' });
  await form.getByLabel('KEY').fill(KEY);
  await form.getByLabel('顯示名稱').fill('E2E 維護');
  await form.getByRole('button', { name: '建立於 LORE' }).click();
  const row = page.locator(`[data-vault="${KEY}"]`);
  await expect(row).toContainText('E2E 維護');
  await expect(row).toContainText('手動');

  const write = await request.post('/v1/write', {
    headers: { Authorization: `Bearer ${E2E_TOKEN}` },
    data: { space: 'lore', vault: KEY, title: NOTE_TITLE, body: '刪除後要留下墓碑。', author: 'e2e' },
  });
  expect(write.ok()).toBe(true);

  // 維護：新增別名
  await row.getByRole('link', { name: '維護 E2E 維護' }).click();
  await expect(page.getByRole('heading', { name: 'E2E 維護' })).toBeVisible();
  await page.getByLabel('新別名').fill('lore/e2e-maint-old');
  await page.getByRole('button', { name: '導向此 vault' }).click();
  await expect(page.getByRole('button', { name: '移除別名 lore/e2e-maint-old' })).toBeVisible();

  // 換 space：dev 停用、personal 可用；兩段式確認顯示新 key
  await expect(page.getByRole('button', { name: /移到 DEV/ })).toBeDisabled();
  await page.getByRole('button', { name: /移到 PERSONAL/ }).click();
  const moveDialog = page.getByRole('dialog');
  await expect(moveDialog.getByTestId('delete-plan')).toContainText('lore/e2e-maint → personal/e2e-maint');
  await expect(moveDialog.getByTestId('delete-plan')).toContainText('personal/e2e-maint-old');
  await moveDialog.getByRole('button', { name: '確認搬移' }).click();
  await expect(page.getByTestId('move-done')).toContainText('personal/e2e-maint');
  await page.getByRole('button', { name: /切換到 PERSONAL/ }).click();

  await expect(page.locator('.lv-app')).toHaveAttribute('data-zone', 'echoes');
  await expect(page).toHaveURL(/\/ui\/maint\/personal%2Fe2e-maint$/);
  await expect(page.getByRole('heading', { name: 'E2E 維護' })).toBeVisible();
  await expect(page.getByRole('button', { name: '移除別名 personal/e2e-maint-old' })).toBeVisible();

  // 刪除：輸入完整 key 前不能確認
  await page.getByRole('button', { name: '刪除這個 vault…' }).click();
  const delDialog = page.getByRole('dialog');
  await expect(delDialog.getByTestId('delete-plan')).toContainText('1 則筆記');
  const confirm = delDialog.getByRole('button', { name: '確認刪除' });
  await expect(confirm).toBeDisabled();
  await delDialog.getByRole('textbox').fill('personal/e2e-maint');
  await confirm.click();

  // 回到維護頁：全 space 墓碑列出剛刪的筆記，vault 已刪除所以不能還原
  await expect(page).toHaveURL(/\/ui\/maint$/);
  const graves = page.getByTestId('tombstones');
  const grave = graves.locator('li', { hasText: NOTE_TITLE });
  await expect(grave).toContainText('vault 已刪除');
  await expect(grave.getByRole('button', { name: `還原 ${NOTE_TITLE}` })).toBeDisabled();
  await expect(page.locator('[data-vault]')).toHaveCount(0);

  await watch.assertClean();
});
