/** @vitest-environment happy-dom */
// 筆記列表：標籤清單走 /v1/topics、摘要預算（budget）與 truncated／summaries_omitted 的呈現、更正鏈標示。
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import type { NoteListItem } from '../lib/types';
import { json, makeApi, renderWithApp, TEST_LIMITS, type Handler } from '../test/harness';
import { LIST_SUMMARY_CHARS, Notes } from './Notes';

afterEach(cleanup);

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
    expect(first.limit).toBe(TEST_LIMITS.list_default_limit);
    expect(first.budget).toBe(TEST_LIMITS.list_default_limit * LIST_SUMMARY_CHARS);
    expect(screen.getByText('摘要省略').getAttribute('data-source')).toBe('omitted');

    fireEvent.click(screen.getByRole('button', { name: '顯示更多摘要' }));
    await waitFor(() => expect(screen.queryByTestId('list-truncated')).toBeNull());
    expect(callsTo('/v1/list').at(-1)!.body.budget).toBe((first.budget as number) * 2);
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
});
