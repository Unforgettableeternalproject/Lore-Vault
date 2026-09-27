/** @vitest-environment happy-dom */
// 筆記列表：標籤清單走 /v1/topics、摘要預算（budget）與 truncated／summaries_omitted 的呈現、更正鏈標示。
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import type { NoteListItem } from '../lib/types';
import { json, makeApi, renderWithApp, TEST_LIMITS, type Handler } from '../test/harness';
import { dayStartIso, DEFAULT_PAGE_SIZE } from '../components/Pager';
import { LIST_SUMMARY_CHARS, Notes } from './Notes';

afterEach(() => {
  cleanup();
  // happy-dom 的網址在同一檔內跨測試共用：篩選會寫回查詢字串，每個測試後重設
  window.history.replaceState(null, '', '/ui/notes');
});

const VAULT = 'github.com/org/lore-vault';

function item(overrides: Partial<NoteListItem> = {}): NoteListItem {
  return {
    id: 'n1',
    kind: 'note',
    vault: VAULT,
    title: '注入預算',
    topics: ['hook'],
    updated: '2026-09-26T01:00:00.000Z',
    author: 'Minka',
    summary: '第一行',
    summary_source: 'lead',
    supersedes: null,
    superseded_by: null,
    ...overrides,
  };
}

const topics: Handler = () =>
  json({ space: 'dev', vault: '*', topics: [{ topic: 'hook', count: 12 }, { topic: 'e2e', count: 3 }] });

describe('筆記列表', () => {
  it('標籤清單取自 /v1/topics（含筆數）；選標籤後以 topics 篩選', async () => {
    const { api, callsTo } = makeApi({
      '/v1/topics': topics,
      '/v1/list': () => json({ items: [item()], next_cursor: null, has_more: false, unsupported_kinds: [], budget: 8000, used_chars: 3, truncated: false, summaries_omitted: 0 }),
    });
    renderWithApp(<Notes />, api);
    const chip = await screen.findByRole('button', { name: '#hook（12 則）' });
    expect(callsTo('/v1/topics')[0]!.body).toEqual({ space: 'dev', vault: '*' });
    fireEvent.click(chip);
    await waitFor(() => expect(callsTo('/v1/list').at(-1)!.body.topics).toEqual(['hook']));
  });

  it('帶摘要預算；truncated 時顯示省略筆數，可放大預算重取；省略的摘要標為「摘要省略」', async () => {
    let call = 0;
    const { api, callsTo } = makeApi({
      '/v1/topics': topics,
      '/v1/list': (body) => {
        call++;
        const budget = body.budget as number;
        return json({
          items: [item(), item({ id: 'n2', title: '第二則', summary: null, summary_source: 'omitted' })],
          next_cursor: null,
          has_more: false,
          unsupported_kinds: [],
          budget,
          used_chars: budget,
          truncated: call === 1,
          summaries_omitted: call === 1 ? 1 : 0,
        });
      },
    });
    renderWithApp(<Notes />, api);
    const banner = await screen.findByTestId('list-truncated');
    expect(banner.textContent).toContain('1 則的摘要沒有列出');
    const first = callsTo('/v1/list')[0]!.body;
    // 預設每頁 30 則（分頁元件），以 offset／with_total 取頁
    expect(first.limit).toBe(DEFAULT_PAGE_SIZE);
    expect(first).toMatchObject({ offset: 0, with_total: true });
    expect(first.budget).toBe(Math.max(TEST_LIMITS.list_default_budget, DEFAULT_PAGE_SIZE * LIST_SUMMARY_CHARS));
    expect(screen.getByText('摘要省略').getAttribute('data-source')).toBe('omitted');

    fireEvent.click(screen.getByRole('button', { name: '顯示更多摘要' }));
    await waitFor(() => expect(screen.queryByTestId('list-truncated')).toBeNull());
    expect(callsTo('/v1/list').at(-1)!.body.budget).toBe((first.budget as number) * 2);
  });

  it('摘要被截短：列上標「截短」，橫幅分列截短與省略筆數', async () => {
    const { api } = makeApi({
      '/v1/topics': topics,
      '/v1/list': () =>
        json({
          items: [
            item({ summary: '很長的摘要…', summary_source: 'summary', summary_truncated: true }),
            item({ id: 'n2', title: '短的', summary: '短摘要', summary_source: 'summary', summary_truncated: false }),
            item({ id: 'n3', title: '尾端', summary: null, summary_source: 'omitted', summary_truncated: false }),
          ],
          next_cursor: null,
          has_more: false,
          unsupported_kinds: [],
          budget: 14000,
          used_chars: 9,
          truncated: true,
          summaries_omitted: 1,
          summaries_truncated: 1,
        }),
    });
    renderWithApp(<Notes />, api);
    const banner = await screen.findByTestId('list-truncated');
    expect(banner.textContent).toContain('1 則摘要被截短');
    expect(banner.textContent).toContain('1 則的摘要沒有列出');
    const marks = screen.getAllByTestId('summary-truncated');
    expect(marks).toHaveLength(1);
    expect(marks[0]!.closest('[role="row"]')!.textContent).toContain('很長的摘要…');
  });

  it('更正鏈：被取代與更正版在列上標示', async () => {
    const { api } = makeApi({
      '/v1/topics': topics,
      '/v1/list': () =>
        json({
          items: [item({ superseded_by: 'n2' }), item({ id: 'n2', title: '注入預算（更正）', supersedes: 'n1' })],
          next_cursor: null,
          has_more: false,
          unsupported_kinds: [],
          budget: 8000,
          used_chars: 6,
          truncated: false,
          summaries_omitted: 0,
        }),
    });
    renderWithApp(<Notes />, api);
    expect((await screen.findByTestId('note-superseded')).textContent).toContain('已被更正取代');
    expect(screen.getByTestId('note-supersedes').textContent).toContain('更正版');
  });

  it('標籤清單載入失敗要說明，列表照常', async () => {
    const { api } = makeApi({
      '/v1/topics': () => ({ status: 500, body: { error: { code: 'storage_error', message: 'x' } } }),
      '/v1/list': () => json({ items: [item()], next_cursor: null, has_more: false, unsupported_kinds: [], budget: 8000, used_chars: 3, truncated: false, summaries_omitted: 0 }),
    });
    renderWithApp(<Notes />, api);
    expect((await screen.findByTestId('topics-error')).textContent).toContain('storage_error');
    expect(await screen.findByText('注入預算')).toBeTruthy();
  });

  describe('篩選（與記憶層同一套）', () => {
    const emptyList = () => json({ items: [], next_cursor: null, has_more: false, unsupported_kinds: [], budget: 8000, used_chars: 0, truncated: false, summaries_omitted: 0, total: 0 });

    it('初值取自網址；不合法的值當沒有', async () => {
      window.history.replaceState(null, '', '/ui/notes?tag=hook&title=%E6%B3%A8%E5%85%A5&author=Minka&author_state=bogus&from=2026-09-01&to=not-a-date');
      const { api, callsTo } = makeApi({ '/v1/topics': topics, '/v1/list': emptyList });
      renderWithApp(<Notes />, api);
      await waitFor(() => expect(callsTo('/v1/list').length).toBeGreaterThan(0));
      const body = callsTo('/v1/list')[0]!.body;
      expect(body).toMatchObject({ topics: ['hook'], title: '注入', author: 'Minka', since: dayStartIso('2026-09-01'), offset: 0 });
      expect(body).not.toHaveProperty('author_state');
      expect(body).not.toHaveProperty('until');
      expect((screen.getByRole('textbox', { name: '標題關鍵字' }) as HTMLInputElement).value).toBe('注入');
    });

    it('標題、作者、作者狀態、日期送進 /v1/list，並寫回網址', async () => {
      const { api, callsTo } = makeApi({ '/v1/topics': topics, '/v1/list': () => json({ items: [item()], next_cursor: null, has_more: false, unsupported_kinds: [], budget: 8000, used_chars: 3, truncated: false, summaries_omitted: 0, total: 1 }) });
      renderWithApp(<Notes />, api);
      await screen.findByText('注入預算');
      fireEvent.input(screen.getByRole('textbox', { name: '標題關鍵字' }), { target: { value: '  預算 ' } });
      fireEvent.click(screen.getByRole('button', { name: '套用標題' }));
      await waitFor(() => expect(callsTo('/v1/list').at(-1)!.body.title).toBe('預算'));
      fireEvent.input(screen.getByRole('textbox', { name: '作者名稱' }), { target: { value: 'minka' } });
      fireEvent.click(screen.getByRole('button', { name: '套用作者' }));
      fireEvent.click(within(screen.getByRole('group', { name: '作者狀態' })).getByRole('button', { name: '已具名' }));
      fireEvent.input(screen.getByLabelText('起日'), { target: { value: '2026-09-02' } });
      await waitFor(() =>
        expect(callsTo('/v1/list').at(-1)!.body).toMatchObject({ title: '預算', author: 'minka', author_state: 'named', since: dayStartIso('2026-09-02'), offset: 0 }),
      );
      const q = new URLSearchParams(window.location.search);
      expect(Object.fromEntries(q)).toEqual({ title: '預算', author: 'minka', author_state: 'named', from: '2026-09-02' });
      expect(screen.getByTestId('filter-panel').textContent).toContain('標題含「預算」');
    });

    it('篩選後沒有結果：空狀態可一鍵清除全部篩選', async () => {
      window.history.replaceState(null, '', '/ui/notes?title=zzz&author_state=missing');
      const { api, callsTo } = makeApi({ '/v1/topics': topics, '/v1/list': emptyList });
      renderWithApp(<Notes />, api);
      const empty = await screen.findByTestId('notes-empty');
      expect(empty.textContent).toContain('沒有符合篩選條件的筆記');
      fireEvent.click(within(empty).getByRole('button', { name: '清除篩選' }));
      await waitFor(() => {
        const body = callsTo('/v1/list').at(-1)!.body;
        expect(body).not.toHaveProperty('title');
        expect(body).not.toHaveProperty('author_state');
      });
      expect(window.location.search).toBe('');
      expect(screen.queryByTestId('filter-clear')).toBeNull();
      expect((await screen.findByTestId('notes-empty')).textContent).toContain('這裡還沒有筆記');
    });
  });
});
