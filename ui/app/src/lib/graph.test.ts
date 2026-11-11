// 概念圖譜純函式：座標換算、hit-test、鄰居、搜尋、視覺編碼。
import { describe, expect, it } from 'vitest';

import {
  centerOn,
  edgeWidth,
  hitTest,
  kindToken,
  matchNodes,
  neighborMap,
  nodeRadius,
  preTicks,
  screenToWorld,
  sortByDegree,
  summarize,
  worldToScreen,
  type Placed,
} from './graph';
import type { ConceptEdge, ConceptItem } from './types';

function item(id: string, extra: Partial<ConceptItem> = {}): ConceptItem {
  return {
    id,
    vault: 'v',
    kind: 'project-fact',
    scope: 's',
    scope_state: 'repo',
    statement: `陳述 ${id}`,
    anchors: [],
    surprisal: 1,
    usability_verdict: null,
    updated: '2026-01-01T00:00:00.000Z',
    ...extra,
  };
}

const EDGES: ConceptEdge[] = [
  { source: 'a', target: 'b', weight: 3, via: ['x', 'y', 'z'] },
  { source: 'a', target: 'c', weight: 1, via: ['x'] },
  { source: 'b', target: 'd', weight: 1, via: ['q'] },
];

describe('座標換算', () => {
  const t = { x: 40, y: -20, k: 2 };

  it('screen 與 world 互為反函數', () => {
    const [wx, wy] = screenToWorld(t, 100, 60);
    expect([wx, wy]).toEqual([30, 40]);
    expect(worldToScreen(t, wx, wy)).toEqual([100, 60]);
  });

  it('centerOn 讓指定的 world 點落在畫布正中央並保留縮放', () => {
    const c = centerOn(30, 40, 2, 800, 600);
    expect(c.k).toBe(2);
    expect(worldToScreen(c, 30, 40)).toEqual([400, 300]);
  });
});

describe('hitTest', () => {
  const nodes: Placed[] = [
    { id: 'under', x: 0, y: 0, r: 10 },
    { id: 'over', x: 5, y: 0, r: 10 },
    { id: 'far', x: 200, y: 200, r: 6 },
  ];
  const identity = { x: 0, y: 0, k: 1 };

  it('命中半徑內回傳節點，重疊時取較晚繪製者', () => {
    expect(hitTest(nodes, identity, 2, 0)?.id).toBe('over');
    expect(hitTest(nodes, identity, -9, 0)?.id).toBe('under');
  });

  it('空白處回傳 null', () => {
    expect(hitTest(nodes, identity, 100, 100)).toBeNull();
  });

  it('縮放與平移後仍以 screen 座標命中', () => {
    const t = { x: 300, y: 300, k: 0.5 };
    const [sx, sy] = worldToScreen(t, 200, 200);
    expect(hitTest(nodes, t, sx, sy)?.id).toBe('far');
    expect(hitTest(nodes, t, sx + 50, sy)).toBeNull();
  });

  it('縮小時多給的容差以螢幕像素計', () => {
    const t = { x: 0, y: 0, k: 0.2 };
    // 節點半徑 6 * 0.2 = 1.2px；偏 3px 以內仍點得到，偏 5px 不行
    const [sx, sy] = worldToScreen(t, 200, 200);
    expect(hitTest(nodes, t, sx + 3, sy)?.id).toBe('far');
    expect(hitTest(nodes, t, sx + 5, sy)).toBeNull();
  });
});

describe('鄰居與排序', () => {
  it('neighborMap 雙向、權重大者在前', () => {
    const map = neighborMap(EDGES);
    expect(map.get('a')!.map((n) => [n.id, n.weight])).toEqual([
      ['b', 3],
      ['c', 1],
    ]);
    expect(map.get('b')!.map((n) => n.id)).toEqual(['a', 'd']);
    expect(map.get('d')!.map((n) => n.id)).toEqual(['b']);
    expect(map.has('zzz')).toBe(false);
  });

  it('sortByDegree：相鄰多者在前，同數依 id；孤立節點殿後', () => {
    const nodes = ['d', 'c', 'e', 'b', 'a'].map((id) => item(id));
    expect(sortByDegree(nodes, neighborMap(EDGES)).map((n) => n.id)).toEqual(['a', 'b', 'c', 'd', 'e']);
  });
});

describe('搜尋與摘要', () => {
  const nodes = [
    item('a', { statement: 'Windows 路徑要用 pathlib', anchors: ['src/storage.py'] }),
    item('b', { statement: '別的主題', kind: 'user-stance', anchors: ['docs/README.md', 7 as unknown as string] }),
  ];

  it('空字串不篩選（null）；比對 statement、kind、anchor 且不分大小寫', () => {
    expect(matchNodes(nodes, '  ')).toBeNull();
    expect([...matchNodes(nodes, 'PATHLIB')!]).toEqual(['a']);
    expect([...matchNodes(nodes, 'user-stance')!]).toEqual(['b']);
    expect([...matchNodes(nodes, 'readme')!]).toEqual(['b']);
    expect(matchNodes(nodes, '不存在')!.size).toBe(0);
  });

  it('summarize 壓縮空白並以字元（不是 UTF-16 單位）截斷', () => {
    expect(summarize('a\n  b\tc')).toBe('a b c');
    expect(summarize('一二三四五', 3)).toBe('一二三…');
    expect(summarize('😀😀😀😀', 2)).toBe('😀😀…');
    expect(summarize(null)).toBe('');
  });
});

describe('視覺編碼', () => {
  it('節點半徑隨 surprisal 單調遞增且有上下限', () => {
    expect(nodeRadius(null)).toBe(5);
    expect(nodeRadius(-3)).toBe(5);
    expect(nodeRadius(1)).toBeLessThan(nodeRadius(4));
    expect(nodeRadius(1000)).toBeLessThanOrEqual(12);
    expect(nodeRadius(Number.NaN)).toBe(5);
  });

  it('線寬隨權重遞增、在 6 封頂', () => {
    expect(edgeWidth(1)).toBeLessThan(edgeWidth(3));
    expect(edgeWidth(6)).toBe(edgeWidth(60));
  });

  it('kind 對應 token，未知 kind 走 other', () => {
    expect(kindToken('project-fact')).toBe('--lv-graph-fact');
    expect(kindToken('belief-correction')).toBe('--lv-graph-correction');
    expect(kindToken('user-stance')).toBe('--lv-graph-stance');
    expect(kindToken('weird')).toBe('--lv-graph-other');
    expect(kindToken(null)).toBe('--lv-graph-other');
  });

  it('預跑 tick：小圖不預跑、大圖有上限', () => {
    expect(preTicks(10)).toBe(0);
    expect(preTicks(200)).toBeGreaterThan(0);
    expect(preTicks(1500)).toBe(300);
  });
});
