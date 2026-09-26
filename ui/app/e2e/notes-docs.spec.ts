// 畫面流程（T-79～T-81）：
// 1. 寫筆記 → 檢索命中 → 開啟詳情
// 2. 拖放上傳 md → 抽取 ready → 檢索命中段落 → 跳到該段
// E2E 服務的語意模型刻意不可達：檢索必須顯示降級，而不是失敗。
import { expect, test } from '@playwright/test';

import { createVault, login, watchPage } from './helpers';

const VAULT_KEY = 'folder/e2e-screens';
const VAULT_NAME = 'E2E 畫面';

test.beforeEach(async ({ request }) => {
  await createVault(request, VAULT_KEY, VAULT_NAME);
});

test('寫筆記 → 檢索命中 → 開啟詳情', async ({ page }) => {
  const watch = await watchPage(page);
  await login(page);

  await page.getByRole('link', { name: '筆記' }).click();
  await page.getByRole('button', { name: '+ 新增筆記' }).click();
  await page.getByRole('combobox').selectOption(VAULT_KEY);
  await page.getByLabel('標題', { exact: true }).fill('zephyrquartz 注入預算調整');
  await page.getByLabel('標籤（逗號分隔）').fill('hook, e2e');
  await page.getByLabel('正文（Markdown）').fill('zephyrquartz 首段說明：PreToolUse 預算改成 800 字。\n\n| HOOK | 上限 |\n|---|---|\n| PreToolUse | 800 字 |');
  await page.getByRole('button', { name: '寫入' }).click();

  // 語意模型不可達：查重只做關鍵字，必須明講
  await expect(page.getByRole('heading', { name: /已寫入/ })).toBeVisible();
  await expect(page.getByTestId('dedup-degraded')).toBeVisible();

  await page.getByRole('link', { name: '檢索' }).click();
  await page.getByLabel('檢索查詢').fill('zephyrquartz');
  await page.getByLabel('檢索查詢').press('Enter');
  await expect(page.getByTestId('recall-degraded')).toBeVisible();
  await expect(page.getByTestId('recall-mode')).toHaveText('KEYWORD ONLY');
  const hit = page.locator('.lv-result', { hasText: 'zephyrquartz 注入預算調整' });
  await expect(hit).toBeVisible();
  await expect(hit.locator('.lv-src')).toHaveText('首段'); // enrich worker 關閉：摘要尚未產生
  await expect(page.getByRole('button', { name: '語意檢索離線' })).toBeVisible();

  await hit.click();
  await expect(page.getByRole('heading', { name: 'zephyrquartz 注入預算調整' })).toBeVisible();
  await expect(page.getByTestId('note-summary')).toContainText('摘要尚未產生');
  await expect(page.locator('.lv-md table td', { hasText: '800 字' })).toBeVisible();
  await expect(page).toHaveURL(/\/ui\/notes\/[0-9a-f]+$/);

  await watch.assertClean();
});

test('拖放上傳 md → ready → 檢索命中段落 → 跳到該段', async ({ page }) => {
  const watch = await watchPage(page);
  await login(page);

  await page.getByRole('link', { name: '文件' }).click();
  await page.getByLabel('上傳目標 vault').selectOption(VAULT_KEY);

  // 真實 drop 事件（DataTransfer 內含 File），走頁面上的 onDrop
  const content = '# 流程總覽\n\n開頭段落。\n\n# 注入時機\n\nnebulafjord 段落：PreToolUse 在編輯前注入 concept。\n';
  const dataTransfer = await page.evaluateHandle((text) => {
    const dt = new DataTransfer();
    dt.items.add(new File([text], 'hook-flow.md', { type: 'text/markdown' }));
    return dt;
  }, content);
  await page.getByTestId('dropzone').dispatchEvent('drop', { dataTransfer });

  const results = page.getByTestId('upload-results');
  await expect(results).toContainText('hook-flow.md');
  await expect(results).toContainText('排入抽取');

  // 背景 worker 抽取完成，列表輪詢更新
  const row = page.locator('.lv-doc-row', { hasText: 'hook-flow.md' });
  await expect(row).toHaveAttribute('data-status', 'ready', { timeout: 30_000 });
  await expect(row).toContainText('完成');

  await page.getByRole('link', { name: '檢索' }).click();
  await page.getByLabel('檢索查詢').fill('nebulafjord');
  await page.getByLabel('檢索查詢').press('Enter');
  await expect(page.getByTestId('recall-degraded')).toBeVisible();
  const hit = page.locator('.lv-result[data-kind="chunk"]', { hasText: 'hook-flow.md' });
  await expect(hit).toBeVisible();
  await expect(hit.locator('.lv-src')).toHaveText('摘錄');
  await expect(hit).toContainText('注入時機');

  await hit.click();
  await expect(page.getByRole('heading', { name: 'hook-flow.md' })).toBeVisible();
  await expect(page.getByTestId('from-recall')).toContainText('nebulafjord');
  const active = page.getByTestId('chunk-active');
  await expect(active).toContainText('nebulafjord');
  await expect(active).toContainText('注入時機');
  await expect(active).toBeFocused();

  await watch.assertClean();
});
