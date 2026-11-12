/** @vitest-environment happy-dom */
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import type { NoteFull, NoteListItem, ProposalDetail, ProposalRow, QueueCounts, QueueItem } from '../lib/types';
import { apiError, json, makeApi, renderWithApp, type Handler } from '../test/harness';
import { NoteDetail } from './NoteDetail';
import { Notes } from './Notes';
import { Review } from './Review';

afterEach(() => {
  cleanup();
  window.history.replaceState(null, '', '/ui/notes');
});

const VAULT = 'github.com/org/lore-vault';

function listItem(overrides: Partial<NoteListItem> = {}): NoteListItem {
  return {
    id: 'n1',
    kind: 'note',
    vault: VAULT,
    title: 'UEP 寫的筆記',
    topics: [],
    updated: '2026-09-26T01:00:00.000Z',
    author: 'uep',
    review_state: 'pending',
    review: 'unverified',
    ...overrides,
  };
}

function page(items: NoteListItem[]) {
  return json({ items, next_cursor: null, has_more: false, total: items.length, unsupported_kinds: [], budget: 8000, used_chars: 0, truncated: false, summaries_omitted: 0 });
}

function note(overrides: Partial<NoteFull> = {}): NoteFull {
  return {
    id: 'n1',
    kind: 'note',
    vault: VAULT,
    title: 'UEP 寫的筆記',
    summary: '摘要',
    summary_source: 'summary',
    body: '內文',
    body_chars: 2,
    truncated: false,
    topics: [],
    links: [],
    supersedes: null,
    created: '2026-09-20T00:00:00.000Z',
    updated: '2026-09-26T01:00:00.000Z',
    author: 'uep',
    review_state: 'pending',
    review: 'unverified',
    ...overrides,
  };
}

function getReply(n: NoteFull) {
  return json({ items: [n], missing: [], unavailable: [], truncated: false, budget: 200000, used_chars: 2 });
}

function qItem(overrides: Partial<QueueItem> = {}): QueueItem {
  return {
    item_id: 'prop_1',
    source: 'proposal',
    kind: 'note_delete',
    vault: VAULT,
    target: { type: 'note', id: 'n1', title: '要刪的筆記' },
    summary: '刪除原因：重複',
    proposer: { principal: 'agent-a', author: 'Claude' },
    created_at: '2026-11-10T01:00:00.000Z',
    expires_at: '2026-11-24T01:00:00.000Z',
    state: 'pending',
    rev: 1,
    stale: false,
    deep_link: null,
    ...overrides,
  };
}

function queue(items: QueueItem[], next: string | null = null) {
  return json({ items, next_cursor: next, has_more: next !== null });
}

function counts(c: QueueCounts['counts']) {
  return json({ counts: c });
}

function proposalRow(overrides: Partial<ProposalRow> = {}): ProposalRow {
  return {
    id: 'prop_1',
    kind: 'note_delete',
    state: 'pending',
    rev: 3,
    payload: { reason: '重複', plan_summary: { title: '要刪的筆記', link_count: 2, backlink_count: 1 } },
    base_snapshot: null,
    base_updated: null,
    created_at: '2026-11-10T01:00:00.000Z',
    expires_at: '2026-11-24T01:00:00.000Z',
    ...overrides,
  };
}

function detail(overrides: Partial<ProposalDetail> = {}): ProposalDetail {
  return {
    proposal: proposalRow(),
    target: { type: 'note', id: 'n1', title: '要刪的筆記' },
    current: null,
    current_updated: null,
    ...overrides,
  };
}

const emptyHandlers: Record<string, Handler> = {
  '/v1/review/queue': () => queue([]),
  '/v1/review/counts': () => counts({}),
};

describe('審核佇列', () => {
  it('合併顯示各種類，標出種類；UEP 導到筆記詳情、任務授權導到任務頁', async () => {
    const { api, callsTo } = makeApi({
      '/v1/review/queue': () =>
        queue([
          qItem(),
          qItem({ item_id: 'n9', source: 'uep', kind: 'uep_review', target: { type: 'note', id: 'n9', title: 'UEP 筆記' }, proposer: { principal: 'uep', author: 'uep' } }),
          qItem({ item_id: 'add-x', source: 'task_auth', kind: 'task_auth', target: { type: 'task_change', id: 'add-x', title: 'add-x' }, deep_link: '/ui/tasks/v/add-x' }),
        ]),
      '/v1/review/counts': () => counts({ note_delete: 1, uep_review: 1, task_auth: 1 }),
    });
    const { navigate } = renderWithApp(<Review />, api);
    await screen.findByText('要刪的筆記');
    expect(callsTo('/v1/review/queue')[0]!.body).toMatchObject({ space: 'dev', limit: 30 });
    expect(callsTo('/v1/review/queue')[0]!.body.kinds).toBeUndefined();
    expect(screen.getAllByTestId('review-kind').map((e) => e.textContent)).toEqual(['刪除筆記', 'UEP 待審', '任務授權']);
    expect(screen.getByRole('button', { name: '全部 3' })).toBeTruthy();
    const rows = screen.getAllByTestId('review-row');
    fireEvent.click(rows[1]!);
    expect(navigate).toHaveBeenCalledWith('/ui/notes/n9');
    fireEvent.click(rows[2]!);
    expect(navigate).toHaveBeenCalledWith('/ui/tasks/v/add-x');
  });

  it('kind 篩選以 kinds 送出，並同步到網址', async () => {
    window.history.replaceState(null, '', '/ui/review');
    const { api, callsTo } = makeApi(emptyHandlers);
    renderWithApp(<Review />, api);
    await screen.findByTestId('review-empty');
    fireEvent.click(screen.getByRole('button', { name: /^修改筆記/ }));
    await waitFor(() => expect(callsTo('/v1/review/queue').at(-1)!.body.kinds).toEqual(['note_update']));
    expect(window.location.search).toContain('kind=note_update');
  });

  it('分頁：載入更多帶游標並接在後面', async () => {
    const { api, callsTo } = makeApi({
      '/v1/review/queue': (body) =>
        body.cursor ? queue([qItem({ item_id: 'prop_2', target: { type: 'note', id: 'n2', title: '第二頁' } })]) : queue([qItem()], 'cur-1'),
      '/v1/review/counts': () => counts({ note_delete: 2 }),
    });
    renderWithApp(<Review />, api);
    fireEvent.click(await screen.findByTestId('review-more'));
    await screen.findByText('第二頁');
    expect(callsTo('/v1/review/queue')[1]!.body.cursor).toBe('cur-1');
    expect(screen.getAllByTestId('review-row')).toHaveLength(2);
    expect(screen.queryByTestId('review-more')).toBeNull();
  });

  it('標頭用共用的 lv-screen__head，VAULT 選擇器在標頭之外的 lv-filters 列（與其他頁同一套間距）', async () => {
    const { api } = makeApi(emptyHandlers);
    renderWithApp(<Review />, api);
    await screen.findByTestId('review-empty');
    const h1 = screen.getByRole('heading', { name: '審核佇列' });
    const head = h1.closest('.lv-screen__head')!;
    expect(head).toBeTruthy();
    expect(head.querySelector('.lv-eyebrow')).toBeTruthy();
    const picker = screen.getByTestId('vault-picker');
    expect(head.contains(picker)).toBe(false);
    expect(picker.closest('.lv-filters')).toBeTruthy();
    expect(head.nextElementSibling?.classList.contains('lv-filters')).toBe(true);
  });

  it('沒有待審項目時顯示空狀態', async () => {
    const { api } = makeApi(emptyHandlers);
    renderWithApp(<Review />, api);
    expect(await screen.findByTestId('review-empty')).toBeTruthy();
  });

  it('personal space：顯示空狀態，不查佇列、沒有 vault 選擇器，可切到 dev 的審核頁', () => {
    const { api, calls } = makeApi(emptyHandlers);
    const { switchSpace } = renderWithApp(<Review />, api, { space: 'personal' });
    const empty = screen.getByTestId('review-personal-none');
    expect(empty.textContent).toContain('不經審核');
    expect(screen.queryByTestId('vault-picker')).toBeNull();
    expect(calls).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '切換到 DEV 檢視' }));
    expect(switchSpace).toHaveBeenCalledWith('dev', '/ui/review');
  });

  it.each(['dev', 'lore'] as const)('%s space：照常查詢並顯示 vault 選擇器', async (space) => {
    const { api, callsTo } = makeApi(emptyHandlers);
    renderWithApp(<Review />, api, { space });
    await screen.findByTestId('review-empty');
    expect(screen.queryByTestId('review-personal-none')).toBeNull();
    expect(screen.getByTestId('vault-picker')).toBeTruthy();
    expect(callsTo('/v1/review/queue')[0]!.body.space).toBe(space);
    expect(callsTo('/v1/review/counts')).toHaveLength(1);
  });
});

describe('審核頁處理提案', () => {
  it('刪除提案：面板顯示規劃，核准帶 id 與 expected_rev，成功後重新載入', async () => {
    let done = false;
    const { api, callsTo } = makeApi({
      '/v1/review/queue': () => queue(done ? [] : [qItem()]),
      '/v1/review/counts': () => counts({}),
      '/v1/review/proposal_get': () => json(detail()),
      '/v1/review/proposal_approve': () => {
        done = true;
        return json({ ok: true });
      },
    });
    const { toast } = renderWithApp(<Review />, api);
    fireEvent.click(await screen.findByTestId('review-row'));
    const panel = await screen.findByTestId('proposal-panel');
    await within(panel).findByText('連出連結數');
    expect(callsTo('/v1/review/proposal_get')[0]!.body).toEqual({ space: 'dev', vault: VAULT, id: 'prop_1' });
    fireEvent.click(screen.getByRole('button', { name: '核准刪除' }));
    await screen.findByTestId('review-empty');
    expect(callsTo('/v1/review/proposal_approve')[0]!.body).toEqual({ space: 'dev', vault: VAULT, id: 'prop_1', expected_rev: 3 });
    expect(toast).toHaveBeenCalledWith('已核准', 'success');
    expect(screen.queryByTestId('proposal-panel')).toBeNull();
  });

  it('駁回沒填原因不送出；填了才送 proposal_reject', async () => {
    const { api, callsTo } = makeApi({
      ...emptyHandlers,
      '/v1/review/queue': () => queue([qItem()]),
      '/v1/review/proposal_get': () => json(detail()),
      '/v1/review/proposal_reject': () => json({ ok: true }),
    });
    renderWithApp(<Review />, api);
    fireEvent.click(await screen.findByTestId('review-row'));
    await screen.findByRole('button', { name: '駁回…' });
    fireEvent.click(screen.getByRole('button', { name: '駁回…' }));
    fireEvent.click(screen.getByRole('button', { name: '確認駁回' }));
    expect(await screen.findByTestId('proposal-reason-required')).toBeTruthy();
    expect(callsTo('/v1/review/proposal_reject')).toHaveLength(0);
    fireEvent.input(screen.getByLabelText('駁回原因'), { target: { value: '還要用' } });
    fireEvent.click(screen.getByRole('button', { name: '確認駁回' }));
    await waitFor(() => expect(callsTo('/v1/review/proposal_reject')).toHaveLength(1));
    expect(callsTo('/v1/review/proposal_reject')[0]!.body).toEqual({ space: 'dev', vault: VAULT, id: 'prop_1', expected_rev: 3, reason: '還要用' });
  });

  it('修改提案：用 ProposalDiff 顯示前後差異', async () => {
    const { api } = makeApi({
      ...emptyHandlers,
      '/v1/review/queue': () => queue([qItem({ kind: 'note_update', summary: '修改標題與內文' })]),
      '/v1/review/proposal_get': () =>
        json(
          detail({
            proposal: proposalRow({
              kind: 'note_update',
              payload: { changes: { title: '新標題', body: 'a\nb2' } },
              base_snapshot: { title: '舊標題', body: 'a\nb' },
            }),
          }),
        ),
    });
    renderWithApp(<Review />, api);
    fireEvent.click(await screen.findByTestId('review-row'));
    const diff = await screen.findByTestId('proposal-diff');
    expect(diff.textContent).toContain('舊標題');
    expect(diff.textContent).toContain('新標題');
    expect(diff.textContent).toContain('- b');
    expect(diff.textContent).toContain('+ b2');
    expect(screen.getByRole('button', { name: '核准' })).toBeTruthy();
  });

  it('proposal_stale：改顯示三欄比對，勾選「以目前版本為基準」後才能核准並帶 rebase_to', async () => {
    const stale = { base: { body: 'a' }, current: { body: 'a\nc' }, proposed: { body: 'a\nb' }, current_updated: '2026-11-11T00:00:00.000Z' };
    let calls = 0;
    const { api, callsTo } = makeApi({
      ...emptyHandlers,
      '/v1/review/queue': () => queue([qItem({ kind: 'note_update' })]),
      '/v1/review/proposal_get': () =>
        json(detail({ proposal: proposalRow({ kind: 'note_update', payload: { changes: { body: 'a\nb' } }, base_snapshot: { body: 'a' } }) })),
      '/v1/review/proposal_approve': () => (++calls === 1 ? apiError(409, 'proposal_stale', stale) : json({ ok: true })),
    });
    renderWithApp(<Review />, api);
    fireEvent.click(await screen.findByTestId('review-row'));
    await screen.findByTestId('proposal-diff');
    fireEvent.click(screen.getByRole('button', { name: '核准' }));
    expect(await screen.findByTestId('proposal-diff-3col')).toBeTruthy();
    expect(screen.getByTestId('proposal-col-current').textContent).toContain('+ c');
    expect(screen.getByTestId('proposal-stale')).toBeTruthy();
    expect(callsTo('/v1/review/proposal_approve')[0]!.body.rebase_to).toBeUndefined();
    expect((screen.getByRole('button', { name: '核准' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '核准' }));
    await waitFor(() => expect(callsTo('/v1/review/proposal_approve')).toHaveLength(2));
    expect(callsTo('/v1/review/proposal_approve')[1]!.body.rebase_to).toBe('2026-11-11T00:00:00.000Z');
  });

  it.each([
    ['proposal_state_conflict', '提案的狀態已經改變'],
    ['proposal_resolved', '已被另一個管道處理完成'],
    ['proposal_executing', '正在執行中'],
    ['proposal_not_found', '找不到這則提案'],
  ])('%s：顯示中文訊息、關閉面板並重新載入佇列', async (code, text) => {
    const { api, callsTo } = makeApi({
      ...emptyHandlers,
      '/v1/review/queue': () => queue([qItem()]),
      '/v1/review/proposal_get': () => json(detail()),
      '/v1/review/proposal_approve': () => apiError(code === 'proposal_not_found' ? 404 : 409, code),
    });
    renderWithApp(<Review />, api);
    fireEvent.click(await screen.findByTestId('review-row'));
    await screen.findByRole('button', { name: '核准刪除' });
    fireEvent.click(screen.getByRole('button', { name: '核准刪除' }));
    expect((await screen.findByTestId('review-notice')).textContent).toContain(text);
    expect(screen.queryByTestId('proposal-panel')).toBeNull();
    await waitFor(() => expect(callsTo('/v1/review/queue')).toHaveLength(2));
  });

  it('task_archive 已同意：只顯示等待 agent 執行，不再有核准按鈕', async () => {
    const { api } = makeApi({
      ...emptyHandlers,
      '/v1/review/queue': () => queue([qItem({ kind: 'task_archive', state: 'approved', target: { type: 'task_change', id: 'c1', title: 'c1' } })]),
      '/v1/review/proposal_get': () =>
        json(detail({ proposal: proposalRow({ kind: 'task_archive', state: 'approved', payload: { reason: '完成', allow_incomplete: false } }) })),
    });
    renderWithApp(<Review />, api);
    fireEvent.click(await screen.findByTestId('review-row'));
    expect(await screen.findByTestId('proposal-approved-note')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '同意封存' })).toBeNull();
  });
});

describe('Notes 列表', () => {
  const listHandlers: Record<string, Handler> = {
    '/v1/topics': () => json({ space: 'dev', vault: '*', topics: [] }),
  };

  it('未審核項目有標記，一般項目沒有', async () => {
    const { api } = makeApi({
      ...listHandlers,
      '/v1/list': () => page([listItem(), listItem({ id: 'n2', title: '一般', review_state: 'none', review: null })]),
    });
    renderWithApp(<Notes />, api);
    await screen.findByText('一般');
    expect(screen.getAllByTestId('unverified-tag')).toHaveLength(1);
  });

  it('審核狀態篩選以 review_state 送出', async () => {
    const { api, callsTo } = makeApi({ ...listHandlers, '/v1/list': () => page([]) });
    renderWithApp(<Notes />, api);
    fireEvent.click(await screen.findByRole('button', { name: '待審' }));
    await waitFor(() => expect(callsTo('/v1/list').at(-1)!.body.review_state).toBe('pending'));
    expect(callsTo('/v1/list')[0]!.body.review_state).toBeUndefined();
  });
});

describe('NoteDetail 審核', () => {
  it.each([
    [{ review_state: 'pending' as const }, '待審（UEP）'],
    [{ review_state: 'pending_delete' as const }, '待刪除'],
    [{ review_state: 'rejected' as const, review_reason: '重複', review: null }, '已駁回：重複'],
    [{ review_state: 'verified' as const, reviewed_by: 'Bernie', review: null }, '已核准 by Bernie'],
  ])('徽章依狀態顯示 %#', async (state, text) => {
    const { api } = makeApi({ '/v1/get': () => getReply(note(state)) });
    renderWithApp(<NoteDetail id="n1" />, api);
    expect((await screen.findByTestId('review-badge')).textContent).toContain(text);
  });

  it('已核准時沒有審核面板', async () => {
    const { api } = makeApi({ '/v1/get': () => getReply(note({ review_state: 'verified', review: null })) });
    renderWithApp(<NoteDetail id="n1" />, api);
    await screen.findByTestId('review-badge');
    expect(screen.queryByTestId('review-panel')).toBeNull();
  });

  it('核准：呼叫 /v1/review/approve 帶 vault 與 id，成功後重新載入', async () => {
    let state: NoteFull = note();
    const { api, callsTo } = makeApi({
      '/v1/get': () => getReply(state),
      '/v1/review/approve': () => {
        state = note({ review_state: 'verified', reviewed_by: 'Bernie', review: null });
        return json({ ok: true });
      },
    });
    renderWithApp(<NoteDetail id="n1" />, api);
    fireEvent.click(await screen.findByRole('button', { name: '核准' }));
    await waitFor(() => expect(screen.getByTestId('review-badge').textContent).toContain('已核准 by Bernie'));
    expect(callsTo('/v1/review/approve')[0]!.body).toEqual({ space: 'dev', vault: VAULT, id: 'n1', expected_updated: '2026-09-26T01:00:00.000Z' });
    expect(screen.queryByTestId('review-panel')).toBeNull();
  });

  it('駁回沒填原因不送出；填了原因才送 /v1/review/reject', async () => {
    let state: NoteFull = note();
    const { api, callsTo } = makeApi({
      '/v1/get': () => getReply(state),
      '/v1/review/reject': () => {
        state = note({ review_state: 'rejected', review_reason: '資訊不正確', review: null });
        return json({ ok: true });
      },
    });
    renderWithApp(<NoteDetail id="n1" />, api);
    fireEvent.click(await screen.findByRole('button', { name: '駁回…' }));
    fireEvent.click(screen.getByRole('button', { name: '確認駁回' }));
    expect(await screen.findByTestId('review-reason-required')).toBeTruthy();
    fireEvent.input(screen.getByLabelText('駁回原因'), { target: { value: '   ' } });
    fireEvent.click(screen.getByRole('button', { name: '確認駁回' }));
    expect(callsTo('/v1/review/reject')).toHaveLength(0);
    fireEvent.input(screen.getByLabelText('駁回原因'), { target: { value: '資訊不正確' } });
    fireEvent.click(screen.getByRole('button', { name: '確認駁回' }));
    await waitFor(() => expect(screen.getByTestId('review-badge').textContent).toContain('已駁回：資訊不正確'));
    expect(callsTo('/v1/review/reject')[0]!.body).toEqual({ space: 'dev', vault: VAULT, id: 'n1', expected_updated: '2026-09-26T01:00:00.000Z', reason: '資訊不正確' });
  });

  it('pending_delete：按鈕為「核准刪除」「保留」；核准刪除後回審核佇列', async () => {
    const { api, callsTo } = makeApi({
      '/v1/get': () => getReply(note({ review_state: 'pending_delete' })),
      '/v1/review/approve': () => json({ ok: true }),
    });
    const { navigate } = renderWithApp(<NoteDetail id="n1" />, api);
    expect(await screen.findByRole('button', { name: '保留…' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '核准刪除' }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/ui/review'));
    expect(callsTo('/v1/review/approve')).toHaveLength(1);
    expect(callsTo('/v1/review/approve')[0]!.body.expected_updated).toBe('2026-09-26T01:00:00.000Z');
  });

  it('pending_delete 保留：同樣要求原因，送 reject', async () => {
    const { api, callsTo } = makeApi({
      '/v1/get': () => getReply(note({ review_state: 'pending_delete' })),
      '/v1/review/reject': () => json({ ok: true }),
    });
    renderWithApp(<NoteDetail id="n1" />, api);
    fireEvent.click(await screen.findByRole('button', { name: '保留…' }));
    fireEvent.click(screen.getByRole('button', { name: '確認保留' }));
    expect(await screen.findByTestId('review-reason-required')).toBeTruthy();
    fireEvent.input(screen.getByLabelText('保留原因'), { target: { value: '還有用' } });
    fireEvent.click(screen.getByRole('button', { name: '確認保留' }));
    await waitFor(() => expect(callsTo('/v1/review/reject')).toHaveLength(1));
    expect(callsTo('/v1/review/reject')[0]!.body.reason).toBe('還有用');
    expect(callsTo('/v1/review/reject')[0]!.body.expected_updated).toBe('2026-09-26T01:00:00.000Z');
  });

  it('409：顯示可理解的訊息並重新載入最新狀態', async () => {
    let state: NoteFull = note();
    const { api, callsTo } = makeApi({
      '/v1/get': () => getReply(state),
      '/v1/review/approve': () => {
        state = note({ review_state: 'verified', reviewed_by: '別人', review: null });
        return apiError(409, 'review_state_conflict', { current_updated: '2026-09-27T00:00:00.000Z' });
      },
    });
    const { toast } = renderWithApp(<NoteDetail id="n1" />, api);
    fireEvent.click(await screen.findByRole('button', { name: '核准' }));
    expect((await screen.findByTestId('review-conflict')).textContent).toContain('審核狀態已經改變');
    expect(screen.getByTestId('review-conflict').textContent).toContain('內容已更新，請重新檢視');
    await waitFor(() => expect(screen.getByTestId('review-badge').textContent).toContain('已核准 by 別人'));
    expect(callsTo('/v1/get').filter((c) => c.body.fields !== 'meta').length).toBeGreaterThanOrEqual(2);
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('審核狀態已改變'), 'warning');
  });

  it('非 409 的錯誤留在面板，不重新載入', async () => {
    const { api } = makeApi({
      '/v1/get': () => getReply(note()),
      '/v1/review/approve': () => apiError(403, 'ui_session_required'),
    });
    renderWithApp(<NoteDetail id="n1" />, api);
    fireEvent.click(await screen.findByRole('button', { name: '核准' }));
    expect((await screen.findByTestId('review-error')).textContent).toContain('ui_session_required');
    expect(screen.getByTestId('review-panel')).toBeTruthy();
  });

  it('有開放中的提案時顯示提示並連到審核頁', async () => {
    const { api } = makeApi({
      '/v1/get': () => getReply(note({ review_state: 'none', review: null, open_proposals: [{ id: 'prop_1', kind: 'note_update', state: 'pending' }] })),
    });
    const { navigate } = renderWithApp(<NoteDetail id="n1" />, api);
    const banner = await screen.findByTestId('open-proposals');
    expect(banner.textContent).toContain('修改筆記');
    fireEvent.click(within(banner).getByRole('button', { name: '前往審核頁' }));
    expect(navigate).toHaveBeenCalledWith('/ui/review');
  });
});
