// 第三輪：頁碼分頁（每頁筆數、跳頁、日期區間）、側欄 vault 區段收合與限高、系統健康分類收合、
// 頂列工具鈕同尺寸，以及上述新元件在深淺主題的 axe。
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { E2E_TOKEN } from './constants';
import { axeViolations, createVault, login, presetTheme, watchPage, writeNote } from './helpers';

const VAULT = 'folder/e2e-paging';
const REPO = 'github.com/e2e/paging-repo';
const NOTES = 25;
const CONCEPTS = 12;

async function createConcepts(request: APIRequestContext) {
  const concepts = Array.from({ length: CONCEPTS }, (_, i) => ({
    id: `e2e-page-${i}`,
    statement: `pagingquartz 概念 ${i}`,
    kind: 'project-fact',
    scope: 'paging-repo',
    cue: 'cue',
    probe: 'probe',
    why: 'why',
    source_candidate: 'cand',
    from_signal: true,
    source_turns: [['p', 0]],
    source_files: ['src/a.py'],
    anchors: ['src/a.py'],
    surprisal: null,
    probe_result: null,
  }));
  const resp = await request.post('/v1/concepts', {
    headers: { Authorization: `Bearer ${E2E_TOKEN}` },
    data: { vault: REPO, mode: 'upsert', concepts },
  });
  expect(resp.ok(), await resp.text()).toBe(true);
}

test.beforeAll(async ({ request }) => {
  // 寫入會做查重（語意模型指向不存在的位址，逾時後降級），25 則需要一些時間
  test.setTimeout(180_000);
  await createVault(request, VAULT, 'E2E 分頁');
  for (let i = 0; i < NOTES; i++) {
    await writeNote(request, { vault: VAULT, title: `pagingquartz 筆記 ${String(i).padStart(2, '0')}`, body: `第 ${i} 則。` });
  }
  await createVault(request, REPO, 'E2E 分頁 repo');
  await createConcepts(request);
});

async function settle(page: Page) {
  await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
  await expect(page.locator('.lv-loading')).toHaveCount(0, { timeout: 15_000 });
}

function localDate(offsetDays = 0): string {
  const d = new Date(Date.now() + offsetDays * 86400_000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 以唯一的 key 搜尋後選取（顯示名稱可能互為前綴） */
async function pickVault(page: Page, key: string, name: string) {
  const picker = page.getByRole('combobox', { name: 'vault 篩選' });
  await picker.click();
  await picker.fill(key);
  await expect(page.getByRole('listbox').getByRole('option')).toHaveCount(1);
  await picker.press('Enter');
  await expect(picker).toHaveValue(name);
}

test('筆記分頁：每頁筆數、頁碼、上下頁與跳頁', async ({ page }) => {
  const watch = await watchPage(page);
  await login(page);
  await page.goto('/ui/notes');
  await settle(page);
  await pickVault(page, VAULT, 'E2E 分頁');
  const pager = page.getByRole('navigation', { name: '筆記分頁' });
  const rows = page.getByRole('table', { name: '筆記列表' }).locator('a[role=row]');
  // 預設每頁 30：25 則一頁放得下
  await expect(pager.getByTestId('pager-info')).toContainText(`共 ${NOTES} 則`);
  await expect(rows).toHaveCount(NOTES);
  // 每頁 10 → 3 頁
  await pager.getByRole('combobox', { name: '每頁筆數' }).selectOption('10');
  await expect(rows).toHaveCount(10);
  await expect(pager.getByTestId('pager-info')).toContainText('第 1–10 則');
  await pager.getByRole('button', { name: '下一頁' }).click();
  await expect(pager.getByTestId('pager-info')).toContainText('第 11–20 則');
  await expect(pager.getByRole('button', { name: '第 2 頁' })).toHaveAttribute('aria-current', 'page');
  await pager.getByRole('button', { name: '最後一頁' }).click();
  await expect(rows).toHaveCount(5);
  await expect(pager.getByRole('button', { name: '下一頁' })).toBeDisabled();
  // 跳頁：輸入 1 回第一頁
  const jump = pager.getByLabel(/跳至頁碼/);
  await jump.fill('1');
  await jump.press('Enter');
  await expect(pager.getByTestId('pager-info')).toContainText('第 1–10 則');
  // 三頁合起來不漏不重
  const titles = new Set<string>();
  for (const p of [1, 2, 3]) {
    const before = p === 1 ? null : await rows.first().locator('.lv-table__title').innerText();
    await pager.getByRole('button', { name: `第 ${p} 頁` }).click();
    // 等新的一頁真的載入（頁碼會先變、資料稍後到）：第一列換掉才算
    if (before !== null) await expect(rows.first().locator('.lv-table__title')).not.toHaveText(before);
    await expect(rows).toHaveCount(p === 3 ? NOTES - 20 : 10);
    for (const t of await rows.locator('.lv-table__title').allInnerTexts()) titles.add(t);
  }
  expect(titles.size).toBe(NOTES);
  await watch.assertClean();
});

test('記憶層與文件：篩選區塊與日期區間', async ({ page }) => {
  const watch = await watchPage(page);
  await login(page);
  await page.goto('/ui/memory');
  await settle(page);
  const panel = page.getByTestId('filter-panel');
  await expect(panel).toBeVisible();
  await pickVault(page, REPO, 'E2E 分頁 repo');
  const list = page.getByTestId('concepts');
  const pager = page.getByRole('navigation', { name: 'concept 分頁' });
  await expect(pager.getByTestId('pager-info')).toContainText(`共 ${CONCEPTS} 則`);
  await pager.getByRole('combobox', { name: '每頁筆數' }).selectOption('10');
  await expect(list.locator('li')).toHaveCount(10);
  await pager.getByRole('button', { name: '第 2 頁' }).click();
  await expect(list.locator('li')).toHaveCount(CONCEPTS - 10);
  // 日期區間含今天：全部；只取明天之後：沒有
  await panel.getByLabel('起日').fill(localDate(0));
  await panel.getByLabel('訖日').fill(localDate(0));
  await expect(pager.getByTestId('pager-info')).toContainText(`共 ${CONCEPTS} 則`);
  await panel.getByLabel('訖日').fill(localDate(1));
  await panel.getByLabel('起日').fill(localDate(1));
  await expect(page.getByText('沒有符合條件的 concept')).toBeVisible();
  await panel.getByRole('button', { name: '清除' }).click();
  await expect(pager.getByTestId('pager-info')).toContainText(`共 ${CONCEPTS} 則`);

  // 文件頁：同一個篩選區塊（vault＋日期）
  await page.goto('/ui/docs');
  await settle(page);
  await expect(page.getByTestId('filter-panel').getByTestId('date-range')).toBeVisible();
  await watch.assertClean();
});

test('側欄 vault 區段：可收合（記住狀態）、限高捲動、寫入位置在首屏', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 950 });
  const watch = await watchPage(page);
  await login(page);
  await page.goto('/ui/search');
  await settle(page);
  const toggle = page.getByRole('button', { name: /VAULTS · DEV/ });
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  const list = page.locator('.lv-vaults__list');
  // 限高：清單可捲動或內容本來就少；高度不超過約 9 項
  const box = (await list.boundingBox())!;
  expect(box.height).toBeLessThanOrEqual(9 * 34 + 2);
  // 寫入位置在首屏
  const target = (await page.locator('.lv-write-target').boundingBox())!;
  expect(target.y + target.height).toBeLessThanOrEqual(950);
  // 鍵盤收合：焦點留在按鈕上，清單隱藏
  await toggle.focus();
  await page.keyboard.press('Enter');
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(toggle).toBeFocused();
  await expect(list).toBeHidden();
  // 重新載入後維持收合
  await page.reload();
  await settle(page);
  await expect(page.getByRole('button', { name: /VAULTS · DEV/ })).toHaveAttribute('aria-expanded', 'false');
  await page.getByRole('button', { name: /VAULTS · DEV/ }).click();
  await expect(page.locator('.lv-vaults__list')).toBeVisible();
  await watch.assertClean();
});

test('系統健康：分類可收合（有 warn／fail 展開、全通過收合），頂列工具鈕同尺寸', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const watch = await watchPage(page);
  await login(page);
  await page.goto('/ui/health');
  await settle(page);
  const groups = page.locator('details.lv-check-group[data-category]');
  expect(await groups.count()).toBeGreaterThan(0);
  for (const g of await groups.all()) {
    const worst = await g.getAttribute('class');
    const open = await g.evaluate((el) => (el as HTMLDetailsElement).open);
    if (/--(fail|warn)\b/.test(worst ?? '')) expect(open, worst ?? '').toBe(true);
    else expect(open, worst ?? '').toBe(false);
  }
  // 收合的分類可用點擊展開
  const closedCategory = await page.locator('details.lv-check-group[data-category]:not([open])').first().getAttribute('data-category');
  if (closedCategory) {
    const closed = page.locator(`details.lv-check-group[data-category="${closedCategory}"]`);
    await closed.locator('summary').click();
    await expect(closed).toHaveJSProperty('open', true);
  }
  // 檢查清單在固定高度區塊內捲動
  const region = page.getByRole('region', { name: '檢查項目' });
  const overflow = await region.evaluate((el) => getComputedStyle(el).overflowY);
  expect(overflow).toBe('auto');

  // 頂列三顆工具鈕同高同寬
  const sizes = [];
  for (const name of ['快捷鍵說明', /切換為(淺色|深色)/, '登出']) {
    const b = (await page.locator('.lv-header__tools').getByRole('button', { name }).boundingBox())!;
    sizes.push([Math.round(b.width), Math.round(b.height)]);
  }
  expect(new Set(sizes.map((s) => s.join('x'))).size).toBe(1);
  expect(sizes[0]![0]).toBe(sizes[0]![1]);
  await watch.assertClean();
});

for (const theme of ['dark', 'light'] as const) {
  test(`axe：新元件（分頁、篩選區塊、側欄收合、健康分類、記憶層 EmptyState）· ${theme}`, async ({ page }) => {
    test.setTimeout(90_000);
    await presetTheme(page, theme);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await login(page);
    const found: unknown[] = [];
    const scan = async (ctx: string) => found.push(...(await axeViolations(page, `${theme} · ${ctx}`)));
    await page.goto('/ui/notes');
    await settle(page);
    await pickVault(page, VAULT, 'E2E 分頁');
    await page.getByRole('navigation', { name: '筆記分頁' }).getByRole('combobox', { name: '每頁筆數' }).selectOption('10');
    await scan('筆記分頁');
    await page.goto('/ui/memory');
    await settle(page);
    await page.getByTestId('filter-panel').getByLabel('起日').fill(localDate(0));
    await scan('記憶層篩選區塊');
    await page.goto('/ui/health');
    await settle(page);
    await page.evaluate(() => document.querySelectorAll('details').forEach((d) => (d.open = true)));
    await scan('系統健康（全部展開）');
    await page.getByRole('button', { name: /VAULTS · DEV/ }).click();
    await scan('側欄收合');
    await page.getByRole('button', { name: /VAULTS · DEV/ }).click();
    // LORE 的記憶層 EmptyState
    await page.getByRole('button', { name: /DEV · 專案開發/ }).click();
    await page.getByRole('menuitemradio', { name: /LORE · 世界觀/ }).click();
    await page.goto('/ui/memory');
    await settle(page);
    await expect(page.getByTestId('memory-dev-only')).toBeVisible();
    await scan('LORE 記憶層');
    expect(found, JSON.stringify(found, null, 2)).toEqual([]);
  });
}
