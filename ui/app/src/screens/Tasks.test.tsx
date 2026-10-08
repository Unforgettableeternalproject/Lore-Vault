/** @vitest-environment happy-dom */
// 任務層畫面：dev-only 守門、列表四態篩選（預設隱藏已完成）、尚未同步／服務錯誤／格式錯誤、
// 過時提示、「無法判定」的錯誤色，以及詳情（封存 note 連結、依賴導航、note 找不到）。
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import type { TaskChange } from '../lib/types';
import { apiError, base64Utf8, json, makeApi, renderWithApp } from '../test/harness';
import { Tasks } from './Tasks';

afterEach(() => {
  cleanup();
  // 篩選會寫回網址查詢字串（happy-dom 的網址跨測試共用），每個測試後重設
  window.history.replaceState(null, '', '/ui/tasks');
});

const VAULT = 'github.com/org/lore-vault';
const FRESH = new Date(Date.now() - 2 * 3_600_000).toISOString();
const OLD = new Date(Date.now() - 30 * 3_600_000).toISOString();

function change(overrides: Partial<TaskChange> = {}): TaskChange {
  return {
    name: 'ready-one',
    status: '可開工',
    reasons: [],
    blocked_by: [],
    depends_on: [],
    requires_authorization: false,
    tasks: { done: 2, total: 5 },
    source: 'T-90',
    why: '**為什麼**要做',
    specs: [],
    note_id: null,
    archived_at: null,
    ...overrides,
  };
}

const MIXED: TaskChange[] = [
  change(),
  change({ name: 'blocked-one', status: '被擋住', reasons: ['D6 未裁決'], blocked_by: [{ id: 'D6', resolved: false }] }),
  change({ name: 'unknown-one', status: '無法判定', reasons: ['D999：DECISIONS.md 沒有此小節'], blocked_by: [{ id: 'D999', resolved: null }] }),
  change({ name: 'auth-one', status: '待授權', requires_authorization: true }),
  change({
    name: 'done-one',
    status: '已完成',
    note_id: 'note-123',
    archived_at: '2026-10-07T00:00:00Z',
    specs: [{ capability: 'sidecar', requirement: '覆寫語意', op: 'ADDED' }],
  }),
  change({ name: 'needs-done', status: '被擋住', depends_on: [{ name: 'done-one', archived: true }, { name: 'ghost', archived: false }] }),
];

function blob(changes: TaskChange[], updated = FRESH) {
  return {
    mime: 'application/json',
    content_base64: base64Utf8(JSON.stringify({ schema: 1, changes })),
    updated,
  };
}

function names() {
  return within(screen.getByTestId('tasks'))
    .getAllByRole('link')
    .map((a) => a.textContent);
}

function badgeOf(el: HTMLElement) {
  return el.closest('.lv-badge')!;
}

describe('任務層：守門', () => {
  it('非 dev space 只顯示說明與「切換到 DEV 檢視」，不打 API', () => {
    const { api, calls } = makeApi({});
    const { switchSpace } = renderWithApp(<Tasks />, api, { space: 'lore' });
    expect(screen.getByTestId('tasks-dev-only')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '切換到 DEV 檢視' }));
    expect(switchSpace).toHaveBeenCalledWith('dev', '/ui/tasks');
    expect(calls).toEqual([]);
  });
});

describe('任務層：列表', () => {
  it('預設「未完成」；狀態 chip 單選（含「全部」）；「無法判定」併入被擋住且標錯誤色；篩選寫回網址', async () => {
    const { api } = makeApi({ '/v1/blob_get': () => json({ items: [{ vault: VAULT, ...blob(MIXED) }] }) });
    renderWithApp(<Tasks />, api);
    await screen.findByTestId('tasks');
    expect(screen.getByRole('button', { name: '未完成' }).getAttribute('aria-pressed')).toBe('true');
    expect(names()).toEqual(['ready-one', 'auth-one', 'blocked-one', 'unknown-one', 'needs-done']);
    expect(screen.queryByRole('checkbox')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '可開工' }));
    expect(names()).toEqual(['ready-one']);
    await waitFor(() => expect(window.location.search).toBe('?status=ready'));

    fireEvent.click(screen.getByRole('button', { name: '被擋住' }));
    expect(names()).toEqual(['blocked-one', 'unknown-one', 'needs-done']);
    const unknownRow = screen.getByRole('link', { name: 'unknown-one' }).closest('li')!;
    const status = within(unknownRow).getByTestId('task-status');
    expect(status.textContent).toBe('無法判定');
    expect(badgeOf(status).classList.contains('lv-badge--error')).toBe(true);
    expect(within(unknownRow).getByText('D999 無法判定')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '待授權' }));
    expect(names()).toEqual(['auth-one']);
    const authRow = screen.getByRole('link', { name: 'auth-one' }).closest('li')!;
    expect(badgeOf(within(authRow).getByText('需授權')).classList.contains('lv-badge--auth')).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: '已完成' }));
    expect(names()).toEqual(['done-one']);
    expect(badgeOf(screen.getByTestId('task-status')).classList.contains('lv-badge--plain')).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: '全部' }));
    expect(names()).toHaveLength(6);

    // 清除篩選回到預設，網址不帶查詢字串
    fireEvent.click(screen.getByTestId('filter-clear'));
    expect(names()).toHaveLength(5);
    await waitFor(() => expect(window.location.search).toBe(''));
  });

  it('網址的 status 決定初始篩選（重新整理、從詳情返回都保留）', async () => {
    window.history.replaceState(null, '', '/ui/tasks?status=done');
    const { api } = makeApi({ '/v1/blob_get': () => json({ items: [{ vault: VAULT, ...blob(MIXED) }] }) });
    renderWithApp(<Tasks />, api);
    await screen.findByTestId('tasks');
    expect(names()).toEqual(['done-one']);
    expect(screen.getByTestId('filter-panel').textContent).toContain('已完成');
  });

  it('全部 vault：依 vault 分組，標頭有 vault 名與同步時間；列上不重複 vault 與同步時間', async () => {
    const other = 'folder/other';
    const { api } = makeApi({
      '/v1/blob_get': () => json({ items: [{ vault: VAULT, ...blob(MIXED) }, { vault: other, ...blob([change({ name: 'x' })]) }] }),
    });
    renderWithApp(<Tasks />, api);
    await screen.findByTestId('tasks');
    const groups = screen.getAllByTestId('task-group');
    expect(groups.map((g) => g.getAttribute('data-vault'))).toEqual([VAULT, other]);
    expect(within(groups[0]!).getByRole('heading', { level: 2 }).textContent).toBe('Lore Vault');
    expect(within(groups[0]!).getByTestId('task-synced').textContent).toContain('同步於');
    expect(within(groups[1]!).getAllByRole('link').map((a) => a.textContent)).toEqual(['x']);
    const row = screen.getByRole('link', { name: 'blocked-one' }).closest('li')!;
    expect(within(row).getByTestId('task-progress').textContent).toBe('2/5');
    expect(within(row).getByText('T-90')).toBeTruthy();
    expect(badgeOf(within(row).getByText('D6 未裁決')).classList.contains('lv-badge--warn')).toBe(true);
    expect(within(row).queryByText('Lore Vault')).toBeNull();
    expect(within(row).queryByText(/同步/)).toBeNull();
  });

  it('單一 vault：帶 vault 參數，不加分組標頭，同步資訊仍顯示', async () => {
    const { api, callsTo } = makeApi({ '/v1/blob_get': () => json(blob(MIXED)) });
    renderWithApp(<Tasks />, api, { vault: VAULT });
    await screen.findByRole('link', { name: 'ready-one' });
    expect(callsTo('/v1/blob_get')[0]!.body).toEqual({ space: 'dev', vault: VAULT, key: 'tasks-snapshot' });
    expect(screen.queryByRole('heading', { level: 2 })).toBeNull();
    expect(screen.getByTestId('task-synced').textContent).toContain('同步於');
  });

  it('同步超過 24 小時，該 vault 區塊標「可能已過時」；新鮮的不標', async () => {
    const other = 'folder/other';
    const { api } = makeApi({
      '/v1/blob_get': () => json({ items: [{ vault: VAULT, ...blob(MIXED, OLD) }, { vault: other, ...blob([change({ name: 'x' })], FRESH) }] }),
    });
    renderWithApp(<Tasks />, api);
    await screen.findByTestId('tasks');
    const groups = screen.getAllByTestId('task-group');
    expect(groups.map((g) => g.getAttribute('data-stale'))).toEqual(['true', 'false']);
    const stale = within(groups[0]!).getByTestId('task-stale');
    expect(stale.textContent).toBe('可能已過時');
    expect(badgeOf(stale).classList.contains('lv-badge--warn')).toBe(true);
    expect(within(groups[1]!).queryByTestId('task-stale')).toBeNull();
  });

  it('全部 vault 回空陣列：顯示「尚未同步」而不是錯誤', async () => {
    const { api } = makeApi({ '/v1/blob_get': () => json({ items: [] }) });
    renderWithApp(<Tasks />, api);
    expect((await screen.findByTestId('tasks-not-synced')).textContent).toContain('尚未同步');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('單一 vault 404 not_found：顯示「尚未同步」', async () => {
    const { api } = makeApi({ '/v1/blob_get': () => apiError(404, 'not_found') });
    renderWithApp(<Tasks />, api, { vault: VAULT });
    expect(await screen.findByTestId('tasks-not-synced')).toBeTruthy();
  });

  it('服務錯誤：顯示錯誤與重試，重試會重打', async () => {
    const { api, callsTo } = makeApi({
      '/v1/blob_get': (_b, n) => (n === 1 ? apiError(500, 'internal_error') : json({ items: [{ vault: VAULT, ...blob(MIXED) }] })),
    });
    renderWithApp(<Tasks />, api);
    const alert = await screen.findByRole('alert');
    fireEvent.click(within(alert).getByRole('button', { name: '重試' }));
    await screen.findByTestId('tasks');
    expect(callsTo('/v1/blob_get')).toHaveLength(2);
  });

  it('快照格式不正確：錯誤橫幅放在該 vault 區塊內，其他 vault 照常列出', async () => {
    const { api } = makeApi({
      '/v1/blob_get': () =>
        json({
          items: [
            { vault: 'folder/bad', mime: 'application/json', content_base64: base64Utf8('{'), updated: FRESH },
            { vault: VAULT, ...blob([change()]) },
          ],
        }),
    });
    renderWithApp(<Tasks />, api);
    const banner = await screen.findByTestId('task-snapshot-invalid');
    expect(banner.textContent).toContain('JSON');
    expect(banner.closest('[data-testid="task-group"]')!.getAttribute('data-vault')).toBe('folder/bad');
    expect(names()).toEqual(['ready-one']);
  });

  it('只有已完成的 change 時，預設篩選給出提示而非空白', async () => {
    const { api } = makeApi({ '/v1/blob_get': () => json({ items: [{ vault: VAULT, ...blob([MIXED[4]!]) }] }) });
    renderWithApp(<Tasks />, api);
    expect((await screen.findByTestId('tasks-filter-empty')).textContent).toContain('已完成');
  });

  it('點 change 名稱導向詳情（vault 與名稱逐段編碼）', async () => {
    const { api } = makeApi({ '/v1/blob_get': () => json({ items: [{ vault: VAULT, ...blob(MIXED) }] }) });
    const { navigate } = renderWithApp(<Tasks />, api);
    fireEvent.click(await screen.findByRole('link', { name: 'ready-one' }));
    expect(navigate).toHaveBeenCalledWith(`/ui/tasks/${encodeURIComponent(VAULT)}/ready-one`);
  });
});

describe('任務層：詳情', () => {
  it('已封存 change：以 /v1/get 取 note 標題並連過去；spec delta 只列標題與操作', async () => {
    const { api, callsTo } = makeApi({
      '/v1/blob_get': () => json(blob(MIXED)),
      '/v1/get': () => json({ items: [{ id: 'note-123', title: '變更 done-one：封存總結' }], missing: [], unavailable: [], truncated: false, budget: 0, used_chars: 0 }),
    });
    const { navigate } = renderWithApp(<Tasks params={[VAULT, 'done-one']} />, api);
    const link = await screen.findByTestId('task-note-link');
    expect(link.textContent).toBe('變更 done-one：封存總結');
    expect(callsTo('/v1/get')[0]!.body).toEqual({ space: 'dev', vault: '*', ids: ['note-123'], fields: 'meta' });
    fireEvent.click(link);
    expect(navigate).toHaveBeenCalledWith('/ui/notes/note-123');
    const specs = screen.getByTestId('task-specs');
    expect(specs.textContent).toContain('ADDED');
    expect(specs.textContent).toContain('覆寫語意');
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('done-one');
  });

  it('note 不存在：明說找不到，不靜默', async () => {
    const { api } = makeApi({
      '/v1/blob_get': () => json(blob(MIXED)),
      '/v1/get': () => json({ items: [], missing: ['note-123'], unavailable: [], truncated: false, budget: 0, used_chars: 0 }),
    });
    renderWithApp(<Tasks params={[VAULT, 'done-one']} />, api);
    expect((await screen.findByTestId('task-note-missing')).textContent).toContain('note-123');
  });

  it('無法判定：原因以錯誤橫幅呈現；進行中沒有 note 連結', async () => {
    const { api, callsTo } = makeApi({ '/v1/blob_get': () => json(blob(MIXED)) });
    renderWithApp(<Tasks params={[VAULT, 'unknown-one']} />, api);
    const reasons = await screen.findByTestId('task-reasons');
    expect(reasons.className).toContain('lv-banner--error');
    expect(reasons.textContent).toContain('D999');
    expect(screen.getByTestId('task-note-none').textContent).toContain('封存後');
    expect(callsTo('/v1/get')).toHaveLength(0);
  });

  it('依賴：同快照內找得到的 change 可點擊導航，找不到的純文字', async () => {
    const { api } = makeApi({ '/v1/blob_get': () => json(blob(MIXED)) });
    const { navigate } = renderWithApp(<Tasks params={[VAULT, 'needs-done']} />, api);
    const deps = await screen.findByTestId('task-deps');
    fireEvent.click(within(deps).getByRole('link', { name: 'done-one' }));
    expect(navigate).toHaveBeenCalledWith(`/ui/tasks/${encodeURIComponent(VAULT)}/done-one`);
    expect(within(deps).queryByRole('link', { name: 'ghost' })).toBeNull();
    expect(within(deps).getByText('ghost')).toBeTruthy();
  });

  it('詳情同步過舊時也標「可能已過時」', async () => {
    const { api } = makeApi({ '/v1/blob_get': () => json(blob(MIXED, OLD)) });
    renderWithApp(<Tasks params={[VAULT, 'ready-one']} />, api);
    expect((await screen.findByTestId('task-stale')).textContent).toBe('可能已過時');
  });

  it('vault 尚未同步、快照裡沒有這個 change 各有空狀態', async () => {
    const { api } = makeApi({ '/v1/blob_get': (_b, n) => (n === 1 ? apiError(404, 'not_found') : json(blob(MIXED))) });
    renderWithApp(<Tasks params={[VAULT, 'nope']} />, api);
    expect(await screen.findByTestId('tasks-not-synced')).toBeTruthy();
    cleanup();
    renderWithApp(<Tasks params={[VAULT, 'nope']} />, api);
    await waitFor(() => expect(screen.getByTestId('task-missing')).toBeTruthy());
  });
});
