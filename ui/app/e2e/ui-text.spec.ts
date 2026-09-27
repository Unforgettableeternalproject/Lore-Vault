// UI 文字品質：逐畫面檢查
// 1. 可見文字的 computed font-size 不低於字級下限（12px）
// 2. 介面文字（含 title、aria-label、placeholder）不出現內部決策／工作編號（A22、T-86、D5…）與版本階段（v12 起）
// 3. 列表表頭與資料列的欄位對齊（同一組欄線）
// 測試資料只用不含這類代號的標題與 vault 名，掃到的就一定是介面本身的字串。
import { expect, test, type Page } from '@playwright/test';

import { createVault, login, watchPage, writeNote } from './helpers';

const VAULT = 'folder/e2e-ui-text';
let noteId = '';

test.beforeAll(async ({ request }) => {
  await createVault(request, VAULT, 'E2E 介面文字');
  noteId = await writeNote(request, {
    vault: VAULT,
    title: 'uitextquartz 介面文字檢查',
    body: 'uitextquartz 正文。',
    topics: ['ui-text'],
  });
});

const MIN_FONT_PX = 12;
const INTERNAL_REF = /\b[ATD]\d{1,3}\b|\bT-\d+\b|階段\s*\d|(?<![A-Za-z])v\d+\s*(?:起|前)/;

async function settle(page: Page) {
  await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
  await expect(page.locator('.lv-loading')).toHaveCount(0, { timeout: 15_000 });
}

function paths() {
  return [
    '/ui/search',
    '/ui/search?q=uitextquartz',
    '/ui/notes',
    `/ui/notes/${noteId}`,
    '/ui/notes/new',
    '/ui/docs',
    '/ui/vaults',
    '/ui/maint',
    `/ui/maint/${encodeURIComponent(VAULT)}`,
    '/ui/health',
    '/ui/memory',
    '/ui/settings',
  ];
}

test('可見文字字級不低於下限', async ({ page }) => {
  test.setTimeout(90_000);
  const watch = await watchPage(page);
  await login(page);
  const problems: unknown[] = [];
  for (const path of paths()) {
    await page.goto(path);
    await settle(page);
    const small = await page.evaluate((min) => {
      const out: string[] = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (!node.textContent?.trim()) continue;
        const el = node.parentElement!;
        // 只給螢幕閱讀器的文字不算
        if (el.closest('.lv-visually-hidden')) continue;
        const rect = el.getBoundingClientRect();
        if (!rect.width || !rect.height || getComputedStyle(el).visibility === 'hidden') continue;
        const size = parseFloat(getComputedStyle(el).fontSize);
        if (size < min && out.length < 10) out.push(`${el.tagName.toLowerCase()}.${Array.from(el.classList).join('.')} ${size}px「${node.textContent.trim().slice(0, 20)}」`);
      }
      return out;
    }, MIN_FONT_PX);
    if (small.length) problems.push({ path, small });
  }
  expect(problems, JSON.stringify(problems, null, 2)).toEqual([]);
  await watch.assertClean();
});

test('介面文字不含內部編號', async ({ page }) => {
  test.setTimeout(90_000);
  const watch = await watchPage(page);
  await login(page);
  const problems: unknown[] = [];
  const pattern = INTERNAL_REF.source;
  for (const path of paths()) {
    await page.goto(path);
    await settle(page);
    // 收合的區塊（details）也要掃：展開全部
    await page.evaluate(() => document.querySelectorAll('details').forEach((d) => (d.open = true)));
    const hits = await page.evaluate((src) => {
      const re = new RegExp(src);
      const out: string[] = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const text = walker.currentNode.textContent ?? '';
        const m = re.exec(text);
        if (m && out.length < 10) out.push(`文字「${text.trim().slice(0, 60)}」`);
      }
      document.querySelectorAll<HTMLElement>('[title], [aria-label], [placeholder]').forEach((el) => {
        for (const attr of ['title', 'aria-label', 'placeholder']) {
          const v = el.getAttribute(attr);
          if (v && re.exec(v) && out.length < 10) out.push(`${attr}「${v.slice(0, 60)}」`);
        }
      });
      return out;
    }, pattern);
    if (hits.length) problems.push({ path, hits });
  }
  // 快捷鍵說明對話框
  await page.goto('/ui/search');
  await settle(page);
  await page.keyboard.press('?');
  const dialogText = await page.getByRole('dialog').innerText();
  if (INTERNAL_REF.test(dialogText)) problems.push({ path: '快捷鍵說明', text: dialogText });
  expect(problems, JSON.stringify(problems, null, 2)).toEqual([]);
  await watch.assertClean();
});

test('列表表頭與資料列欄位對齊', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const watch = await watchPage(page);
  await login(page);
  for (const path of ['/ui/vaults', '/ui/notes', '/ui/docs']) {
    await page.goto(path);
    await settle(page);
    const table = page.getByRole('table').first();
    if ((await table.getByRole('row').count()) < 2) continue;
    const lefts = await table.evaluate((t) =>
      Array.from(t.querySelectorAll(':scope > [role=row]')).slice(0, 3).map((r) => Array.from(r.children).map((c) => Math.round(c.getBoundingClientRect().left))),
    );
    for (const row of lefts.slice(1)) expect(row, path).toEqual(lefts[0]);
  }
  await watch.assertClean();
});
