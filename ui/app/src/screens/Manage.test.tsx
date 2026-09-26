/** @vitest-environment happy-dom */
// T-82～T-85：A20 禁用、兩段式（輸入 key、plan_changed 兩種路徑）、墓碑兩種還原結果、doctor fail、非 dev 記憶層。
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import type { DoctorCheck, StatusResult, TombstoneItem, VaultSummary } from '../lib/types';
import { apiError, json, makeApi, renderWithApp, type Handler } from '../test/harness';
import { Health } from './Health';
import { Maint } from './Maint';
import { Memory } from './Memory';
import { Vaults } from './Vaults';

afterEach(cleanup);

function vault(overrides: Partial<VaultSummary> = {}): VaultSummary {
  return {
    key: 'lore/world',
    display: '世界觀',
    kind: 'repo',
    space: 'lore',
    origin: 'manual',
    aliases: [],
    note_count: 2,
    document_count: 1,
    created: '2026-09-26T00:00:00.000Z',
    last_updated: '2026-09-26T00:00:00.000Z',
    ...overrides,
  };
}

const noGraves: Handler = () => json({ items: [], next_cursor: null });

describe('換 space（A20）', () => {
  it('dev vault：兩個目標都停用並說明原因（不寫內部編號），不會送出搬移', async () => {
    const dev = vault({ key: 'github.com/org/repo', display: 'Repo', space: 'dev' });
    const { api, callsTo } = makeApi({ '/v1/tombstones': noGraves });
    renderWithApp(<Maint vaultKey={dev.key} />, api, { vaults: [dev] });
    const lore = screen.getByRole('button', { name: /移到 LORE/ });
    const personal = screen.getByRole('button', { name: /移到 PERSONAL/ });
    expect((lore as HTMLButtonElement).disabled).toBe(true);
    expect((personal as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId('move-a20').textContent).toContain('dev 的 vault 不能移到其他 space');
    expect(screen.getByTestId('move-a20').textContent).not.toMatch(/\bA\d+\b/);
    fireEvent.click(lore);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(callsTo('/v1/vault_move_space')).toHaveLength(0);
  });

  it('lore vault：可移到 personal、dev 停用；確認後顯示新 key 並可切換 space', async () => {
    const v = vault();
    const { api, callsTo } = makeApi({
      '/v1/tombstones': noGraves,
      '/v1/vault_move_space': (body) =>
        body.confirm_token
          ? json({ executed: true, plan: {}, vault: vault({ key: 'personal/world', space: 'personal' }) })
          : json({
              executed: false,
              plan: { key: 'lore/world', new_key: 'personal/world', from: 'lore', to: 'personal', aliases: { 'lore/old': 'personal/old' }, counts: { 'notes.vault': 2 } },
              confirm_token: 'mv',
              expires_at: null,
            }),
    });
    const { switchSpace } = renderWithApp(<Maint vaultKey="lore/world" />, api, { vaults: [v], space: 'lore' });
    expect((screen.getByRole('button', { name: /移到 DEV/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /移到 PERSONAL/ }));
    const dialog = await screen.findByRole('dialog');
    const plan = await within(dialog).findByTestId('delete-plan');
    expect(plan.textContent).toContain('lore/world → personal/world');
    expect(plan.textContent).toContain('personal/old');
    expect(plan.textContent).toContain('notes.vault');
    fireEvent.click(within(dialog).getByRole('button', { name: '確認搬移' }));
    await screen.findByTestId('move-done');
    expect(callsTo('/v1/vault_move_space')[1]!.body).toEqual({ space: 'lore', key: 'lore/world', to_space: 'personal', confirm_token: 'mv' });
    fireEvent.click(screen.getByRole('button', { name: /切換到 PERSONAL/ }));
    expect(switchSpace).toHaveBeenCalledWith('personal', '/ui/maint/personal%2Fworld');
  });
});

describe('刪除 vault（兩段式）', () => {
  const plan = { target: 'vault', vault: 'lore/world', counts: { notes: 2, documents: 1 }, note_ids: ['a', 'b'], requires_force: true };

  it('顯示受影響筆數；輸入完整 key 前確認鈕停用；確認後回維護頁', async () => {
    const { api, callsTo } = makeApi({
      '/v1/tombstones': noGraves,
      '/v1/vault_delete': (body) =>
        body.confirm_token ? json({ executed: true, plan }) : json({ executed: false, plan, confirm_token: 'del', expires_at: null }),
    });
    const { navigate, refreshVaults } = renderWithApp(<Maint vaultKey="lore/world" />, api, { vaults: [vault()], space: 'lore' });
    fireEvent.click(screen.getByRole('button', { name: '刪除這個 vault…' }));
    const dialog = await screen.findByRole('dialog');
    const shown = await within(dialog).findByTestId('delete-plan');
    expect(shown.textContent).toContain('2 則筆記');
    expect(shown.textContent).toContain('文件');
    const confirm = within(dialog).getByRole('button', { name: '確認刪除' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    const input = within(dialog).getByRole('textbox');
    fireEvent.input(input, { target: { value: 'lore/worl' } });
    expect(confirm.disabled).toBe(true);
    fireEvent.input(input, { target: { value: 'lore/world' } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/ui/maint'));
    expect(callsTo('/v1/vault_delete')[1]!.body).toEqual({ space: 'lore', key: 'lore/world', confirm_token: 'del' });
    expect(refreshVaults).toHaveBeenCalled();
  });

  it('409 plan_changed 附新 token：直接顯示新規劃，再確認時送新 token', async () => {
    const changed = { ...plan, counts: { notes: 5, documents: 1 } };
    const { api, callsTo } = makeApi({
      '/v1/tombstones': noGraves,
      '/v1/vault_delete': (body, n) => {
        if (!body.confirm_token) return json({ executed: false, plan, confirm_token: 'old', expires_at: null });
        if (n === 2) return apiError(409, 'plan_changed', { plan: changed, confirm_token: 'new', expires_at: '2026-09-26T01:05:00.000Z' });
        return json({ executed: true, plan: changed });
      },
    });
    const { navigate } = renderWithApp(<Maint vaultKey="lore/world" />, api, { vaults: [vault()], space: 'lore' });
    fireEvent.click(screen.getByRole('button', { name: '刪除這個 vault…' }));
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByTestId('delete-plan');
    fireEvent.input(within(dialog).getByRole('textbox'), { target: { value: 'lore/world' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '確認刪除' }));
    await within(dialog).findByTestId('plan-replanned');
    expect(within(dialog).getByTestId('delete-plan').textContent).toContain('5');
    expect(within(dialog).queryByRole('button', { name: '重新規劃' })).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: '確認刪除' }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/ui/maint'));
    expect(callsTo('/v1/vault_delete').map((c) => c.body.confirm_token)).toEqual([undefined, 'old', 'new']);
  });

  it('409 plan_changed 沒附 token：顯示目前規劃並要求重新規劃', async () => {
    const changed = { ...plan, counts: { notes: 7 } };
    const { api, callsTo } = makeApi({
      '/v1/tombstones': noGraves,
      '/v1/vault_delete': (body) =>
        body.confirm_token ? apiError(409, 'plan_changed', { plan: changed }) : json({ executed: false, plan, confirm_token: 'old', expires_at: null }),
    });
    renderWithApp(<Maint vaultKey="lore/world" />, api, { vaults: [vault()], space: 'lore' });
    fireEvent.click(screen.getByRole('button', { name: '刪除這個 vault…' }));
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByTestId('delete-plan');
    fireEvent.input(within(dialog).getByRole('textbox'), { target: { value: 'lore/world' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '確認刪除' }));
    const replan = await within(dialog).findByRole('button', { name: '重新規劃' });
    expect(within(dialog).getByRole('alert').textContent).toContain('plan_changed');
    expect(within(dialog).getByTestId('delete-plan').textContent).toContain('7');
    fireEvent.click(replan);
    await waitFor(() => expect(callsTo('/v1/vault_delete')).toHaveLength(3));
    expect(callsTo('/v1/vault_delete')[2]!.body).not.toHaveProperty('confirm_token');
  });
});

describe('墓碑還原', () => {
  function grave(overrides: Partial<TombstoneItem>): TombstoneItem {
    return {
      kind: 'note',
      id: 'n1',
      vault: 'lore/world',
      vault_exists: true,
      deleted_at: '2026-09-26T00:00:00.000Z',
      reason: 'ui',
      title: '角色設定',
      source: null,
      restorable: true,
      reimportable: false,
      ...overrides,
    };
  }

  it('有快照：完整還原；舊墓碑：只能移除墓碑、靠重新匯入；vault 已刪：停用', async () => {
    const { api, callsTo } = makeApi({
      '/v1/tombstones': () =>
        json({
          items: [
            grave({}),
            grave({ id: 'n2', title: null, restorable: false, reimportable: true, source: 'pm' }),
            grave({ id: 'n3', title: '孤兒', vault: 'lore/gone', vault_exists: false, restorable: false }),
          ],
          next_cursor: null,
        }),
      '/v1/note_undelete': (body) =>
        body.id === 'n1'
          ? json({ undeleted: {}, restored: true, reimportable: false, note: { id: 'n1', vault: 'lore/world', title: '角色設定', author: null, updated: 'x' } })
          : json({ undeleted: {}, restored: false, reimportable: true, note: null }),
    });
    const { refreshVaults } = renderWithApp(<Maint vaultKey={null} />, api, { vaults: [vault()], space: 'lore' });
    const list = await screen.findByTestId('tombstones');
    expect(callsTo('/v1/tombstones')[0]!.body).toMatchObject({ space: 'lore', vault: '*' });

    expect((within(list).getByRole('button', { name: '還原 孤兒' }) as HTMLButtonElement).disabled).toBe(true);
    expect(list.textContent).toContain('重建同 key 的 vault 後才能還原');

    fireEvent.click(within(list).getByRole('button', { name: '還原 角色設定' }));
    const results = await screen.findByTestId('restore-results');
    await waitFor(() => expect(results.textContent).toContain('已完整還原「角色設定」'));
    expect(results.querySelector('[data-tone="ok"]')).toBeTruthy();

    const old = within(screen.getByTestId('tombstones')).getByRole('button', { name: '移除墓碑 n2' });
    fireEvent.click(old);
    await waitFor(() => expect(screen.getByTestId('restore-results').textContent).toContain('重跑匯入'));
    expect(screen.getByTestId('restore-results').querySelector('[data-tone="warn"]')).toBeTruthy();
    expect(callsTo('/v1/note_undelete').map((c) => c.body)).toEqual([
      { space: 'lore', id: 'n1' },
      { space: 'lore', id: 'n2' },
    ]);
    expect(refreshVaults).toHaveBeenCalled();
  });

  it('文件還原失敗（409 not_restorable）顯示原因', async () => {
    const { api } = makeApi({
      '/v1/tombstones': () =>
        json({ items: [grave({ kind: 'document', id: 'doc:1', title: undefined, filename: 'a.pdf', restorable: false })], next_cursor: null }),
      '/v1/document_undelete': () => apiError(409, 'not_restorable', { reason: 'blob_missing' }),
    });
    renderWithApp(<Maint vaultKey={null} />, api, { vaults: [vault()], space: 'lore' });
    fireEvent.click(await screen.findByRole('button', { name: '還原 a.pdf' }));
    expect((await screen.findByRole('alert')).textContent).toContain('原始檔已不在');
  });
});

describe('別名', () => {
  it('新增撞到既有 vault（409 vault_exists）明確說明；成功移除後更新', async () => {
    const { api, callsTo } = makeApi({
      '/v1/tombstones': noGraves,
      '/v1/vault_alias_add': () => apiError(409, 'vault_exists', { existing: { key: 'lore/other' } }),
      '/v1/vault_alias_remove': () => json(vault({ aliases: [] })),
    });
    renderWithApp(<Maint vaultKey="lore/world" />, api, { vaults: [vault({ aliases: ['lore/old'] })], space: 'lore' });
    fireEvent.input(screen.getByRole('textbox', { name: '新別名' }), { target: { value: 'lore/new' } });
    fireEvent.click(screen.getByRole('button', { name: '導向此 vault' }));
    expect((await screen.findByRole('alert')).textContent).toContain('lore/other');
    expect(callsTo('/v1/vault_alias_add')[0]!.body).toEqual({ space: 'lore', vault: 'lore/world', alias: 'lore/new' });

    fireEvent.click(screen.getByRole('button', { name: '移除別名 lore/old' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '移除別名 lore/old' })).toBeNull());
  });

  it('非 dev 別名缺前綴時前端先擋下，不送出', async () => {
    const { api, callsTo } = makeApi({ '/v1/tombstones': noGraves });
    renderWithApp(<Maint vaultKey="lore/world" />, api, { vaults: [vault()], space: 'lore' });
    fireEvent.input(screen.getByRole('textbox', { name: '新別名' }), { target: { value: 'github.com/x/y' } });
    fireEvent.click(screen.getByRole('button', { name: '導向此 vault' }));
    expect((await screen.findByRole('alert')).textContent).toContain('lore/');
    expect(callsTo('/v1/vault_alias_add')).toHaveLength(0);
  });
});

describe('Vault 列表', () => {
  it('lore 建立 vault 帶 space 與前綴；改名呼叫 vault_update', async () => {
    const { api, callsTo } = makeApi({
      '/v1/vaults': (body) => json({ ...vault(), key: body.key as string, display: body.display as string }, 201),
      '/v1/vault_update': (body) => json(vault({ display: body.display as string })),
    });
    const { refreshVaults, toast } = renderWithApp(<Vaults />, api, { vaults: [vault()], space: 'lore' });
    expect(screen.getByText('手動')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '+ 建立 vault' }));
    const form = screen.getByRole('form', { name: '建立 vault' });
    const [key, display] = within(form).getAllByRole('textbox') as HTMLInputElement[];
    expect(key!.value).toBe('lore/');
    fireEvent.input(key!, { target: { value: 'lore/chronology' } });
    fireEvent.input(display!, { target: { value: '年表' } });
    fireEvent.click(within(form).getByRole('button', { name: '建立於 LORE' }));
    await waitFor(() => expect(callsTo('/v1/vaults')).toHaveLength(1));
    expect(callsTo('/v1/vaults')[0]!.body).toEqual({ space: 'lore', key: 'lore/chronology', display: '年表' });
    await waitFor(() => expect(refreshVaults).toHaveBeenCalled());
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('年表'), 'success');

    fireEvent.click(screen.getByRole('button', { name: '編輯 世界觀 的顯示名稱' }));
    fireEvent.input(screen.getByRole('textbox', { name: '新的顯示名稱' }), { target: { value: '邊際世界' } });
    fireEvent.click(screen.getByRole('button', { name: '儲存' }));
    await waitFor(() => expect(callsTo('/v1/vault_update')).toHaveLength(1));
    expect(callsTo('/v1/vault_update')[0]!.body).toEqual({ space: 'lore', vault: 'lore/world', display: '邊際世界' });
  });
});

describe('系統健康', () => {
  function check(overrides: Partial<DoctorCheck>): DoctorCheck {
    return { name: 'x.y', category: 'storage', description: '說明', status: 'pass', summary: '', details: [], counts: {}, ...overrides };
  }

  function status(checks: DoctorCheck[]): StatusResult {
    const count = (s: string) => checks.filter((c) => c.status === s).length;
    return {
      ok: count('fail') === 0,
      checked_at: '2026-09-26T06:00:00.000Z',
      schema: { version: 12, expected: 12 },
      space: 'dev',
      embedding: { warmup: { status: 'failed', started_at: null, finished_at: null, elapsed_ms: null, error: 'connect refused' } },
      enrich: {
        worker: { enabled: true, running: true },
        backlog: { status: 'warn', summary: '3 則待補', counts: { summary_pending: 3, embedding_pending: 0, oldest_age_seconds: 10 } },
      },
      documents: { enabled: false, worker: { enabled: false, running: false }, backlog: { status: 'pass', summary: '', counts: {} } },
      doctor: {
        ok: count('fail') === 0,
        exit_code: count('fail') ? 1 : 0,
        summary: { total: checks.length, pass: count('pass'), warn: count('warn'), fail: count('fail'), skipped: count('skipped') },
        checks,
      },
    };
  }

  it('客戶端檢查（snapshot／spool／concept_snapshot）歸成預設收合的一組，不算進 SKIP、不顯示缺少設定', async () => {
    const { api } = makeApi({
      '/v1/status': () =>
        json(
          status([
            check({ name: 'storage.ok', category: 'storage' }),
            check({ name: 'snapshot.age', category: 'snapshot', status: 'skipped', summary: '缺少設定：snapshot_dir', description: '快照新鮮度' }),
            check({ name: 'spool.pending', category: 'spool', status: 'skipped', summary: '缺少設定：spool_dir', description: 'spool 待推送' }),
            check({ name: 'concept_snapshot.age', category: 'concept_snapshot', status: 'skipped', summary: '缺少設定：concept_snapshot' }),
          ]),
        ),
      '/v1/episode_summary': () => json({ space: 'dev', vault: '*', total: 0, last_recorded: null, by_machine: [], by_vault: [] }),
    });
    renderWithApp(<Health />, api);
    const group = (await screen.findByTestId('client-checks')) as HTMLDetailsElement;
    expect(group.open).toBe(false);
    expect(group.textContent).toContain('客戶端檢查 · 3 項');
    expect(group.textContent).toContain('python -m lore_vault.doctor');
    expect(group.textContent).not.toContain('缺少設定');
    expect(screen.getByTestId('check-snapshot.age').textContent).toContain('AGENT');
    expect(screen.getByTestId('count-skipped').textContent).toContain('0');
    // 服務端分組裡沒有客戶端分類
    const categories = Array.from(document.querySelectorAll('[data-category]')).map((g) => g.getAttribute('data-category'));
    expect(categories).toEqual(['storage']);
  });

  it('fail 醒目、排最前且展開明細；計數與頂列徽章同步；收料過久標紅', async () => {
    const recent = new Date(Date.now() - 3_600_000).toISOString();
    const { api, callsTo } = makeApi({
      '/v1/status': () =>
        json(
          status([
            check({ name: 'storage.ok', category: 'storage' }),
            check({ name: 'backup.recent', category: 'backup', status: 'skipped', summary: '缺少設定：backup_dir' }),
            check({ name: 'enrich.stale', category: 'enrich', status: 'warn', summary: '摘要過期', details: ['n1'] }),
            check({ name: 'notes.fts', category: 'notes', status: 'fail', summary: 'FTS 筆數不符', details: ['缺 n9'], counts: { notes: 10, fts_rows: 9 } }),
          ]),
        ),
      '/v1/episode_summary': () =>
        json({
          space: 'dev',
          vault: '*',
          total: 30,
          last_recorded: recent,
          by_machine: [
            { machine: 'desk', episodes: 20, last_recorded: recent, last_started: recent },
            { machine: 'laptop', episodes: 10, last_recorded: '2026-01-01T00:00:00.000Z', last_started: null },
          ],
          by_vault: [],
        }),
    });
    const { reportHealth } = renderWithApp(<Health />, api);
    await screen.findByTestId('health-unhealthy');
    expect(screen.getByTestId('count-fail').textContent).toContain('1');
    expect(screen.getByTestId('count-fail').className).toContain('is-nonzero');
    const groups = document.querySelectorAll('[data-category]');
    expect(groups[0]!.getAttribute('data-category')).toBe('notes');
    expect(groups[1]!.getAttribute('data-category')).toBe('enrich');
    const fail = screen.getByTestId('check-notes.fts') as HTMLDetailsElement;
    expect(fail.open).toBe(true);
    expect(fail.textContent).toContain('缺 n9');
    expect(fail.textContent).toContain('fts_rows');
    expect((screen.getByTestId('check-enrich.stale') as HTMLDetailsElement).open).toBe(false);
    expect(screen.getByTestId('health-backup').textContent).toContain('backup_dir');
    expect(screen.getByTestId('health-warmup').textContent).toContain('暖機失敗');
    expect(reportHealth).toHaveBeenCalledWith(expect.objectContaining({ ok: false, fail: 1, warn: 1 }));

    const machines = await screen.findByTestId('machines');
    const rows = machines.querySelectorAll('li');
    expect(rows[0]!.getAttribute('data-stale')).toBe('false');
    expect(rows[1]!.getAttribute('data-stale')).toBe('true');
    expect(callsTo('/v1/episode_summary')[0]!.body).toEqual({ space: 'dev', vault: '*' });

    fireEvent.click(screen.getByRole('button', { name: '重新整理' }));
    await waitFor(() => expect(callsTo('/v1/status')).toHaveLength(2));
  });

  it('status 取得失敗：顯示錯誤並讓徽章標為無法取得；收料概況仍顯示', async () => {
    const { api } = makeApi({
      '/v1/status': () => apiError(500, 'storage_error'),
      '/v1/episode_summary': () => json({ space: 'dev', vault: '*', total: 0, last_recorded: null, by_machine: [], by_vault: [] }),
    });
    const { reportHealth } = renderWithApp(<Health />, api);
    expect((await screen.findByRole('alert')).textContent).toContain('storage_error');
    await waitFor(() => expect(reportHealth).toHaveBeenCalledWith(expect.objectContaining({ error: expect.stringContaining('storage_error') })));
    expect(await screen.findByText('還沒有收到任何 episode。')).toBeTruthy();
  });
});

describe('記憶層', () => {
  it('非 dev space：顯示只屬於 dev 的說明，不查 concept', () => {
    const { api, calls } = makeApi({});
    renderWithApp(<Memory />, api, { space: 'personal', vaults: [] });
    expect(screen.getByTestId('memory-dev-only').textContent).toContain('只屬於 dev');
    expect(calls).toHaveLength(0);
  });

  it('dev：依 kind／scope 篩選查 concept_query，只顯示 statement 與 metadata', async () => {
    const { api, callsTo } = makeApi({
      '/v1/concept_query': () =>
        json({
          items: [
            {
              id: 'c1',
              vault: 'github.com/org/lore-vault',
              kind: 'belief-correction',
              scope: 'lore-vault',
              scope_state: 'repo',
              statement: 'hook 路徑只能用標準庫',
              anchors: [{ file: 'src/hooks/x.py', symbol: 'run' }],
              surprisal: 0.42,
              usability_verdict: null,
              updated: '2026-09-26T00:00:00.000Z',
            },
          ],
          next_cursor: 'c2',
        }),
      '/v1/episode_summary': () => json({ space: 'dev', vault: '*', total: 5, last_recorded: null, by_machine: [], by_vault: [] }),
    });
    renderWithApp(<Memory />, api);
    const list = await screen.findByTestId('concepts');
    expect(list.textContent).toContain('hook 路徑只能用標準庫');
    expect(list.textContent).toContain('src/hooks/x.py#run');
    expect(list.textContent).toContain('0.42');
    fireEvent.change(screen.getByRole('combobox', { name: 'concept 類型' }), { target: { value: 'user-stance' } });
    await waitFor(() => expect(callsTo('/v1/concept_query').at(-1)!.body).toMatchObject({ kind: 'user-stance' }));
    fireEvent.click(screen.getByRole('button', { name: '跨專案' }));
    await waitFor(() => expect(callsTo('/v1/concept_query').at(-1)!.body).toMatchObject({ scope_state: 'global', kind: 'user-stance' }));
    fireEvent.click(screen.getByRole('button', { name: '下一頁 →' }));
    await waitFor(() => expect(callsTo('/v1/concept_query').at(-1)!.body).toMatchObject({ cursor: 'c2' }));
    expect(await screen.findByTestId('episode-total')).toBeTruthy();
  });
});
