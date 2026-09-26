/** @vitest-environment happy-dom */
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import { json, makeApi, renderWithApp } from '../test/harness';
import type { RecallItem, RecallResult } from '../lib/types';
import { Search } from './Search';

afterEach(cleanup);

function result(overrides: Partial<RecallResult> = {}): RecallResult {
  return {
    items: [],
    mode: 'hybrid',
    legs: ['lexical', 'vector'],
    degraded: false,
    degraded_reason: null,
    degraded_detail: null,
    truncated: false,
    omitted: 0,
    budget: 2000,
    used_chars: 100,
    unsupported_kinds: [],
    missing_embeddings: 0,
    kinds: ['note', 'chunk'],
    missing_chunk_embeddings: 0,
    ...overrides,
  };
}

function item(overrides: Partial<RecallItem>): RecallItem {
  return {
    id: 'n1',
    kind: 'note',
    vault: 'github.com/org/lore-vault',
    title: '標題',
    summary: '摘要文字',
    summary_source: 'summary',
    score: 0.0321,
    updated: '2026-09-26T01:02:03.000Z',
    ...overrides,
  };
}

describe('檢索狀態呈現', () => {
  it('降級：橫幅含原因與細節、模式標為 KEYWORD ONLY、分數標「關鍵字」、不支援的種類列出', async () => {
    const { api } = makeApi({
      '/v1/recall': () =>
        json(
          result({
            degraded: true,
            degraded_reason: 'embedder_unavailable',
            degraded_detail: 'connection refused',
            legs: ['lexical'],
            unsupported_kinds: ['chunk'],
            items: [item({})],
          }),
        ),
    });
    renderWithApp(<Search initialQuery="hook 預算" />, api);
    const banner = await screen.findByTestId('recall-degraded');
    expect(banner.textContent).toContain('DEGRADED');
    expect(banner.textContent).toContain('embedder_unavailable');
    expect(banner.textContent).toContain('connection refused');
    expect(screen.getByTestId('recall-mode').textContent).toBe('KEYWORD ONLY');
    expect(screen.getByText(/關鍵字 · 0\.0321/)).toBeTruthy();
    expect(screen.getByTestId('recall-unsupported').textContent).toContain('文件段落');
  });

  it('截斷：顯示 omitted 筆數；「載入其餘結果」以更大的 budget／limit 重查', async () => {
    const { api, callsTo } = makeApi({
      '/v1/recall': (_body, n) =>
        json(n === 1 ? result({ truncated: true, omitted: 7, used_chars: 1962, items: [item({})] }) : result({ items: [item({})] })),
    });
    renderWithApp(<Search initialQuery="q" />, api);
    const trunc = await screen.findByTestId('recall-truncated');
    expect(trunc.textContent).toContain('7 筆');
    expect(trunc.textContent).toContain('2,000');
    fireEvent.click(within(trunc).getByRole('button', { name: '載入其餘結果' }));
    await waitFor(() => expect(callsTo('/v1/recall')).toHaveLength(2));
    const [first, second] = callsTo('/v1/recall');
    expect(first!.body).toMatchObject({ query: 'q', vault: '*', space: 'dev', budget: 2000, limit: 10 });
    expect(second!.body.budget as number).toBeGreaterThan(2000);
    expect(second!.body.limit as number).toBeGreaterThanOrEqual(8);
    await waitFor(() => expect(screen.queryByTestId('recall-truncated')).toBeNull());
  });

  it('截斷但 omitted=0：說明是摘要被截短', async () => {
    const { api } = makeApi({ '/v1/recall': () => json(result({ truncated: true, omitted: 0, items: [item({})] })) });
    renderWithApp(<Search initialQuery="q" />, api);
    const trunc = await screen.findByTestId('recall-truncated');
    expect(trunc.textContent).toContain('部分摘要被截短');
  });

  it('四種摘要來源都有標籤；文件段落顯示 locator', async () => {
    const { api } = makeApi({
      '/v1/recall': () =>
        json(
          result({
            items: [
              item({ id: 'a', summary_source: 'summary' }),
              item({ id: 'b', summary_source: 'lead' }),
              item({ id: 'c', summary_source: 'none', summary: null }),
              item({
                id: 'chunk:u1:3',
                kind: 'chunk',
                title: 'hook-flow.pptx',
                summary_source: 'excerpt',
                document_id: 'doc:u1',
                chunk_id: 'chunk:u1:3',
                locator: { kind: 'slide', value: 6 },
              }),
            ],
          }),
        ),
    });
    const { navigate } = renderWithApp(<Search initialQuery="q" />, api);
    await screen.findByText('hook-flow.pptx');
    const tags = Array.from(document.querySelectorAll('.lv-src')).map((el) => el.textContent);
    expect(tags).toEqual(['摘要', '首段', '無摘要', '摘錄']);
    expect(screen.getByText('投影片 6')).toBeTruthy();
    fireEvent.click(screen.getByText('hook-flow.pptx'));
    expect(navigate).toHaveBeenCalledWith('/ui/docs/doc%3Au1?chunk=3&q=q');
  });

  it('缺向量：提示語意那一路查不到', async () => {
    const { api } = makeApi({
      '/v1/recall': () => json(result({ missing_embeddings: 4, missing_chunk_embeddings: 2, items: [item({})] })),
    });
    renderWithApp(<Search initialQuery="q" />, api);
    const note = await screen.findByTestId('recall-missing-vectors');
    expect(note.textContent).toContain('4 則筆記');
    expect(note.textContent).toContain('2 個文件段落');
  });

  it('空結果有空狀態', async () => {
    const { api } = makeApi({ '/v1/recall': () => json(result()) });
    renderWithApp(<Search initialQuery="沒有這個" />, api);
    expect((await screen.findByTestId('recall-empty')).textContent).toContain('沒有這個');
  });

  it('服務錯誤顯示錯誤碼，不當成空結果', async () => {
    const { api } = makeApi({
      '/v1/recall': () => ({ status: 404, body: { error: { code: 'unknown_vault', message: 'x' } } }),
    });
    renderWithApp(<Search initialQuery="q" />, api);
    expect((await screen.findByRole('alert')).textContent).toContain('unknown_vault');
    expect(screen.queryByTestId('recall-empty')).toBeNull();
  });
});
