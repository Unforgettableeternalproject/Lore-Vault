// 概念圖譜的純函式：視覺編碼（顏色／大小／粗細）、座標換算、hit-test、鄰居與搜尋。
// 不碰 DOM 與 canvas，方便在 node 環境測試。
import type { ConceptEdge, ConceptItem } from './types';

/** d3-zoom 的視圖變換：screen = world * k + (x, y) */
export interface ViewTransform {
  x: number;
  y: number;
  k: number;
}

/** 已排版的節點（world 座標）與半徑 */
export interface Placed {
  id: string;
  x: number;
  y: number;
  r: number;
}

export interface Neighbor {
  id: string;
  weight: number;
  via: string[];
}

/** kind → CSS 自訂屬性（顏色隨主題；定義在 app.css 的 --lv-graph-*） */
const KIND_TOKENS: Record<string, string> = {
  'project-fact': '--lv-graph-fact',
  'belief-correction': '--lv-graph-correction',
  'user-stance': '--lv-graph-stance',
};
export const OTHER_KIND_TOKEN = '--lv-graph-other';

export function kindToken(kind: string | null | undefined): string {
  return (kind && KIND_TOKENS[kind]) || OTHER_KIND_TOKEN;
}

export const GRAPH_MIN_ZOOM = 0.2;
export const GRAPH_MAX_ZOOM = 8;
export const GRAPH_MAX_NODES = 500;

/** 節點半徑：surprisal 越高越大，5～12px；缺值或負值取最小。 */
export function nodeRadius(surprisal: number | null | undefined): number {
  const s = typeof surprisal === 'number' && Number.isFinite(surprisal) ? Math.max(0, surprisal) : 0;
  return 5 + 7 * (1 - Math.exp(-s / 3));
}

/** 邊的線寬：共用的 anchor 越多越粗，1.3～3.8px。 */
export function edgeWidth(weight: number): number {
  return 0.8 + Math.min(Math.max(weight, 1), 6) * 0.5;
}

export function screenToWorld(t: ViewTransform, sx: number, sy: number): [number, number] {
  return [(sx - t.x) / t.k, (sy - t.y) / t.k];
}

export function worldToScreen(t: ViewTransform, wx: number, wy: number): [number, number] {
  return [wx * t.k + t.x, wy * t.k + t.y];
}

/** 讓 world 座標 (wx, wy) 落在 (w, h) 畫布正中央的變換（保持縮放 k）。 */
export function centerOn(wx: number, wy: number, k: number, w: number, h: number): ViewTransform {
  return { x: w / 2 - wx * k, y: h / 2 - wy * k, k };
}

/**
 * 找 screen 座標 (sx, sy) 下的節點；重疊時取較晚繪製（陣列較後）者。
 * 命中半徑固定多給 `slop` 個螢幕像素，縮小時小點仍點得到。
 */
export function hitTest<T extends Placed>(
  nodes: readonly T[],
  t: ViewTransform,
  sx: number,
  sy: number,
  slop = 3,
): T | null {
  const [wx, wy] = screenToWorld(t, sx, sy);
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i]!;
    const reach = n.r + slop / t.k;
    const dx = wx - n.x;
    const dy = wy - n.y;
    if (dx * dx + dy * dy <= reach * reach) return n;
  }
  return null;
}

/** 每個節點的相鄰節點（權重大者在前，同權重依 id）。 */
export function neighborMap(edges: readonly ConceptEdge[]): Map<string, Neighbor[]> {
  const map = new Map<string, Neighbor[]>();
  const add = (from: string, to: string, e: ConceptEdge) => {
    const list = map.get(from) ?? [];
    list.push({ id: to, weight: e.weight, via: e.via });
    map.set(from, list);
  };
  for (const e of edges) {
    add(e.source, e.target, e);
    add(e.target, e.source, e);
  }
  for (const list of map.values()) list.sort((a, b) => b.weight - a.weight || a.id.localeCompare(b.id));
  return map;
}

/** 節點清單的排序：相鄰節點數多者在前，同數依 id（穩定、與截斷時的取捨一致）。 */
export function sortByDegree(nodes: readonly ConceptItem[], neighbors: Map<string, Neighbor[]>): ConceptItem[] {
  const deg = (n: ConceptItem) => neighbors.get(n.id)?.length ?? 0;
  return [...nodes].sort((a, b) => deg(b) - deg(a) || a.id.localeCompare(b.id));
}

function anchorStrings(anchors: unknown): string[] {
  return Array.isArray(anchors) ? anchors.filter((a): a is string => typeof a === 'string') : [];
}

/** 搜尋：statement、kind、anchor 含關鍵字（不分大小寫）。空字串回 null＝不篩選。 */
export function matchNodes(nodes: readonly ConceptItem[], query: string): Set<string> | null {
  const q = query.trim().toLowerCase();
  if (!q) return null;
  const hit = new Set<string>();
  for (const n of nodes) {
    const hay = [n.statement, n.kind, ...anchorStrings(n.anchors)].join('\n').toLowerCase();
    if (hay.includes(q)) hit.add(n.id);
  }
  return hit;
}

/** 單行摘要：空白壓成一格，超過 max 個字以「…」截斷。 */
export function summarize(text: string | null | undefined, max = 60): string {
  const flat = (text ?? '').replace(/\s+/g, ' ').trim();
  const chars = Array.from(flat);
  return chars.length > max ? `${chars.slice(0, max).join('')}…` : flat;
}

/** 排版先預跑的 tick 數：小圖直接動畫，節點多時先跑到接近穩定再顯示。 */
export function preTicks(nodeCount: number): number {
  if (nodeCount < 40) return 0;
  return Math.min(300, Math.round(120 + nodeCount * 0.4));
}

export function scopeLabel(scopeState: string, scope: string | null): string {
  return scopeState === 'global' ? '跨專案' : scopeState === 'missing' ? '缺 scope' : (scope ?? '—');
}

export { anchorStrings };
