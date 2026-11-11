/** @vitest-environment happy-dom */
// graphEngine：以假的 2d context 與固定尺寸驗證實際繪製路徑（排版、縮放、點擊、銷毀）。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createGraphEngine } from './graphEngine';
import type { ConceptEdge, ConceptItem } from './types';

function item(id: string, extra: Partial<ConceptItem> = {}): ConceptItem {
  return {
    id,
    vault: 'v',
    kind: 'project-fact',
    scope: 's',
    scope_state: 'repo',
    statement: `${id} 陳述`,
    anchors: [],
    surprisal: 1,
    usability_verdict: null,
    updated: '2026-01-01T00:00:00.000Z',
    ...extra,
  };
}

const ITEMS = [item('a'), item('b', { kind: 'user-stance' }), item('c')];
const EDGES: ConceptEdge[] = [{ source: 'a', target: 'b', weight: 2, via: ['x', 'y'] }];

let calls: Record<string, number>;
let frames: FrameRequestCallback[];

function flush() {
  const run = frames;
  frames = [];
  run.forEach((f) => f(0));
}

function setup() {
  const canvas = document.createElement('canvas');
  vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({
    x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}),
  } as DOMRect);
  document.body.appendChild(canvas);
  return canvas;
}

beforeEach(() => {
  calls = {};
  frames = [];
  const ctx = new Proxy({ measureText: () => ({ width: 40 }) } as Record<string, unknown>, {
    get: (target, prop) =>
      prop in target
        ? target[prop as string]
        : (...args: unknown[]) => {
            void args;
            calls[String(prop)] = (calls[String(prop)] ?? 0) + 1;
          },
    set: () => true,
  });
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => ctx) as never);
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => frames.push(cb));
  vi.stubGlobal('cancelAnimationFrame', () => undefined);
});

afterEach(() => {
  document.body.innerHTML = '';
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('createGraphEngine', () => {
  it('沒有 2d context 時回傳 null', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    expect(createGraphEngine(setup(), ITEMS, EDGES, { onSelect: vi.fn() })).toBeNull();
  });

  it('畫出每個節點與每條邊，並依畫布尺寸設定像素大小', () => {
    const canvas = setup();
    const engine = createGraphEngine(canvas, ITEMS, EDGES, { onSelect: vi.fn() })!;
    flush();
    expect(canvas.width).toBe(800);
    expect(calls.arc).toBeGreaterThanOrEqual(ITEMS.length);
    expect(calls.lineTo).toBeGreaterThanOrEqual(EDGES.length);
    engine.destroy();
  });

  it('點擊空白處回報 null；縮放、平移、聚焦、重設、解除固定、搜尋與選取都不會出錯', () => {
    const canvas = setup();
    const onSelect = vi.fn();
    const engine = createGraphEngine(canvas, ITEMS, EDGES, { onSelect })!;
    flush();
    canvas.dispatchEvent(new MouseEvent('click', { clientX: 5, clientY: 5, bubbles: true }));
    expect(onSelect).toHaveBeenLastCalledWith(null);

    engine.zoomBy(1.4);
    engine.focus('b');
    engine.setSelected('b');
    engine.setMatches(new Set(['a']));
    engine.unpinAll();
    engine.resetView();
    flush();
    canvas.dispatchEvent(new MouseEvent('mousemove', { clientX: 400, clientY: 300, bubbles: true }));
    canvas.dispatchEvent(new MouseEvent('mouseleave', { bubbles: true }));
    engine.focus('不存在');
    engine.destroy();
    // 銷毀後事件不再回報
    const count = onSelect.mock.calls.length;
    canvas.dispatchEvent(new MouseEvent('click', { clientX: 5, clientY: 5, bubbles: true }));
    expect(onSelect.mock.calls.length).toBe(count);
  });

  it('節點多時走預跑排版路徑，仍畫出全部節點', () => {
    const many = Array.from({ length: 80 }, (_, i) => item(`n${i}`));
    const edges: ConceptEdge[] = many.slice(1).map((n, i) => ({ source: many[i]!.id, target: n.id, weight: 1, via: ['x'] }));
    const canvas = setup();
    const engine = createGraphEngine(canvas, many, edges, { onSelect: vi.fn() })!;
    flush();
    expect(calls.arc).toBeGreaterThanOrEqual(many.length);
    engine.destroy();
  });
});
