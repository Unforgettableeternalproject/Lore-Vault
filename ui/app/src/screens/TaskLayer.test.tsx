/** @vitest-environment happy-dom */
// 任務層啟用／停用（Vault 維護頁）與任務頁的停用呈現：
// 卡片狀態、未啟用→確認→啟用→toast→重讀、已啟用→停用（說明內容保留）、停用中→重新啟用、
// 遠端同步關閉時按鈕停用、非 dev 不顯示、任務頁停用橫幅且不顯示核准按鈕。
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import type { TaskChange, VaultSummary } from '../lib/types';
import { base64Utf8, json, makeApi, renderWithApp, type Handler } from '../test/harness';
import { Maint } from './Maint';
import { Tasks } from './Tasks';

afterEach(() => {
  cleanup();
  window.history.replaceState(null, '', '/ui/maint');
});

const KEY = 'github.com/org/repo';
const OTHER = 'folder/other';

function vault(overrides: Partial<VaultSummary> = {}): VaultSummary {
  return {
    key: KEY,
    display: 'Repo',
    kind: 'repo',
    space: 'dev',
    origin: 'manual',
    aliases: [],
    note_count: 2,
    document_count: 1,
    created: '2026-09-26T00:00:00.000Z',
    last_updated: '2026-09-26T00:00:00.000Z',
    ...overrides,
  };
}

type Row = Record<string, unknown>;

function row(state: 'off' | 'enabled' | 'disabled', key = KEY): Row {
  if (state === 'off') {
    return { vault: key, initialized: false, enabled: false, disabled: null, changes: null, states: null, version: 0 };
  }
  return {
    vault: key,
    initialized: true,
    enabled: state === 'enabled',
    disabled: state === 'disabled' ? { at: '2026-10-09T01:00:00Z', by: '艾斯維爾' } : null,
    changes: 3,
    states: { active: 2, pending_apply: 1, archived: 0 },
    version: 2,
  };
}

const noGraves: Handler = () => json({ items: [], next_cursor: null });

function server(initial: 'off' | 'enabled' | 'disabled', remoteSync = true) {
  const state = { layer: initial };
  const handlers: Record<string, Handler> = {
    '/v1/tombstones': noGraves,
    '/v1/tasks_status': () => json({ space: 'dev', remote_sync: remoteSync, vaults: [row(state.layer)] }),
    '/v1/tasks_enable': () => {
      const reenabled = state.layer === 'disabled';
      state.layer = 'enabled';
      return json({ vault: KEY, space: 'dev', created: !reenabled, reenabled, version: 1 });
    },
    '/v1/tasks_disable': () => {
      state.layer = 'disabled';
      return json({ vault: KEY, space: 'dev', changed: true, disabled: row('disabled').disabled, version: 3 });
    },
  };
  return { state, handlers };
}

async function section() {
  const el = await screen.findByTestId('task-layer');
  return el;
}

describe('Vault 維護頁：任務層', () => {
  it('列表卡片顯示各 dev vault 的任務層狀態（一次 tasks_status 查全部）', async () => {
    const { api, callsTo } = makeApi({
      '/v1/tombstones': noGraves,
      '/v1/tasks_status': () =>
        json({ space: 'dev', remote_sync: true, vaults: [row('enabled'), row('off', OTHER)] }),
    });
    renderWithApp(<Maint vaultKey={null} />, api, {
      vaults: [vault(), vault({ key: OTHER, display: 'Other' })],
    });
    const card = (key: string) => document.querySelector(`[data-maint-vault="${key}"] .lv-vcard__layer`) as HTMLElement;
    await waitFor(() => expect(card(KEY)).toBeTruthy());
    expect(card(KEY).dataset.taskLayer).toBe('enabled');
    expect(card(KEY).textContent).toContain('已啟用 · 3 change');
    expect(card(OTHER).dataset.taskLayer).toBe('off');
    expect(card(OTHER).textContent).toContain('未啟用');
    expect(callsTo('/v1/tasks_status')).toHaveLength(1);
    expect(callsTo('/v1/tasks_status')[0]!.body).toEqual({ space: 'dev' });
  });

  it('非 dev space：不查任務層、不顯示區塊', async () => {
    const lore = vault({ key: 'lore/world', display: '世界觀', space: 'lore' });
    const { api, callsTo } = makeApi({ '/v1/tombstones': noGraves });
    renderWithApp(<Maint vaultKey="lore/world" />, api, { vaults: [lore], space: 'lore' });
    await screen.findByTestId('graves-empty');
    expect(screen.queryByTestId('task-layer')).toBeNull();
    expect(callsTo('/v1/tasks_status')).toHaveLength(0);
  });

  it('未啟用：確認對話框可取消；確認後呼叫 tasks_enable、toast 並重讀成已啟用', async () => {
    const { handlers } = server('off');
    const { api, callsTo } = makeApi(handlers);
    const { toast } = renderWithApp(<Maint vaultKey={KEY} />, api, { vaults: [vault()] });
    const panel = await section();
    expect(within(panel).getByTestId('task-layer-state').textContent).toBe('未啟用');
    expect(callsTo('/v1/tasks_status')[0]!.body).toEqual({ space: 'dev', vault: KEY });

    fireEvent.click(within(panel).getByRole('button', { name: '啟用任務層' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '取消' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(callsTo('/v1/tasks_enable')).toHaveLength(0);

    fireEvent.click(within(panel).getByRole('button', { name: '啟用任務層' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog.textContent).toContain('啟用任務層');
    fireEvent.click(within(dialog).getByRole('button', { name: '確認啟用' }));
    await waitFor(() => expect(within(screen.getByTestId('task-layer')).getByTestId('task-layer-state').textContent).toBe('已啟用'));
    expect(callsTo('/v1/tasks_enable')[0]!.body).toEqual({ space: 'dev', vault: KEY });
    expect(callsTo('/v1/tasks_status')).toHaveLength(2);
    expect(toast).toHaveBeenCalledWith('Repo：已啟用任務層', 'success');
    expect(screen.getByTestId('task-layer-counts').textContent).toBe('3 個 change（進行中 2 · 待落地 1）');
  });

  it('已啟用：可連到任務頁（設定 vault 篩選）；停用前說明內容保留，停用後顯示停用資訊與重新啟用', async () => {
    const { handlers } = server('enabled');
    const { api, callsTo } = makeApi(handlers);
    const { toast, setVault, navigate } = renderWithApp(<Maint vaultKey={KEY} />, api, { vaults: [vault()] });
    const panel = await section();
    expect(within(panel).queryByRole('button', { name: '啟用任務層' })).toBeNull();
    fireEvent.click(within(panel).getByTestId('task-layer-open'));
    expect(setVault).toHaveBeenCalledWith(KEY);
    expect(navigate).toHaveBeenCalledWith('/ui/tasks');

    fireEvent.click(within(panel).getByRole('button', { name: '停用任務層…' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog.textContent).toContain('內容全部保留');
    expect(dialog.textContent).toContain('重新啟用即復原');
    fireEvent.click(within(dialog).getByRole('button', { name: '確認停用' }));
    await waitFor(() => expect(within(screen.getByTestId('task-layer')).getByTestId('task-layer-state').textContent).toBe('已停用'));
    expect(callsTo('/v1/tasks_disable')[0]!.body).toEqual({ space: 'dev', vault: KEY });
    expect(toast).toHaveBeenCalledWith('Repo：已停用任務層，內容保留', 'success');
    expect(screen.getByTestId('task-layer-disabled').textContent).toContain('艾斯維爾');

    fireEvent.click(screen.getByRole('button', { name: '重新啟用任務層' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '確認重新啟用' }));
    await waitFor(() => expect(within(screen.getByTestId('task-layer')).getByTestId('task-layer-state').textContent).toBe('已啟用'));
    expect(toast).toHaveBeenCalledWith('Repo：已重新啟用任務層，內容原樣復原', 'success');
  });

  it('遠端同步關閉：按鈕停用並說明', async () => {
    const { handlers } = server('off', false);
    const { api } = makeApi(handlers);
    renderWithApp(<Maint vaultKey={KEY} />, api, { vaults: [vault()] });
    const panel = await section();
    expect((within(panel).getByRole('button', { name: '啟用任務層' }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(panel).getByTestId('task-layer-sync-off')).toBeTruthy();
  });

  it('啟用失敗：toast 錯誤並重讀，仍是未啟用', async () => {
    const { handlers } = server('off');
    handlers['/v1/tasks_enable'] = () => ({ status: 403, body: { error: { code: 'tasks_remote_sync_disabled', message: '關閉中' } } });
    const { api, callsTo } = makeApi(handlers);
    const { toast } = renderWithApp(<Maint vaultKey={KEY} />, api, { vaults: [vault()] });
    const panel = await section();
    fireEvent.click(within(panel).getByRole('button', { name: '啟用任務層' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '確認啟用' }));
    await waitFor(() => expect(callsTo('/v1/tasks_status')).toHaveLength(2));
    expect(toast.mock.calls[0]![1]).toBe('error');
    expect(within(screen.getByTestId('task-layer')).getByTestId('task-layer-state').textContent).toBe('未啟用');
  });
});

describe('任務頁：停用中的 vault', () => {
  const auth: TaskChange = {
    name: 'auth-one',
    status: '待授權',
    reasons: [],
    blocked_by: [],
    depends_on: [],
    requires_authorization: true,
    tasks: { done: 1, total: 1 },
    source: null,
    why: '理由',
    specs: [],
    note_id: null,
    archived_at: null,
  };
  const snapshot = () =>
    json({
      mime: 'application/json',
      content_base64: base64Utf8(JSON.stringify({ schema: 1, changes: [auth] })),
      updated: new Date().toISOString(),
    });
  const approval = () =>
    json({
      change: { version: 1, state: 'active', requires_authorization: true, content_digest: 'a'.repeat(64) },
      record: null,
      approved: false,
    });

  it('詳情：停用橫幅、不顯示核准區塊', async () => {
    const { api } = makeApi({
      '/v1/blob_get': snapshot,
      '/v1/tasks_authorization_status': approval,
      '/v1/tasks_status': () => json({ space: 'dev', remote_sync: true, vaults: [row('disabled')] }),
    });
    renderWithApp(<Tasks params={[KEY, 'auth-one']} />, api);
    const banner = await screen.findByTestId('task-layer-disabled');
    expect(banner.textContent).toContain('艾斯維爾');
    expect(banner.textContent).toContain('不能核准');
    expect(screen.queryByTestId('task-approval')).toBeNull();
    expect(screen.queryByRole('button', { name: /核准/ })).toBeNull();
  });

  it('詳情：已啟用時照常顯示核准區塊、沒有停用橫幅', async () => {
    const { api } = makeApi({
      '/v1/blob_get': snapshot,
      '/v1/tasks_authorization_status': approval,
      '/v1/tasks_status': () => json({ space: 'dev', remote_sync: true, vaults: [row('enabled')] }),
    });
    renderWithApp(<Tasks params={[KEY, 'auth-one']} />, api);
    await screen.findByTestId('task-approval');
    expect(screen.queryByTestId('task-layer-disabled')).toBeNull();
  });

  it('列表：停用中的 vault 區塊顯示停用橫幅', async () => {
    const { api } = makeApi({
      '/v1/blob_get': () => json({ items: [{ vault: KEY, ...(snapshot().body as object) }] }),
      '/v1/tasks_authorization_status': approval,
      '/v1/tasks_status': () => json({ space: 'dev', remote_sync: true, vaults: [row('disabled')] }),
    });
    renderWithApp(<Tasks />, api);
    const group = await screen.findByTestId('task-group');
    await waitFor(() => expect(within(group).getByTestId('task-layer-disabled')).toBeTruthy());
  });
});
