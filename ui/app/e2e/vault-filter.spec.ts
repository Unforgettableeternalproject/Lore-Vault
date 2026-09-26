// 頁面內 vault 篩選器與左側欄共用同一份狀態：在頁面選 → 側欄同步；點側欄 → 頁面篩選器同步，
// 換頁（筆記 → 文件 → 記憶層 → 檢索）範圍不變，列表內容確實依篩選變化。
import { expect, test, type Page } from '@playwright/test';

import { createVault, login, watchPage, writeNote } from './helpers';

const A = { key: 'folder/e2e-vpick-a', display: 'E2E 篩選甲' };
const B = { key: 'folder/e2e-vpick-b', display: 'E2E 篩選乙' };

test.beforeAll(async ({ request }) => {
  await createVault(request, A.key, A.display);
  await createVault(request, B.key, B.display);
  await writeNote(request, { vault: A.key, title: 'vpickalpha 甲的筆記', body: 'vpickalpha 內容。' });
  await writeNote(request, { vault: B.key, title: 'vpickbeta 乙的筆記', body: 'vpickbeta 內容。' });
});

function picker(page: Page) {
  return page.getByRole('combobox', { name: 'vault 篩選' });
}

function sideItem(page: Page, name: string) {
  return page.getByRole('complementary', { name: '導覽與 vault' }).getByRole('button', { name: new RegExp(name) });
}

test('頁面 vault 篩選器與側欄同步', async ({ page }) => {
  const watch = await watchPage(page);
  await login(page);
  await page.getByRole('navigation', { name: '主導覽' }).getByRole('link', { name: '筆記' }).click();
  await expect(page.getByRole('heading', { name: '筆記', level: 1 })).toBeVisible();
  await expect(picker(page)).toHaveValue('本 space 全部');

  // 頁面篩選器：輸入搜尋、Enter 選取 → 側欄同一項標為選取、列表只剩該 vault
  await picker(page).click();
  await expect(picker(page)).toHaveAttribute('aria-expanded', 'true');
  await picker(page).fill('篩選甲');
  await expect(page.getByRole('option')).toHaveCount(1);
  await picker(page).press('Enter');
  await expect(picker(page)).toHaveAttribute('aria-expanded', 'false');
  await expect(picker(page)).toHaveValue(A.display);
  await expect(sideItem(page, A.display)).toHaveAttribute('aria-pressed', 'true');
  await expect(sideItem(page, '本 space 全部')).toHaveAttribute('aria-pressed', 'false');
  const table = page.getByRole('table', { name: '筆記列表' });
  await expect(table).toContainText('vpickalpha');
  await expect(table).not.toContainText('vpickbeta');

  // 反向：點側欄 → 頁面篩選器同步
  await sideItem(page, B.display).click();
  await expect(picker(page)).toHaveValue(B.display);
  await expect(table).toContainText('vpickbeta');
  await expect(table).not.toContainText('vpickalpha');

  // 換頁範圍不變：文件、記憶層、檢索的篩選器都顯示同一個 vault
  for (const link of ['文件', '記憶層', '檢索']) {
    await page.getByRole('navigation', { name: '主導覽' }).getByRole('link', { name: link }).click();
    await expect(page.getByRole('heading', { name: link, level: 1 })).toBeVisible();
    await expect(picker(page)).toHaveValue(B.display);
  }

  // 檢索頁用篩選器選「本 space 全部」→ 側欄同步
  await picker(page).click();
  await page.getByRole('option', { name: /本 space 全部/ }).click();
  await expect(sideItem(page, '本 space 全部')).toHaveAttribute('aria-pressed', 'true');
  await expect(sideItem(page, B.display)).toHaveAttribute('aria-pressed', 'false');

  await watch.assertClean();
});
