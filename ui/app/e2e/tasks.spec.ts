// 任務層畫面：`/v1/blob_get` 以 page.route 攔截回模擬快照（契約見 docs/hidden/design/TASK_LAYER_UI.md §3），
// vault 與封存 note 由真實服務建立，讓 VaultPicker、vault 顯示名稱與 note 連結走真實路徑。
// 涵蓋：尚未同步（全部 vault 空清單、單一 vault 404 not_found）、混合四態列表與篩選、過時提示、
// 封存 change 的 note 連結、服務錯誤、非 dev space 守門、鍵盤可達與深淺色 axe。
// 設 E2E_SCREENSHOT_DIR 時另存截圖供人工審查（不設就不存）。
import { join } from 'node:path';

import { expect, test, type Page, type Route } from '@playwright/test';

import { axeViolations, createVault, login, presetTheme, watchPage, writeNote } from './helpers';

const REPO = { key: 'github.com/unforgettableeternalproject/u.e.p-tasks-layer-e2e-long-repository', display: 'E2E 任務層' };
const OTHER = { key: 'folder/e2e-tasks-other', display: 'E2E 任務層乙' };
const SHOT_DIR = process.env.E2E_SCREENSHOT_DIR;

let noteId = '';

test.beforeAll(async ({ request }) => {
  await createVault(request, REPO.key, REPO.display);
  await createVault(request, OTHER.key, OTHER.display);
  noteId = await writeNote(request, {
    vault: REPO.key,
    title: '變更 add-sidecar：通用側載封存總結',
    body: '封存總結內容。',
    topics: ['change:add-sidecar'],
  });
});

const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString();

function b64(value: unknown): string {
  return Buffer.from(JSON.stringify(value), 'utf-8').toString('base64');
}

function changes() {
  const base = {
    reasons: [] as string[],
    blocked_by: [] as { id: string; resolved: boolean | null }[],
    depends_on: [] as { name: string; archived: boolean }[],
    requires_authorization: false,
    tasks: { done: 2, total: 5 },
    source: 'T-90',
    why: '任務層需要讓 UI 看到**進行中**的 change。',
    specs: [] as { capability: string; requirement: string; op: string }[],
    note_id: null as string | null,
    archived_at: null as string | null,
  };
  return [
    { ...base, name: 'ready-change', status: '可開工' },
    { ...base, name: 'blocked-change', status: '被擋住', reasons: ['D6 未裁決'], blocked_by: [{ id: 'D6', resolved: false }] },
    {
      ...base,
      name: 'unknown-change',
      status: '無法判定',
      reasons: ['D999：DECISIONS.md 沒有此小節'],
      blocked_by: [{ id: 'D999', resolved: null }],
    },
    { ...base, name: 'auth-change', status: '待授權', requires_authorization: true, tasks: { done: 5, total: 5 } },
    {
      ...base,
      name: 'add-sidecar',
      status: '已完成',
      tasks: { done: 7, total: 7 },
      note_id: noteId,
      archived_at: hoursAgo(40),
      specs: [
        { capability: 'sidecar', requirement: '單列覆寫語意', op: 'ADDED' },
        { capability: 'vault', requirement: '刪除連帶清除側載', op: 'MODIFIED' },
      ],
    },
  ];
}

interface MockOptions {
  /** 全部 vault 的回應項目；預設 REPO 一份（新鮮）＋ OTHER 一份（30 小時前） */
  items?: { vault: string; updated: string; changes: unknown[] }[];
  status?: number;
}

/** 攔截 blob_get：省略 vault 回清單；帶 vault 時有資料回單筆、沒有回 404 not_found（契約 §3）。 */
async function mockBlobs(page: Page, options: MockOptions = {}) {
  const items = options.items ?? [
    { vault: REPO.key, updated: hoursAgo(1), changes: changes() },
    { vault: OTHER.key, updated: hoursAgo(30), changes: [{ ...changes()[0]!, name: 'other-ready' }] },
  ];
  await page.route('**/v1/blob_get', async (route: Route) => {
    if (options.status) {
      await route.fulfill({ status: options.status, json: { error: { code: 'internal_error', message: '服務內部錯誤' } } });
      return;
    }
    const body = route.request().postDataJSON() as { space: string; vault?: string; key: string };
    expect(body.key).toBe('tasks-snapshot');
    expect(body.space).toBe('dev');
    const record = (it: (typeof items)[number]) => ({
      mime: 'application/json',
      content_base64: b64({ schema: 1, changes: it.changes }),
      updated: it.updated,
    });
    if (body.vault === undefined) {
      await route.fulfill({ json: { items: items.map((it) => ({ vault: it.vault, ...record(it) })) } });
      return;
    }
    const found = items.find((it) => it.vault === body.vault);
    if (found) await route.fulfill({ json: record(found) });
    else await route.fulfill({ status: 404, json: { error: { code: 'not_found', message: '找不到' } } });
  });
}

async function shot(page: Page, name: string) {
  if (SHOT_DIR) await page.screenshot({ path: join(SHOT_DIR, `${name}.png`), fullPage: true });
}

async function openTasks(page: Page) {
  await page.getByRole('navigation', { name: '主導覽' }).getByRole('link', { name: '任務' }).click();
  await expect(page.getByRole('heading', { name: '任務', level: 1 })).toBeVisible();
}

function list(page: Page) {
  return page.getByRole('list', { name: 'change 列表' });
}

test('尚未同步：全部 vault 空清單與單一 vault 404 都顯示空狀態，不是錯誤白屏', async ({ page }) => {
  await mockBlobs(page, { items: [] });
  await login(page);
  await openTasks(page);
  await expect(page.getByTestId('tasks-not-synced')).toContainText('尚未同步');
  await expect(page.getByRole('main').getByRole('alert')).toHaveCount(0);
  await shot(page, 'tasks-not-synced');

  const picker = page.getByRole('combobox', { name: 'vault 篩選' });
  await picker.click();
  await picker.fill(OTHER.key);
  await expect(page.getByRole('listbox').getByRole('option')).toHaveCount(1);
  await picker.press('Enter');
  await expect(page.getByTestId('tasks-not-synced')).toContainText('這個 vault 還沒有推送');
  await expect(page.getByRole('main').getByRole('alert')).toHaveCount(0);
});

test('混合四態列表：篩選、過時提示、無法判定標示，鍵盤進詳情並開啟封存 note', async ({ page }) => {
  const watch = await watchPage(page);
  await mockBlobs(page);
  await login(page);
  await openTasks(page);

  // 預設隱藏已完成；排序 可開工 → 待授權 → 被擋住
  await expect(list(page).getByRole('link')).toHaveText(['ready-change', 'other-ready', 'auth-change', 'blocked-change', 'unknown-change']);
  // 30 小時前同步的 vault 標「可能已過時」，1 小時前的不標
  const syncs = page.getByTestId('task-syncs');
  await expect(syncs.locator('[data-stale="true"]')).toHaveCount(1);
  await expect(syncs.locator('[data-stale="true"]')).toContainText(OTHER.display);
  await expect(syncs.locator('[data-stale="true"]').getByTestId('task-stale')).toHaveText('可能已過時');
  await expect(syncs.locator('[data-stale="false"]').getByTestId('task-stale')).toHaveCount(0);
  await shot(page, 'tasks-list');

  const chips = page.getByRole('group', { name: 'change 狀態' });
  await chips.getByRole('button', { name: '被擋住 2' }).click();
  await expect(list(page).getByRole('link')).toHaveText(['blocked-change', 'unknown-change']);
  const unknownStatus = list(page).getByRole('listitem').filter({ hasText: 'unknown-change' }).getByTestId('task-status');
  await expect(unknownStatus).toHaveText('無法判定');
  await expect(unknownStatus.locator('xpath=..')).toHaveClass(/lv-badge--error/);
  await chips.getByRole('button', { name: '已完成 1' }).click();
  await expect(list(page).getByRole('link')).toHaveText(['add-sidecar']);
  await shot(page, 'tasks-filter-done');

  // 鍵盤：Tab 到 change 連結、Enter 進詳情
  const link = list(page).getByRole('link', { name: 'add-sidecar' });
  await link.focus();
  await expect(link).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { name: 'add-sidecar', level: 1 })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/ui/tasks/${encodeURIComponent(REPO.key)}/add-sidecar$`));
  await expect(page.getByTestId('task-specs')).toContainText('單列覆寫語意');
  const noteLink = page.getByTestId('task-note-link');
  await expect(noteLink).toHaveText('變更 add-sidecar：通用側載封存總結');
  await shot(page, 'tasks-detail-archived');
  await noteLink.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { name: '變更 add-sidecar：通用側載封存總結', level: 1 })).toBeVisible();

  await watch.assertClean();
});

test('無法判定的 change 詳情：原因以錯誤橫幅呈現', async ({ page }) => {
  await mockBlobs(page);
  await login(page);
  await page.goto(`/ui/tasks/${encodeURIComponent(REPO.key)}/unknown-change`);
  const reasons = page.getByTestId('task-reasons');
  await expect(reasons).toHaveClass(/lv-banner--error/);
  await expect(reasons).toContainText('D999');
  await expect(page.getByTestId('task-note-none')).toBeVisible();
  await shot(page, 'tasks-detail-unknown');
});

test('服務錯誤：顯示錯誤與重試', async ({ page }) => {
  await mockBlobs(page, { status: 500 });
  await login(page);
  await openTasks(page);
  const alert = page.getByRole('main').getByRole('alert');
  await expect(alert).toBeVisible();
  await expect(alert.getByRole('button', { name: '重試' })).toBeVisible();
});

test('非 dev space：只顯示說明，可切回 DEV', async ({ page }) => {
  await mockBlobs(page);
  await login(page);
  await page.getByRole('button', { name: /DEV · 專案開發/ }).click();
  await page.getByRole('menuitemradio', { name: /LORE · 世界觀/ }).click();
  await page.goto('/ui/tasks');
  await expect(page.getByTestId('tasks-dev-only')).toBeVisible();
  await shot(page, 'tasks-dev-only');
  await page.getByRole('button', { name: '切換到 DEV 檢視' }).click();
  await expect(list(page)).toBeVisible();
});

for (const theme of ['dark', 'light'] as const) {
  test(`axe：任務列表與詳情 · ${theme}`, async ({ page }) => {
    await presetTheme(page, theme);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await mockBlobs(page);
    await login(page);
    const found: unknown[] = [];
    const scan = async (ctx: string) => found.push(...(await axeViolations(page, `${theme} · ${ctx}`)));
    await openTasks(page);
    await page.getByRole('checkbox', { name: '含已完成' }).check();
    await expect(list(page).getByRole('link')).toHaveCount(6);
    await scan('任務列表（含已完成）');
    await shot(page, `tasks-list-all-${theme}`);
    await list(page).getByRole('link', { name: 'add-sidecar' }).click();
    await expect(page.getByTestId('task-note-link')).toBeVisible();
    await scan('任務詳情（已封存）');
    await page.goto(`/ui/tasks/${encodeURIComponent(REPO.key)}/blocked-change`);
    await expect(page.getByTestId('task-reasons')).toBeVisible();
    await scan('任務詳情（被擋住）');
    expect(found, JSON.stringify(found, null, 2)).toEqual([]);
  });
}
