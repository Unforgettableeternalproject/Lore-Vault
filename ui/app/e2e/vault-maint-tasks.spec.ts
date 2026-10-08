// Vault 維護頁的任務層啟用／停用（真實服務）：未啟用 → 確認 → 啟用（服務端出現 task-index）→
// 停用（內容保留、bearer 寫入被拒 403 tasks_disabled、任務頁顯示停用橫幅）→ 重新啟用（原樣復原、寫入恢復）。
// bearer 呼叫 UI 限定端點一律 403。設 E2E_SCREENSHOT_DIR 時另存 enable-*.png 截圖供人工審查。
import { join } from 'node:path';

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { E2E_TOKEN } from './constants';
import { axeViolations, createVault, login, watchPage } from './helpers';

const BEARER = { Authorization: `Bearer ${E2E_TOKEN}` };
const V = { key: 'folder/e2e-tasks-enable', display: 'E2E 任務層啟用' };
const SHOT_DIR = process.env.E2E_SCREENSHOT_DIR;

async function shot(page: Page, name: string) {
  if (SHOT_DIR) await page.screenshot({ path: join(SHOT_DIR, `enable-${name}.png`), fullPage: true });
}

async function index(request: APIRequestContext) {
  const resp = await request.post('/v1/blob_get', { headers: BEARER, data: { space: 'dev', vault: V.key, key: 'task-index' } });
  if (resp.status() === 404) return null;
  expect(resp.ok()).toBe(true);
  const body = (await resp.json()) as { content_base64: string; version: number };
  return { version: body.version, doc: JSON.parse(Buffer.from(body.content_base64, 'base64').toString('utf8')) as Record<string, unknown> };
}

function putSnapshot(request: APIRequestContext) {
  const content = Buffer.from(JSON.stringify({ schema: 1, changes: [] })).toString('base64');
  return request.post('/v1/blob_put', {
    headers: BEARER,
    data: { space: 'dev', vault: V.key, key: 'tasks-snapshot', mime: 'application/json', content_base64: content },
  });
}

test.beforeAll(async ({ request }) => {
  await createVault(request, V.key, V.display);
});

test('bearer 不能呼叫任務層啟用／停用／狀態端點', async ({ request }) => {
  for (const path of ['/v1/tasks_enable', '/v1/tasks_disable', '/v1/tasks_status']) {
    const resp = await request.post(path, { headers: BEARER, data: { space: 'dev', vault: V.key } });
    expect(resp.status()).toBe(403);
    expect(((await resp.json()) as { error: { code: string } }).error.code).toBe('ui_session_required');
  }
  expect(await index(request)).toBeNull();
});

test('維護頁：啟用 → 停用（內容保留、寫入被拒）→ 重新啟用', async ({ page, request }) => {
  const watch = await watchPage(page);
  await login(page);
  await page.getByRole('link', { name: '維護', exact: true }).click();
  const card = page.locator(`[data-maint-vault="${V.key}"]`);
  await expect(card.locator('.lv-vcard__layer')).toHaveAttribute('data-task-layer', 'off');
  await card.click();
  await expect(page.getByRole('heading', { name: V.display })).toBeVisible();

  // 未啟用：按鈕＋確認對話框
  const panel = page.getByTestId('task-layer');
  await expect(panel.getByTestId('task-layer-state')).toHaveText('未啟用');
  await shot(page, 'before');
  await panel.getByRole('button', { name: '啟用任務層' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('啟用任務層');
  await shot(page, 'confirm');
  await dialog.getByRole('button', { name: '確認啟用' }).click();
  await expect(page.locator('.uep-toast').filter({ hasText: '已啟用任務層' }).first()).toBeVisible();
  await expect(panel.getByTestId('task-layer-state')).toHaveText('已啟用');
  await expect(panel.getByTestId('task-layer-counts')).toHaveText('0 個 change');
  const created = await index(request);
  expect(created?.doc).toEqual({ schema: 1, changes: {} });
  expect(await axeViolations(page, '維護頁任務層（已啟用）')).toEqual([]);
  await shot(page, 'after');
  // 啟用中可寫入（任務層推送快照的同一條路徑）
  expect((await putSnapshot(request)).ok()).toBe(true);

  // 停用：說明內容保留；之後 bearer 寫入被拒，索引只多停用標記
  await panel.getByRole('button', { name: '停用任務層…' }).click();
  await expect(page.getByRole('dialog')).toContainText('內容全部保留');
  await shot(page, 'disable-confirm');
  await page.getByRole('dialog').getByRole('button', { name: '確認停用' }).click();
  await expect(panel.getByTestId('task-layer-state')).toHaveText('已停用');
  await expect(panel.getByTestId('task-layer-disabled')).toContainText('內容全部保留');
  const disabled = await index(request);
  expect(disabled?.doc.changes).toEqual({});
  expect(disabled?.doc.disabled).toBeTruthy();
  const rejected = await putSnapshot(request);
  expect(rejected.status()).toBe(403);
  expect(((await rejected.json()) as { error: { code: string } }).error.code).toBe('tasks_disabled');
  await shot(page, 'disabled');

  // 任務頁：停用橫幅（連結會設定 vault 篩選）
  await panel.getByTestId('task-layer-open').click();
  await expect(page).toHaveURL(/\/ui\/tasks$/);
  await expect(page.getByTestId('task-layer-disabled')).toContainText('不能核准');
  await shot(page, 'tasks-disabled');

  // 重新啟用：索引回到停用前，寫入恢復
  await page.goto(`/ui/maint/${encodeURIComponent(V.key)}`);
  await page.getByTestId('task-layer').getByRole('button', { name: '重新啟用任務層' }).click();
  await page.getByRole('dialog').getByRole('button', { name: '確認重新啟用' }).click();
  await expect(page.getByTestId('task-layer').getByTestId('task-layer-state')).toHaveText('已啟用');
  expect((await index(request))?.doc).toEqual({ schema: 1, changes: {} });
  expect((await putSnapshot(request)).ok()).toBe(true);
  await shot(page, 'reenabled');

  await watch.assertClean();
});
