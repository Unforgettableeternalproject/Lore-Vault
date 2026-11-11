/** @vitest-environment happy-dom */
// Memory 的「列表／圖譜」切換：共用篩選面板；scope 未選時不查圖、只顯示提示。
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConceptGraphData, ConceptItem } from '../lib/types';
import { json, makeApi, renderWithApp, type Handler } from '../test/harness';
import { Memory } from './Memory';

function item(id: string, extra: Partial<ConceptItem> = {}): ConceptItem {
  return {
    id,
    vault: 'github.com/org/lore-vault',
    kind: 'project-fact',
    scope: 'lore-vault',
    scope_state: 'repo',
    statement: `${id} 陳述`,
    anchors: ['a.py'],
    surprisal: 1,
    usability_verdict: null,
    updated: '2026-01-01T00:00:00.000Z',
    ...extra,
  };
}

const GRAPH: ConceptGraphData = {
  nodes: [item('g1'), item('g2')],
  edges: [{ source: 'g1', target: 'g2', weight: 1, via: ['a.py'] }],
  truncated: false,
  total: 2,
};

const handlers = (graph: ConceptGraphData = GRAPH): Record<string, Handler> => ({
  '/v1/concept_query': () =>
    json({ items: [item('l1', { usability_verdict: 'APPLIED' }), item('l2')], next_cursor: null, total: 2 }),
  '/v1/concept_graph': () => json(graph),
  '/v1/concept_scopes': () =>
    json({ scopes: [{ scope: 'lore-vault', count: 40 }, { scope: 'nope', count: 3 }, { scope: null, count: 7 }] }),
  '/v1/episode_summary': () => json({ total: 0, last_recorded: null, by_machine: [], by_vault: [] }),
});

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    (() => new Proxy({}, { get: () => vi.fn(), set: () => true })) as never,
  );
  vi.stubGlobal('requestAnimationFrame', () => 0);
  vi.stubGlobal('cancelAnimationFrame', () => undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function openGraph(api: ReturnType<typeof makeApi>['api']) {
  renderWithApp(<Memory />, api);
  await screen.findByTestId('concepts');
  fireEvent.click(screen.getByRole('button', { name: '圖譜' }));
}

async function pickScope(scope: string) {
  const select = (await screen.findByRole('combobox', { name: 'scope 名稱' })) as HTMLSelectElement;
  await waitFor(() => expect([...select.options].some((o) => o.value === scope)).toBe(true));
  fireEvent.change(select, { target: { value: scope } });
}

describe('Memory 視圖切換', () => {
  it('預設列表；切到圖譜且 scope 為「全部」時只顯示提示、不查 concept_graph', async () => {
    const { api, callsTo } = makeApi(handlers());
    await openGraph(api);
    expect(await screen.findByTestId('graph-need-scope')).toBeTruthy();
    expect(screen.queryByTestId('concepts')).toBeNull();
    expect(callsTo('/v1/concept_graph')).toHaveLength(0);
  });

  it('圖譜的 scope 是下拉選單：依筆數列出、通用（無 scope）停用、選回「全部」又回到提示', async () => {
    const { api, callsTo } = makeApi(handlers());
    await openGraph(api);
    const select = (await screen.findByRole('combobox', { name: 'scope 名稱' })) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
    expect([...select.options].map((o) => o.textContent)).toEqual([
      '全部',
      'lore-vault（40）',
      'nope（3）',
      '通用・無 scope（7，不能畫圖）',
    ]);
    expect(select.options[3]!.disabled).toBe(true);
    expect(callsTo('/v1/concept_scopes').at(-1)!.body).toMatchObject({ space: 'dev', vault: '*' });
    await pickScope('lore-vault');
    expect(await screen.findByTestId('concept-graph')).toBeTruthy();
    fireEvent.change(select, { target: { value: '' } });
    expect(await screen.findByTestId('graph-need-scope')).toBeTruthy();
    expect(screen.queryByTestId('concept-graph')).toBeNull();
  });

  it('列表檢視維持文字輸入、不查 concept_scopes', async () => {
    const { api, callsTo } = makeApi(handlers());
    renderWithApp(<Memory />, api);
    await screen.findByTestId('concepts');
    expect(screen.getByRole('textbox', { name: 'scope 名稱' })).toBeTruthy();
    expect(callsTo('/v1/concept_scopes')).toHaveLength(0);
  });

  it('套用 scope 後查 concept_graph（共用 kind 篩選），顯示圖與 N / total', async () => {
    const { api, callsTo } = makeApi(handlers());
    await openGraph(api);
    fireEvent.change(screen.getByRole('combobox', { name: 'concept 類型' }), { target: { value: 'user-stance' } });
    await pickScope('lore-vault');
    expect(await screen.findByTestId('concept-graph')).toBeTruthy();
    expect(screen.getByTestId('graph-count').textContent).toContain('2 / 2');
    await waitFor(() =>
      expect(callsTo('/v1/concept_graph').at(-1)!.body).toMatchObject({
        space: 'dev',
        scope: 'lore-vault',
        kind: 'user-stance',
        max_nodes: 500,
      }),
    );
  });

  it('截斷時顯示提示；點節點清單開啟側欄，再切回列表', async () => {
    const { api } = makeApi(handlers({ ...GRAPH, truncated: true, total: 900 }));
    await openGraph(api);
    await pickScope('lore-vault');
    expect((await screen.findByTestId('graph-truncated')).textContent).toContain('900');
    expect(screen.getByTestId('graph-count').textContent).toContain('2 / 900');
    fireEvent.click(within(screen.getByTestId('graph-nodes')).getAllByRole('button')[0]!);
    expect(screen.getByTestId('graph-detail-statement').textContent).toBe('g1 陳述');

    fireEvent.click(screen.getByRole('button', { name: '列表' }));
    expect(await screen.findByTestId('concepts')).toBeTruthy();
    expect(screen.queryByTestId('concept-graph')).toBeNull();
  });

  it('這個 scope 沒有 concept：顯示空狀態', async () => {
    const { api } = makeApi(handlers({ nodes: [], edges: [], truncated: false, total: 0 }));
    await openGraph(api);
    await pickScope('nope');
    expect(await screen.findByText('這個 scope 沒有符合條件的 concept')).toBeTruthy();
  });
});

describe('可用性徽章', () => {
  it('沒有判定時顯示「未驗證」並說明不是 surprisal 校準', async () => {
    const { api } = makeApi(handlers());
    renderWithApp(<Memory />, api);
    const list = await screen.findByTestId('concepts');
    expect(list.textContent).toContain('未驗證');
    expect(list.textContent).not.toContain('未校準');
    expect(list.textContent).toContain('APPLIED');
    expect(list.querySelector('[title="注入實驗判定，非 surprisal 校準"]')).not.toBeNull();
  });
});
