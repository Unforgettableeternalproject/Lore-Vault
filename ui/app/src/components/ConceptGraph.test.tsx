/** @vitest-environment happy-dom */
// ConceptGraph：canvas 以假的 2d context 取代（happy-dom 沒有繪圖），驗證文字替代（側欄）與互動。
import { cleanup, fireEvent, render, screen, within } from '@testing-library/preact';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConceptGraphData, ConceptItem } from '../lib/types';
import { ConceptGraph } from './ConceptGraph';

function fakeContext() {
  return new Proxy({ measureText: () => ({ width: 40 }) } as Record<string, unknown>, {
    get: (target, prop) => (prop in target ? target[prop as string] : vi.fn()),
    set: () => true,
  });
}

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => fakeContext()) as never);
  vi.stubGlobal('requestAnimationFrame', () => 0);
  vi.stubGlobal('cancelAnimationFrame', () => undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function item(id: string, extra: Partial<ConceptItem> = {}): ConceptItem {
  return {
    id,
    vault: 'v',
    kind: 'project-fact',
    scope: 'graph-x',
    scope_state: 'repo',
    statement: `${id} 的完整陳述`,
    anchors: ['a.py'],
    surprisal: 2.5,
    usability_verdict: null,
    updated: '2026-01-01T00:00:00.000Z',
    ...extra,
  };
}

const DATA: ConceptGraphData = {
  nodes: [
    item('n1', { anchors: ['a.py', 'b.py'] }),
    item('n2', { kind: 'user-stance', anchors: ['b.py'] }),
    item('n3', { kind: 'belief-correction', anchors: ['c.py'] }),
  ],
  edges: [{ source: 'n1', target: 'n2', weight: 1, via: ['b.py'] }],
  truncated: false,
  total: 3,
};

describe('ConceptGraph', () => {
  it('顯示 N / total、圖例，canvas 有文字說明', () => {
    render(<ConceptGraph data={DATA} maxNodes={500} />);
    expect(screen.getByTestId('graph-count').textContent).toContain('3 / 3');
    expect(screen.queryByTestId('graph-truncated')).toBeNull();
    const legend = within(screen.getByTestId('graph-legend'));
    expect(legend.getByText('project-fact')).toBeTruthy();
    expect(legend.getByText('user-stance')).toBeTruthy();
    expect(legend.getByText('belief-correction')).toBeTruthy();
    expect(screen.getByRole('img').getAttribute('aria-label')).toContain('3 個節點');
  });

  it('截斷時顯示提示與 N / total', () => {
    render(<ConceptGraph data={{ ...DATA, truncated: true, total: 812 }} maxNodes={500} />);
    expect(screen.getByTestId('graph-count').textContent).toContain('3 / 812');
    expect(screen.getByTestId('graph-truncated').textContent).toContain('812');
  });

  it('節點清單為按鈕，相鄰多者在前；點選後側欄顯示完整內容、錨點與相鄰節點', () => {
    render(<ConceptGraph data={DATA} maxNodes={500} />);
    expect(screen.getByTestId('graph-detail-empty')).toBeTruthy();
    const buttons = within(screen.getByTestId('graph-nodes')).getAllByRole('button');
    expect(buttons[0]!.textContent).toContain('n1');
    expect(buttons.at(-1)!.textContent).toContain('n3'); // 孤立節點殿後

    fireEvent.click(buttons[0]!);
    expect(screen.getByTestId('graph-detail-statement').textContent).toBe('n1 的完整陳述');
    const anchors = within(screen.getByTestId('graph-detail-anchors')).getAllByRole('listitem');
    expect(anchors.map((a) => a.textContent)).toEqual(['a.py', 'b.py']);
    expect(screen.getByTestId('graph-detail').textContent).toContain('surprisal');
    expect(screen.getByTestId('graph-detail').textContent).toContain('2.50');
    const pressed = within(screen.getByTestId('graph-nodes')).getAllByRole('button', { pressed: true });
    expect(pressed).toHaveLength(1);
    expect(pressed[0]!.textContent).toContain('n1');
    const neighbors = within(screen.getByTestId('graph-neighbors')).getAllByRole('button');
    expect(neighbors).toHaveLength(1);
    expect(neighbors[0]!.textContent).toContain('n2');
    expect(neighbors[0]!.textContent).toContain('b.py');
  });

  it('點相鄰節點清單跳到該節點；取消選取回到提示', () => {
    render(<ConceptGraph data={DATA} maxNodes={500} />);
    fireEvent.click(within(screen.getByTestId('graph-nodes')).getAllByRole('button')[0]!);
    fireEvent.click(within(screen.getByTestId('graph-neighbors')).getByRole('button'));
    expect(screen.getByTestId('graph-detail-statement').textContent).toBe('n2 的完整陳述');
    expect(screen.getByTestId('graph-detail').textContent).toContain('user-stance');
    fireEvent.click(screen.getByRole('button', { name: '取消選取' }));
    expect(screen.getByTestId('graph-detail-empty')).toBeTruthy();
  });

  it('孤立節點的側欄說明沒有相鄰節點', () => {
    render(<ConceptGraph data={DATA} maxNodes={500} />);
    fireEvent.click(within(screen.getByTestId('graph-nodes')).getAllByRole('button').at(-1)!);
    expect(screen.queryByTestId('graph-neighbors')).toBeNull();
    expect(screen.getByTestId('graph-detail').textContent).toContain('孤立節點');
  });

  it('搜尋節點縮小清單', () => {
    render(<ConceptGraph data={DATA} maxNodes={500} />);
    fireEvent.input(screen.getByRole('searchbox', { name: '搜尋節點' }), { target: { value: 'c.py' } });
    const buttons = within(screen.getByTestId('graph-nodes')).getAllByRole('button');
    expect(buttons).toHaveLength(1);
    expect(buttons[0]!.textContent).toContain('n3');
    expect(screen.getByTestId('graph-list-count').textContent).toContain('符合 1 個');
  });

  it('canvas 不可用時（沒有 2d context）側欄仍可用', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    render(<ConceptGraph data={DATA} maxNodes={500} />);
    fireEvent.click(within(screen.getByTestId('graph-nodes')).getAllByRole('button')[0]!);
    expect(screen.getByTestId('graph-detail-statement').textContent).toBe('n1 的完整陳述');
  });
});
