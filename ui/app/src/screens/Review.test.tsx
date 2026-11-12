/** @vitest-environment happy-dom */
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import type { NoteFull, NoteListItem } from '../lib/types';
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

describe('審核佇列', () => {
  it('分別查 pending 與 pending_delete，合併後依更新時間新到舊列出 author／vault／時間', async () => {
    const { api, callsTo } = makeApi({
      '/v1/list': (body) =>
        body.review_state === 'pending'
          ? page([listItem({ id: 'a', title: '較舊', updated: '2026-09-20T00:00:00.000Z' })])
          : page([listItem({ id: 'b', title: '較新（刪除）', review_state: 'pending_delete', updated: '2026-09-27T00:00:00.000Z' })]),
    });
    const { navigate } = renderWithApp(<Review />, api);
    await screen.findByText('較新（刪除）');
    expect(callsTo('/v1/list').map((c) => c.body.review_state).sort()).toEqual(['pending', 'pending_delete']);
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((r) => r.querySelector('.lv-table__title')?.textContent)).toEqual(['較新（刪除）', '較舊']);
    expect(within(rows[0]!).getByTestId('unverified-tag').textContent).toBe('待刪除');
    expect(within(rows[1]!).getByTestId('unverified-tag').textContent).toBe('未審核');
    expect(within(rows[0]!).getByTestId('review-author').textContent).toBe('uep');
    fireEvent.click(rows[0]!);
    expect(navigate).toHaveBeenCalledWith('/ui/notes/b');
  });

  it('標頭用共用的 lv-screen__head，VAULT 選擇器在標頭之外的 lv-filters 列（與其他頁同一套間距）', async () => {
    const { api } = makeApi({ '/v1/list': () => page([]) });
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
    const { api } = makeApi({ '/v1/list': () => page([]) });
    renderWithApp(<Review />, api);
    expect(await screen.findByTestId('review-empty')).toBeTruthy();
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
});
