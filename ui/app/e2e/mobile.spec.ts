// 手機版 smoke（T-86，Pixel 7 profile）：抽屜導覽、觸控目標 ≥ 44px、
// 360／375／414／768（與桌面 1280）寬度下逐畫面沒有橫向捲動、檢索與筆記詳情可用；抽屜開著時跑 axe。
import { expect, test, type Page } from '@playwright/test';

import { axeViolations, createVault, login, watchPage, writeNote } from './helpers';

const VAULT = 'folder/e2e-mobile';
// 正式站的 vault key 多是不含空白的長 GitHub 路徑：出現在 vault 列表、維護頁、麵包屑與記憶層錨點，
// 是 360px 橫向溢出的根因（短 key 測不出來）
const LONG_VAULT = 'github.com/unforgettableeternalproject/testseperatememorysystem-e2e-mobile';
let noteId = '';
let longNoteId = '';

test.beforeAll(async ({ request }) => {
  await createVault(request, VAULT, 'E2E 手機版：名稱故意取得很長來測試窄螢幕的截斷與換行');
  await createVault(request, LONG_VAULT, 'TestSeperateMemorySystem');
  longNoteId = await writeNote(request, {
    vault: LONG_VAULT,
    title: 'mobilequartz 長 key vault 的筆記',
    body: '路徑 `C:/Users/Bernie/source/repos/Unforgettableeternalproject/Chatroom/bridge/chatroom_mcp/watch.py`。',
  });
  noteId = await writeNote(request, {
    vault: VAULT,
    title: 'mobilequartz 很長的標題用來測試窄螢幕換行 PreToolUse_hook_budget_configuration_value',
    body: 'mobilequartz 首段。\n\n```\nconst veryLongIdentifierNameThatDoesNotWrap = someFunctionCall(argumentNumberOne, argumentNumberTwo);\n```\n\n| 欄位一 | 欄位二 | 欄位三 | 欄位四 |\n|---|---|---|---|\n| aaaaaaaaaaaaaaaa | bbbbbbbbbbbbbbbb | cccccccccccccccc | dddddddddddddddd |',
    topics: ['mobile', 'very-long-topic-name-for-wrapping'],
  });
});

async function settle(page: Page) {
  await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
  await expect(page.locator('.lv-loading')).toHaveCount(0, { timeout: 15_000 });
}

/** 頁面寬度不超出視窗（沒有橫向捲動）；回傳溢出的元素以便定位 */
async function horizontalOverflow(page: Page) {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const width = doc.clientWidth;
    if (doc.scrollWidth <= width) return null;
    const culprits: string[] = [];
    document.querySelectorAll<HTMLElement>('body *').forEach((el) => {
      const r = el.getBoundingClientRect();
      // 元件本身超出視窗，或內容（例如不換行的長字串）溢出元件
      const spills = el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflowX === 'visible' && r.left + el.scrollWidth > width + 1;
      if ((r.right > width + 1 || spills) && culprits.length < 8) {
        culprits.push(`${el.tagName.toLowerCase()}.${Array.from(el.classList as unknown as ArrayLike<string>).join('.')} right=${Math.round(r.right)}`);
      }
    });
    return { scrollWidth: doc.scrollWidth, width, culprits };
  });
}

test('抽屜導覽、觸控目標與檢索', async ({ page }) => {
  const watch = await watchPage(page);
  await login(page);

  const menu = page.getByRole('button', { name: '開啟導覽選單' });
  await expect(menu).toBeVisible();
  // 抽屜關著：導覽不在無障礙樹、也不能 Tab 到
  await expect(page.getByRole('navigation', { name: '主導覽' })).toBeHidden();
  const menuBox = await menu.boundingBox();
  expect(menuBox!.height).toBeGreaterThanOrEqual(44);
  expect(menuBox!.width).toBeGreaterThanOrEqual(44);

  await menu.click();
  await expect(page.getByRole('button', { name: '關閉導覽選單' })).toHaveAttribute('aria-expanded', 'true');
  const nav = page.getByRole('navigation', { name: '主導覽' });
  await expect(nav).toBeVisible();
  await expect(nav.getByRole('link').first()).toBeFocused();
  for (const link of await nav.getByRole('link').all()) {
    const box = await link.boundingBox();
    expect(box!.height, await link.innerText()).toBeGreaterThanOrEqual(44);
  }
  // 抽屜開著時的無障礙掃描（抽屜動畫在減量模式下立即完成）
  await page.emulateMedia({ reducedMotion: 'reduce' });
  expect(await axeViolations(page, '手機 · 抽屜')).toEqual([]);
  // Esc 關閉，焦點回到選單鈕
  await page.keyboard.press('Escape');
  await expect(nav).toBeHidden();
  await expect(page.getByRole('button', { name: '開啟導覽選單' })).toBeFocused();

  // 經抽屜切到筆記，選了就收起
  await page.getByRole('button', { name: '開啟導覽選單' }).click();
  await nav.getByRole('link', { name: '筆記' }).click();
  await expect(nav).toBeHidden();
  await expect(page.getByRole('heading', { name: '筆記', level: 1 })).toBeVisible();

  // 深淺色、登出在抽屜底部
  await page.getByRole('button', { name: '開啟導覽選單' }).click();
  await expect(page.getByRole('button', { name: '切換為淺色' })).toBeVisible();
  await page.getByRole('button', { name: '切換為淺色' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  // 點遮罩關閉
  await page.mouse.click(page.viewportSize()!.width - 10, page.viewportSize()!.height / 2);
  await expect(nav).toBeHidden();
  await expect(page.getByRole('button', { name: '開啟導覽選單' })).toBeFocused();

  // 檢索：結果卡片可點進詳情
  await page.goto('/ui/search');
  await settle(page);
  const input = page.getByLabel('檢索查詢');
  await input.fill('mobilequartz');
  await input.press('Enter');
  const hit = page.locator('.lv-result', { hasText: 'mobilequartz' }).first();
  await expect(hit).toBeVisible();
  const hitBox = await hit.boundingBox();
  expect(hitBox!.height).toBeGreaterThanOrEqual(44);
  await hit.click();
  await expect(page.getByRole('heading', { level: 1 })).toContainText('mobilequartz');

  await watch.assertClean();
});

for (const width of [360, 375, 414, 768, 1280]) {
  test(`${width}px：逐畫面沒有橫向捲動`, async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width, height: 860 });
    const watch = await watchPage(page);
    await login(page);
    const paths = [
      '/ui/search?q=mobilequartz',
      '/ui/notes',
      `/ui/notes/${noteId}`,
      `/ui/notes/${longNoteId}`,
      '/ui/notes/new',
      '/ui/docs',
      '/ui/vaults',
      '/ui/maint',
      `/ui/maint/${encodeURIComponent(VAULT)}`,
      `/ui/maint/${encodeURIComponent(LONG_VAULT)}`,
      '/ui/health',
      '/ui/memory',
      '/ui/settings',
    ];
    const problems: unknown[] = [];
    for (const path of paths) {
      await page.goto(path);
      await settle(page);
      const overflow = await horizontalOverflow(page);
      if (overflow) problems.push({ path, ...overflow });
    }
    expect(problems, JSON.stringify(problems, null, 2)).toEqual([]);
    await watch.assertClean();
  });
}
