// 任務層畫面：快照以 bearer 直接呼叫真實的 `/v1/blob_put` 建立（與任務層 CLI 推送同一條路徑），
// vault 與封存 note 也由真實服務建立。只有「服務錯誤」無法自然重現，以 page.route 攔截 blob_get 回 500。
// 涵蓋：尚未同步（全部 vault 空清單、單一 vault 404 not_found）、混合四態列表與篩選、過時提示（以 page.clock
// 把瀏覽器時間往後推）、格式錯誤的快照、封存 change 的 note 連結、服務錯誤、非 dev space 守門、鍵盤可達與深淺色 axe，
// 以及需授權 change 的 UI 核准（MCP-T5）：核准前後服務端授權紀錄不同、edit 後過期、bearer 無法核准或偽造。
// 設 E2E_SCREENSHOT_DIR 時另存截圖供人工審查（不設就不存）。
//
// 側載是服務端共用狀態：第一個測試先斷言本 space 還沒有任何快照，之後的測試才寫入（workers=1、依檔案順序執行）。
import { join } from 'node:path';

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { E2E_DISPLAY, E2E_TOKEN } from './constants';
import { axeViolations, createVault, login, presetTheme, watchPage, writeNote } from './helpers';

const BEARER = { Authorization: `Bearer ${E2E_TOKEN}` };
const REPO = { key: 'github.com/unforgettableeternalproject/u.e.p-tasks-layer-e2e-long-repository', display: 'E2E 任務層' };
const OTHER = { key: 'folder/e2e-tasks-other', display: 'E2E 任務層乙' };
const BAD = { key: 'folder/e2e-tasks-bad', display: 'E2E 任務層壞快照' };
const EMPTY = { key: 'folder/e2e-tasks-empty', display: 'E2E 任務層未同步' };
const SHOT_DIR = process.env.E2E_SCREENSHOT_DIR;

let noteId = '';
let seeded = false;

test.beforeAll(async ({ request }) => {
  for (const v of [REPO, OTHER, BAD, EMPTY]) await createVault(request, v.key, v.display);
  noteId = await writeNote(request, {
    vault: REPO.key,
    title: '變更 add-sidecar：通用側載封存總結',
    body: '封存總結內容。',
    topics: ['change:add-sidecar'],
  });
});

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
      name: 'needs-sidecar',
      status: '被擋住',
      reasons: ['依賴 ghost-change 未封存'],
      depends_on: [
        { name: 'add-sidecar', archived: true },
        { name: 'ghost-change', archived: false },
      ],
    },
    {
      ...base,
      name: 'add-sidecar',
      status: '已完成',
      tasks: { done: 7, total: 7 },
      note_id: noteId,
      archived_at: '2026-10-07T01:45:00Z',
      specs: [
        { capability: 'sidecar', requirement: '單列覆寫語意', op: 'ADDED' },
        { capability: 'vault', requirement: '刪除連帶清除側載', op: 'MODIFIED' },
      ],
    },
  ];
}

async function blobPut(request: APIRequestContext, vault: string, text: string) {
  const resp = await request.post('/v1/blob_put', {
    headers: BEARER,
    data: {
      space: 'dev',
      vault,
      key: 'tasks-snapshot',
      mime: 'application/json',
      content_base64: Buffer.from(text, 'utf-8').toString('base64'),
    },
  });
  expect(resp.status(), await resp.text()).toBe(200);
}

/** 寫入測試快照（只寫一次）：REPO 混合五態、OTHER 一筆、BAD 一份壞 JSON；EMPTY 刻意不寫。 */
async function seed(request: APIRequestContext) {
  if (seeded) return;
  await blobPut(request, REPO.key, JSON.stringify({ schema: 1, changes: changes() }));
  await blobPut(request, OTHER.key, JSON.stringify({ schema: 1, changes: [{ ...changes()[0]!, name: 'other-ready' }] }));
  await blobPut(request, BAD.key, '{"schema": 1, "changes": [');
  seeded = true;
}

async function shot(page: Page, name: string) {
  if (SHOT_DIR) await page.screenshot({ path: join(SHOT_DIR, `${name}.png`), fullPage: true });
}

async function openTasks(page: Page) {
  await page.getByRole('navigation', { name: '主導覽' }).getByRole('link', { name: '任務' }).click();
  await expect(page.getByRole('heading', { name: '任務', level: 1 })).toBeVisible();
}

async function pickVault(page: Page, key: string) {
  const picker = page.getByRole('combobox', { name: 'vault 篩選' });
  await picker.click();
  await picker.fill(key);
  await expect(page.getByRole('listbox').getByRole('option')).toHaveCount(1);
  await picker.press('Enter');
}

function list(page: Page) {
  return page.getByTestId('tasks');
}

test('尚未同步：全部 vault 空清單與單一 vault 404 都顯示空狀態，不是錯誤白屏', async ({ page, request }) => {
  await login(page);
  await openTasks(page);
  await expect(page.getByTestId('tasks-not-synced')).toContainText('這個 space 還沒有任何 repo 推送');
  await expect(page.getByRole('main').getByRole('alert')).toHaveCount(0);
  await shot(page, 'tasks-not-synced');

  // 有其他 vault 的快照後，沒推送過的 vault 仍是「尚未同步」（blob_get 帶 vault → 404 not_found）
  await seed(request);
  await pickVault(page, EMPTY.key);
  await expect(page.getByTestId('tasks-not-synced')).toContainText('這個 vault 還沒有推送');
  await expect(page.getByRole('main').getByRole('alert')).toHaveCount(0);
});

test('混合四態列表：篩選、無法判定標示、格式錯誤快照，鍵盤進詳情並開啟封存 note', async ({ page, request }) => {
  await seed(request);
  const watch = await watchPage(page);
  await login(page);
  await openTasks(page);

  // 預設隱藏已完成；排序 可開工 → 待授權 → 被擋住（同組維持 vault 與快照內順序）
  const rows = list(page).getByRole('link');
  await expect(rows).toHaveCount(6);
  await expect(list(page).getByRole('link', { name: 'add-sidecar' })).toHaveCount(0);
  // 剛同步：不標過時
  await expect(page.getByRole('group', { name: 'change 狀態' }).getByRole('button', { name: '未完成' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('task-stale')).toHaveCount(0);
  // 壞 JSON 的 vault 以錯誤橫幅呈現，其他 vault 照常列出
  const bad = page.getByTestId('task-group').filter({ has: page.getByTestId('task-snapshot-invalid') });
  await expect(bad).toHaveAttribute('data-vault', BAD.key);
  await expect(bad.getByTestId('task-snapshot-invalid')).toContainText('不是有效的 JSON');
  await shot(page, 'tasks-list-default');
  await shot(page, 'tasks-list');

  const chips = page.getByRole('group', { name: 'change 狀態' });
  await chips.getByRole('button', { name: '被擋住', exact: true }).click();
  await expect(rows).toHaveText(['blocked-change', 'unknown-change', 'needs-sidecar']);
  const unknownStatus = list(page).getByRole('listitem').filter({ hasText: 'unknown-change' }).getByTestId('task-status');
  await expect(unknownStatus).toHaveText('無法判定');
  await expect(unknownStatus.locator('xpath=..')).toHaveClass(/lv-badge--error/);
  await chips.getByRole('button', { name: '可開工', exact: true }).click();
  // vault 之間的順序由服務決定，只比對集合
  await expect(rows).toHaveCount(2);
  expect((await rows.allTextContents()).sort()).toEqual(['other-ready', 'ready-change']);
  await chips.getByRole('button', { name: '已完成', exact: true }).click();
  await expect(rows).toHaveText(['add-sidecar']);
  await expect(page).toHaveURL(/[?]status=done$/);
  await shot(page, 'tasks-filter-done');

  // 鍵盤：焦點在 change 連結、Enter 進詳情
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

test('同步超過 24 小時：列表與詳情都標「可能已過時」', async ({ page, request }) => {
  await seed(request);
  // 側載的 updated 由服務寫入當下時間；把瀏覽器時間推到 30 小時後模擬久未同步
  await page.clock.setFixedTime(new Date(Date.now() + 30 * 3_600_000));
  await login(page);
  await openTasks(page);
  await expect(page.locator('[data-testid="task-group"][data-stale="true"]')).toHaveCount(3);
  await expect(page.getByTestId('task-stale').first()).toHaveText('可能已過時');
  await shot(page, 'tasks-list-stale');
  await list(page).getByRole('link', { name: 'ready-change' }).click();
  await expect(page.getByRole('main').getByTestId('task-stale')).toHaveText('可能已過時');
});

test('詳情：無法判定的原因以錯誤橫幅呈現；依賴可導航到同快照的 change', async ({ page, request }) => {
  await seed(request);
  await login(page);
  await page.goto(`/ui/tasks/${encodeURIComponent(REPO.key)}/unknown-change`);
  const reasons = page.getByTestId('task-reasons');
  await expect(reasons).toHaveClass(/lv-banner--error/);
  await expect(reasons).toContainText('D999');
  await expect(page.getByTestId('task-note-none')).toBeVisible();
  await shot(page, 'tasks-detail-unknown');

  await page.goto(`/ui/tasks/${encodeURIComponent(REPO.key)}/needs-sidecar`);
  const deps = page.getByTestId('task-deps');
  await expect(deps.getByRole('link', { name: 'ghost-change' })).toHaveCount(0);
  await deps.getByRole('link', { name: 'add-sidecar' }).click();
  await expect(page.getByRole('heading', { name: 'add-sidecar', level: 1 })).toBeVisible();
});

test('單一 vault：不加分組標頭，同步時間仍顯示，只列該 vault 的 change', async ({ page, request }) => {
  await seed(request);
  await login(page);
  await openTasks(page);
  await pickVault(page, REPO.key);
  await expect(list(page).getByRole('link')).toHaveCount(5);
  await expect(page.getByTestId('task-group')).toHaveCount(1);
  await expect(page.getByTestId('task-group').getByRole('heading', { level: 2 })).toHaveCount(0);
  await expect(page.getByTestId('task-synced')).toContainText('同步於');
  await expect(list(page).getByRole('link', { name: 'other-ready' })).toHaveCount(0);
  await shot(page, 'tasks-single-vault');
});

test('服務錯誤：顯示錯誤與重試（以攔截重現 500）', async ({ page }) => {
  await page.route('**/v1/blob_get', (route) =>
    route.fulfill({ status: 500, json: { error: { code: 'internal_error', message: '服務內部錯誤' } } }),
  );
  await login(page);
  await openTasks(page);
  const alert = page.getByRole('main').getByRole('alert');
  await expect(alert).toBeVisible();
  await expect(alert.getByRole('button', { name: '重試' })).toBeVisible();
});

test('非 dev space：只顯示說明，可切回 DEV', async ({ page, request }) => {
  await seed(request);
  await login(page);
  await page.getByRole('button', { name: /DEV · 專案開發/ }).click();
  await page.getByRole('menuitemradio', { name: /LORE · 世界觀/ }).click();
  await page.goto('/ui/tasks');
  await expect(page.getByTestId('tasks-dev-only')).toBeVisible();
  await shot(page, 'tasks-dev-only');
  await page.getByRole('button', { name: '切換到 DEV 檢視' }).click();
  await expect(list(page)).toBeVisible();
});

// ── 人類核准（MCP-T5）──
// MCP／CLI archive 的授權閘門只認 `task-authorization:<name>` 且其 content_digest 等於服務端目前內容雜湊；
// `/v1/blob_put` 的守衛也在服務端強制（bearer 不能偽造段一結果或取消標記）。這裡以服務端 API 驗證閘門的輸入，
// MCP archive 本身的行為由 pytest（tests/mcp、tests/api/test_tasks_authorize_http.py）驗證。

const AUTH_CHANGE = 'auth-change';

function b64(text: string) {
  return Buffer.from(text, 'utf-8').toString('base64');
}

/** 以 bearer 寫服務端 change 全文（與 MCP propose／edit 同一條路徑）；回新版本 */
function taskChangeDoc(proposal: string, extra: Record<string, unknown> = {}) {
  return {
    schema: 1,
    name: AUTH_CHANGE,
    state: 'active',
    meta: { requires_authorization: true },
    proposal_md: proposal,
    design_md: null,
    tasks_md: '# Tasks\n\n- [x] 1.1 做完\n',
    deltas: {},
    apply: null,
    ...extra,
  };
}

async function postTaskChange(request: APIRequestContext, doc: Record<string, unknown>, expectedVersion?: number) {
  return request.post('/v1/blob_put', {
    headers: BEARER,
    data: {
      space: 'dev',
      vault: REPO.key,
      key: `task-change:${AUTH_CHANGE}`,
      mime: 'application/json',
      content_base64: b64(JSON.stringify(doc)),
      ...(expectedVersion === undefined ? {} : { expected_version: expectedVersion }),
    },
  });
}

/** 以 bearer 寫服務端 change 全文（與 MCP propose／edit 同一條路徑）；回新版本 */
async function putTaskChange(request: APIRequestContext, expectedVersion: number, proposal: string): Promise<number> {
  const resp = await postTaskChange(request, taskChangeDoc(proposal), expectedVersion);
  expect(resp.status(), await resp.text()).toBe(200);
  return ((await resp.json()) as { version: number }).version;
}

async function errorCode(resp: { json(): Promise<unknown> }): Promise<string> {
  return ((await resp.json()) as { error: { code: string } }).error.code;
}

async function authorizationRecord(request: APIRequestContext): Promise<Record<string, unknown> | null> {
  const resp = await request.post('/v1/blob_get', {
    headers: BEARER,
    data: { space: 'dev', vault: REPO.key, key: `task-authorization:${AUTH_CHANGE}` },
  });
  if (resp.status() === 404) return null;
  expect(resp.status(), await resp.text()).toBe(200);
  const blob = (await resp.json()) as { content_base64: string };
  return JSON.parse(Buffer.from(blob.content_base64, 'base64').toString('utf-8')) as Record<string, unknown>;
}

test('核准：需授權 change 經 UI 核准前後授權紀錄不同，內容修改後過期需重新核准；bearer 無法核准或偽造', async ({ page, request }) => {
  await seed(request);
  expect(await putTaskChange(request, 0, '# Proposal\n')).toBe(1);

  // 核准前：沒有授權紀錄（MCP archive 回 authorization_required）
  expect(await authorizationRecord(request)).toBeNull();
  // bearer（所有 agent 共用的 token）不能核准，也不能直接寫授權紀錄
  const viaEndpoint = await request.post('/v1/tasks_authorize', {
    headers: BEARER,
    data: { space: 'dev', vault: REPO.key, change: AUTH_CHANGE },
  });
  expect(viaEndpoint.status()).toBe(403);
  expect(((await viaEndpoint.json()) as { error: { code: string } }).error.code).toBe('ui_session_required');
  const forged = await request.post('/v1/blob_put', {
    headers: BEARER,
    data: {
      space: 'dev',
      vault: REPO.key,
      key: `task-authorization:${AUTH_CHANGE}`,
      mime: 'application/json',
      content_base64: b64(
        JSON.stringify({
          schema: 1,
          vault: REPO.key,
          change: AUTH_CHANGE,
          change_version: 1,
          authorized_by: E2E_DISPLAY,
          authorized_at: '2026-10-08T12:00:00Z',
          principal: { kind: 'ui_session', name: 'forged' },
        }),
      ),
    },
  });
  expect(forged.status()).toBe(403);
  expect(((await forged.json()) as { error: { code: string } }).error.code).toBe('authorization_write_forbidden');
  expect(await authorizationRecord(request)).toBeNull();
  // bearer 直接改 change：不能取消 requires_authorization，也不能自己湊段一結果（pending_apply）跳過核准
  const downgraded = await postTaskChange(request, taskChangeDoc('# Proposal\n', { meta: { requires_authorization: false } }));
  expect(downgraded.status()).toBe(403);
  expect(await errorCode(downgraded)).toBe('authorization_downgrade_forbidden');
  const skipped = await postTaskChange(
    request,
    taskChangeDoc('# Proposal\n', {
      state: 'pending_apply',
      meta: { requires_authorization: true, note_id: '01FAKE' },
      apply: { archived_at: '2026-10-09T00:00:00Z', merged_specs: {}, mirror_versions: {} },
    }),
  );
  expect(skipped.status()).toBe(403);
  expect(await errorCode(skipped)).toBe('authorization_required');

  await login(page);
  await page.goto(`/ui/tasks/${encodeURIComponent(REPO.key)}/${AUTH_CHANGE}`);
  const panel = page.getByTestId('task-approval');
  await expect(panel.getByTestId('task-approval-state')).toHaveText('尚未核准');
  await expect(panel.getByTestId('task-approval-version')).toContainText('v1');
  await shot(page, 'tasks-approve-pending');

  // 取消不寫入
  await panel.getByRole('button', { name: '核准 v1' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('v1');
  await dialog.getByRole('button', { name: '取消' }).click();
  await expect(dialog).toHaveCount(0);
  expect(await authorizationRecord(request)).toBeNull();

  // 鍵盤可達：焦點在核准按鈕、Enter 開對話框
  const approveButton = panel.getByRole('button', { name: '核准 v1' });
  await approveButton.focus();
  await page.keyboard.press('Enter');
  await expect(dialog).toBeVisible();
  await shot(page, 'tasks-approve-dialog');
  await dialog.getByRole('button', { name: '確認核准' }).click();
  await expect(panel.getByTestId('task-approval-state')).toHaveText('已核准');
  await expect(panel.getByTestId('task-approval-record')).toContainText(E2E_DISPLAY);
  await expect(panel.getByRole('button', { name: /核准/ })).toHaveCount(0);
  await shot(page, 'tasks-approve-approved');

  // 核准後：紀錄對準目前版本 v1，授權人與 principal 由服務端依 UI 登入填（MCP archive 的授權檢查通過）
  const approved = await authorizationRecord(request);
  expect(approved).toMatchObject({
    schema: 1,
    vault: REPO.key,
    change: AUTH_CHANGE,
    change_version: 1,
    authorized_by: E2E_DISPLAY,
    principal: { kind: 'ui_session' },
  });
  expect(approved?.content_digest).toMatch(/^[0-9a-f]{64}$/);

  // 同內容重推（版本前進、內容雜湊不變）：核准仍有效
  expect(await putTaskChange(request, 1, '# Proposal\n')).toBe(2);
  await page.reload();
  await expect(panel.getByTestId('task-approval-state')).toHaveText('已核准');

  // 核准後又改內容：內容雜湊不同、紀錄仍是 v1（MCP archive 回 authorization_stale），畫面標過期
  expect(await putTaskChange(request, 2, '# Proposal\n\n核准後又改\n')).toBe(3);
  await page.reload();
  await expect(panel.getByTestId('task-approval-state')).toHaveText('核准已過期');
  await expect(panel.getByTestId('task-approval-record')).toContainText('需要重新核准');
  expect((await authorizationRecord(request))?.change_version).toBe(1);
  await shot(page, 'tasks-approve-stale');
  const violations = await axeViolations(page, '任務詳情（核准已過期）');
  expect(violations, JSON.stringify(violations, null, 2)).toEqual([]);

  await panel.getByRole('button', { name: '重新核准 v3' }).click();
  await dialog.getByRole('button', { name: '確認核准' }).click();
  await expect(panel.getByTestId('task-approval-state')).toHaveText('已核准');
  const renewed = await authorizationRecord(request);
  expect(renewed?.change_version).toBe(3);
  expect(renewed?.content_digest).not.toBe(approved?.content_digest);
});

for (const theme of ['dark', 'light'] as const) {
  test(`axe：任務列表與詳情 · ${theme}`, async ({ page, request }) => {
    await seed(request);
    await presetTheme(page, theme);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await login(page);
    const found: unknown[] = [];
    const scan = async (ctx: string) => found.push(...(await axeViolations(page, `${theme} · ${ctx}`)));
    await openTasks(page);
    await page.getByRole('group', { name: 'change 狀態' }).getByRole('button', { name: '全部', exact: true }).click();
    await expect(list(page).getByRole('link')).toHaveCount(7);
    await scan('任務列表（全部）');
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
