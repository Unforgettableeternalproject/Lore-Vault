// 無障礙（T-87）：深淺兩主題下逐畫面跑 axe（WCAG 2.1 A／AA），含常用對話框與三個 space 的配色；
// 快捷鍵（/、g n、g d、?、輸入框內不觸發）與抽屜外的鍵盤操作。
import { expect, test, type Page } from '@playwright/test';

import { axeViolations, createVault, login, presetTheme, uploadDocument, watchPage, writeNote } from './helpers';

const VAULT = 'folder/e2e-a11y';
const ids: { old: string; newer: string; doc: string } = { old: '', newer: '', doc: '' };

test.beforeAll(async ({ request }) => {
  await createVault(request, VAULT, 'E2E 無障礙');
  ids.old = await writeNote(request, {
    vault: VAULT,
    title: 'a11yquartz 注入預算',
    body: 'a11yquartz 首段：預算 600 字。\n\n```ts\nconst budget = 600;\n```\n\n參考 [[不存在的標題]]',
    topics: ['a11y', 'hook'],
  });
  ids.newer = await writeNote(request, {
    vault: VAULT,
    title: 'a11yquartz 注入預算（更正）',
    body: 'a11yquartz 更正：預算改成 800 字。\n\n| HOOK | 上限 |\n|---|---|\n| PreToolUse | 800 字 |',
    topics: ['a11y'],
    supersedes: ids.old,
  });
  ids.doc = await uploadDocument(request, VAULT, 'a11y-flow.md', '# 流程\n\na11yquartz 文件段落。\n\n# 注入\n\n第二段內容。\n');
});

async function settle(page: Page) {
  await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
  await expect(page.locator('.lv-loading')).toHaveCount(0, { timeout: 15_000 });
}

async function visit(page: Page, path: string) {
  await page.goto(path);
  await settle(page);
}

for (const theme of ['dark', 'light'] as const) {
  test(`axe：${theme === 'dark' ? '深色' : '淺色'}主題逐畫面零違規`, async ({ page }) => {
    test.setTimeout(120_000);
    const watch = await watchPage(page);
    await presetTheme(page, theme);
    // 對話框、toast 的淡入動畫會讓掃描當下的顏色是半透明的；減量動畫下直接是最終顏色
    await page.emulateMedia({ reducedMotion: 'reduce' });
    const found: Awaited<ReturnType<typeof axeViolations>> = [];
    const scan = async (context: string) => found.push(...(await axeViolations(page, `${theme} · ${context}`)));

    await page.goto('/ui/');
    await expect(page.getByRole('heading', { name: '登入' })).toBeVisible();
    await scan('登入頁');
    await login(page);
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);

    await visit(page, '/ui/search?q=a11yquartz');
    await expect(page.getByTestId('recall-degraded')).toBeVisible();
    await scan('檢索（降級結果）');

    await visit(page, '/ui/notes');
    await expect(page.getByTestId('note-superseded').first()).toBeVisible();
    await scan('筆記列表');

    await visit(page, `/ui/notes/${ids.old}`);
    await expect(page.getByTestId('chain-superseded-by')).toContainText('更正');
    await scan('筆記詳情（已被取代）');

    await page.getByRole('button', { name: '刪除…' }).click();
    await expect(page.getByRole('dialog').getByTestId('delete-plan')).toBeVisible();
    await scan('刪除確認對話框');
    await page.getByRole('dialog').getByRole('button', { name: '取消' }).click();

    await visit(page, `/ui/notes/${ids.newer}`);
    await scan('筆記詳情（Markdown 表格）');

    await visit(page, '/ui/notes/new');
    await page.getByRole('combobox').selectOption(VAULT);
    await page.getByLabel('標題', { exact: true }).fill('a11yquartz 注入預算');
    await page.getByLabel('正文（Markdown）').fill('a11yquartz 重複內容 [[沒有這則]]');
    await page.getByRole('button', { name: '寫入' }).click();
    await expect(page.getByTestId('dedup-preview')).toBeVisible();
    // 「寫入」停用後焦點移到查重面板
    await expect(page.getByTestId('dedup-preview')).toBeFocused();
    await scan('新增筆記（查重預覽）');

    await visit(page, '/ui/docs');
    await expect(page.locator('.lv-doc-row', { hasText: 'a11y-flow.md' })).toHaveAttribute('data-status', 'ready', { timeout: 30_000 });
    await scan('文件列表');

    await visit(page, `/ui/docs/${ids.doc}?chunk=1&q=a11yquartz`);
    await expect(page.getByTestId('chunk-active')).toBeVisible();
    await scan('文件檢視');

    for (const path of ['/ui/vaults', '/ui/maint', '/ui/health', '/ui/memory', '/ui/settings']) {
      await visit(page, path);
      await scan(path);
    }

    await page.keyboard.press('?');
    await expect(page.getByTestId('shortcut-help')).toBeVisible();
    await scan('快捷鍵說明');
    await page.keyboard.press('Escape');

    await page.getByRole('button', { name: /DEV · 專案開發/ }).click();
    await expect(page.getByRole('menu')).toBeVisible();
    await scan('space 選單');
    // lore／personal 的 zone 配色
    await page.getByRole('menuitemradio', { name: /LORE · 世界觀/ }).click();
    await visit(page, '/ui/search?q=a11yquartz');
    await scan('LORE 檢索');
    await page.getByRole('button', { name: /LORE · 世界觀/ }).click();
    await page.getByRole('menuitemradio', { name: /PERSONAL · 私人/ }).click();
    await visit(page, '/ui/notes');
    await scan('PERSONAL 筆記列表');

    expect(found, JSON.stringify(found, null, 2)).toEqual([]);
    await watch.assertClean();
  });
}

test('快捷鍵：/ 聚焦檢索、g n／g d 跳畫面、? 說明、輸入框內不觸發', async ({ page }) => {
  const watch = await watchPage(page);
  await login(page);

  await visit(page, '/ui/health');
  await page.keyboard.press('/');
  const search = page.getByLabel('檢索查詢');
  await expect(search).toBeFocused();
  await expect(page).toHaveURL(/\/ui\/search$/);

  // 輸入框中按 / 與 g n 只是打字
  await search.pressSequentially('/gn');
  await expect(search).toHaveValue('/gn');
  await expect(page).toHaveURL(/\/ui\/search$/);

  await search.blur();
  await page.keyboard.press('g');
  await page.keyboard.press('n');
  await expect(page.getByRole('heading', { name: '筆記', level: 1 })).toBeVisible();
  await page.keyboard.press('g');
  await page.keyboard.press('d');
  await expect(page.getByRole('heading', { name: '文件', level: 1 })).toBeVisible();

  await page.keyboard.press('?');
  const dialog = page.getByRole('dialog', { name: '快捷鍵' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId('shortcut-help')).toContainText('前往筆記');
  // 對話框開著時快捷鍵不觸發
  await page.keyboard.press('g');
  await page.keyboard.press('s');
  await expect(page.getByRole('heading', { name: '文件', level: 1 })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();

  // 跳到主內容連結：鍵盤第一個焦點
  await page.keyboard.press('Tab');
  const skip = page.getByRole('link', { name: '跳到主內容' });
  await expect(skip).toBeFocused();
  await expect(skip).toBeVisible();

  await watch.assertClean();
});
