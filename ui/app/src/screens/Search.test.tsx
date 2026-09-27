/** @vitest-environment happy-dom */
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import { apiError, json, makeApi, renderWithApp } from '../test/harness';
import type { AskResult, RecallItem, RecallResult } from '../lib/types';
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

  it('筆記結果顯示作者；未具名要標出', async () => {
    const { api } = makeApi({
      '/v1/recall': () => json(result({ items: [item({ id: 'a', author: 'Minka' }), item({ id: 'b', author: null })] })),
    });
    renderWithApp(<Search initialQuery="q" />, api);
    await waitFor(() => expect(screen.getAllByTestId('result-author')).toHaveLength(2));
    expect(screen.getAllByTestId('result-author').map((el) => el.textContent)).toEqual(['Minka', '未具名']);
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

function askResult(overrides: Partial<AskResult> = {}): AskResult {
  return {
    status: 'answered',
    answer: { points: [] },
    dropped_citations: [],
    status_downgraded: false,
    sources: [],
    k: 10,
    kinds: ['note'],
    unsupported_kinds: [],
    degraded: false,
    degraded_reason: null,
    degraded_detail: null,
    missing_embeddings: 0,
    model: 'test-model',
    usage: null,
    latency_ms: { retrieval: 90, generation: 2100, total: 2190 },
    notice: '回答是檢索片段的整理，信心有限。',
    ...overrides,
  };
}

function source(id: string, title: string) {
  return { id, vault: 'github.com/org/lore-vault', title, updated: '2026-09-26T01:02:03.000Z', score: 0.03, excerpt_truncated: false };
}

function askFor(question: string) {
  fireEvent.input(screen.getByRole('textbox', { name: '問題' }), { target: { value: question } });
  fireEvent.click(screen.getByRole('button', { name: '提問' }));
}

describe('檢索頁分頁（檢索／問答）', () => {
  it('點擊與方向鍵切換分頁；分頁狀態寫進網址；非作用中的面板隱藏', () => {
    const { api } = makeApi({});
    const { navigate } = renderWithApp(<Search initialQuery="" />, api);
    const recallTab = screen.getByRole('tab', { name: '檢索' });
    const askTab = screen.getByRole('tab', { name: '問答' });
    expect(recallTab.getAttribute('aria-selected')).toBe('true');
    expect(askTab.getAttribute('tabindex')).toBe('-1');
    expect(screen.getByRole('tabpanel').id).toBe('lv-search-panel-recall');
    // 快捷鍵 / 的目標 id 只掛在作用中分頁的輸入框
    expect(document.getElementById('lv-search-input')?.getAttribute('aria-label')).toBe('檢索查詢');

    fireEvent.click(askTab);
    expect(askTab.getAttribute('aria-selected')).toBe('true');
    expect(askTab.getAttribute('tabindex')).toBe('0');
    expect(screen.getByRole('tabpanel').id).toBe('lv-search-panel-ask');
    expect(navigate).toHaveBeenLastCalledWith('/ui/search?mode=ask', { replace: true });
    expect(document.getElementById('lv-search-input')?.getAttribute('aria-label')).toBe('問題');
    expect(screen.getByTestId('ask-notice').textContent).toContain('信心有限');

    fireEvent.keyDown(askTab, { key: 'ArrowLeft' });
    expect(recallTab.getAttribute('aria-selected')).toBe('true');
    expect(document.activeElement).toBe(recallTab);
    expect(navigate).toHaveBeenLastCalledWith('/ui/search', { replace: true });

    fireEvent.keyDown(recallTab, { key: 'End' });
    expect(askTab.getAttribute('aria-selected')).toBe('true');
    expect(document.activeElement).toBe(askTab);
  });

  it('網址帶 mode=ask 與問題：開在問答分頁並填入問題，但不自動呼叫模型', () => {
    const { api, calls } = makeApi({});
    renderWithApp(<Search initialQuery="hook 為什麼只用標準庫" initialMode="ask" />, api);
    expect(screen.getByRole('tab', { name: '問答' }).getAttribute('aria-selected')).toBe('true');
    expect((screen.getByRole('textbox', { name: '問題' }) as HTMLInputElement).value).toBe('hook 為什麼只用標準庫');
    expect(calls).toHaveLength(0);
  });

  it('成功回答：逐點列出、引用連到筆記、無依據的點有標記、問題寫進網址', async () => {
    const { api, callsTo } = makeApi({
      '/v1/ask': () =>
        json(
          askResult({
            answer: {
              points: [
                { claim: 'hook 會被系統 Python 直接執行', note_ids: ['n1', 'n2'], unsupported: false },
                { claim: '也許還有其他原因', note_ids: [], unsupported: true },
              ],
            },
            sources: [source('n1', 'hook 規範'), source('n2', 'PreToolUse 成本')],
          }),
        ),
    });
    const { navigate } = renderWithApp(<Search initialQuery="" initialMode="ask" />, api);
    askFor('hook 為什麼只用標準庫？');
    await screen.findByTestId('ask-result');
    expect(callsTo('/v1/ask')[0]!.body).toEqual({ space: 'dev', question: 'hook 為什麼只用標準庫？', vault: '*' });
    expect(navigate).toHaveBeenCalledWith(
      `/ui/search?${new URLSearchParams({ mode: 'ask', q: 'hook 為什麼只用標準庫？' }).toString()}`,
      { replace: true },
    );

    const pts = screen.getAllByTestId('ask-point');
    expect(pts).toHaveLength(2);
    const cites = within(pts[0]!).getAllByRole('link');
    expect(cites.map((a) => a.textContent)).toEqual(['hook 規範', 'PreToolUse 成本']);
    expect(cites[0]!.getAttribute('href')).toBe('/ui/notes/n1');
    expect(within(pts[0]!).queryByTestId('ask-unsupported')).toBeNull();
    expect(within(pts[1]!).getByTestId('ask-unsupported').textContent).toBe('無依據');

    fireEvent.click(cites[1]!);
    expect(navigate).toHaveBeenLastCalledWith('/ui/notes/n2');
  });

  it('拒答（片段不足、沒有任何點）：顯示依據不足的空狀態，不當成錯誤', async () => {
    const { api } = makeApi({
      '/v1/ask': () => json(askResult({ status: 'insufficient', sources: [source('n1', '無關筆記')] })),
    });
    renderWithApp(<Search initialQuery="" initialMode="ask" />, api);
    askFor('月球上有幾隻貓');
    const empty = await screen.findByTestId('ask-insufficient');
    expect(empty.textContent).toContain('不足以回答');
    expect(screen.queryByTestId('ask-point')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('部分依據（insufficient 但有點）：標出只能部分回答', async () => {
    const { api } = makeApi({
      '/v1/ask': () =>
        json(
          askResult({
            status: 'insufficient',
            answer: { points: [{ claim: '只提到一部分', note_ids: ['n1'], unsupported: false }] },
            sources: [source('n1', '片段')],
          }),
        ),
    });
    renderWithApp(<Search initialQuery="" initialMode="ask" />, api);
    askFor('q');
    expect((await screen.findByTestId('ask-partial')).textContent).toContain('部分回答');
    expect(screen.getAllByTestId('ask-point')).toHaveLength(1);
  });

  it('429 限流：友善提示稍後再試（帶秒數），可再試一次', async () => {
    const { api, callsTo } = makeApi({
      '/v1/ask': (_body, n) =>
        n === 1
          ? apiError(429, 'ask_rate_limited', { retry_after: 12 })
          : json(askResult({ answer: { points: [{ claim: '好了', note_ids: [], unsupported: true }] } })),
    });
    renderWithApp(<Search initialQuery="" initialMode="ask" />, api);
    askFor('q');
    const banner = await screen.findByTestId('ask-rate-limited');
    expect(banner.textContent).toContain('稍後再試');
    expect(banner.textContent).toContain('12 秒');
    expect(screen.queryByRole('alert')).toBeNull();
    fireEvent.click(within(banner).getByRole('button', { name: '再試一次' }));
    await screen.findByTestId('ask-result');
    expect(callsTo('/v1/ask')).toHaveLength(2);
    expect(screen.queryByTestId('ask-rate-limited')).toBeNull();
  });

  it('模型錯誤：顯示白話說明與錯誤碼', async () => {
    const { api } = makeApi({ '/v1/ask': () => apiError(500, 'ask_provider_error') });
    renderWithApp(<Search initialQuery="" initialMode="ask" />, api);
    askFor('q');
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('問答模型服務');
    expect(alert.textContent).toContain('ask_provider_error');
  });

  it('載入中：顯示進度並停用送出', async () => {
    let release: () => void = () => {};
    const { api } = makeApi({
      '/v1/ask': () =>
        new Promise((resolve) => {
          release = () => resolve(json(askResult()));
        }),
    });
    renderWithApp(<Search initialQuery="" initialMode="ask" />, api);
    askFor('q');
    await screen.findByText('檢索並整理回答中…（約數秒）');
    expect((screen.getByRole('button', { name: '整理中…' }) as HTMLButtonElement).disabled).toBe(true);
    release();
    await screen.findByTestId('ask-result');
  });
});
