// 概念圖譜的 canvas 引擎：d3-force 排版、d3-zoom 縮放平移、d3-drag 拖曳節點。
// 與 Preact 解耦：元件只負責建立／銷毀與回呼。顏色全從 CSS 自訂屬性讀（深淺主題隨 data-theme）。
import { drag as d3drag } from 'd3-drag';
import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from 'd3-force';
import { pointer, select } from 'd3-selection';
import { zoom as d3zoom, zoomIdentity, type ZoomBehavior } from 'd3-zoom';

import {
  centerOn,
  edgeWidth,
  GRAPH_MAX_ZOOM,
  GRAPH_MIN_ZOOM,
  hitTest,
  kindToken,
  nodeRadius,
  preTicks,
  screenToWorld,
  summarize,
  worldToScreen,
  type ViewTransform,
} from './graph';
import type { ConceptEdge, ConceptItem } from './types';

interface SimNode extends SimulationNodeDatum {
  id: string;
  r: number;
  x: number;
  y: number;
  item: ConceptItem;
}

type SimLink = SimulationLinkDatum<SimNode> & { weight: number };

interface Palette {
  ink: string;
  soft: string;
  bg: string;
  card: string;
  line: string;
  accent: string;
  edge: string;
  font: string;
  kinds: Map<string, string>;
}

export interface GraphCallbacks {
  /** 點擊節點（含空白處＝null） */
  onSelect: (id: string | null) => void;
}

export interface GraphEngine {
  destroy: () => void;
  setSelected: (id: string | null) => void;
  setMatches: (ids: Set<string> | null) => void;
  /** 平移到該節點並選取視覺焦點（不觸發 onSelect） */
  focus: (id: string) => void;
  zoomBy: (factor: number) => void;
  resetView: () => void;
  unpinAll: () => void;
}

function readPalette(el: Element, kinds: Iterable<string>): Palette {
  const cs = getComputedStyle(el);
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  const map = new Map<string, string>();
  for (const kind of kinds) {
    const token = kindToken(kind);
    map.set(kind, v(token, '#888'));
  }
  return {
    ink: v('--ink', '#222'),
    soft: v('--ink-soft', '#555'),
    bg: v('--bg-sunken', '#eee'),
    card: v('--bg-card', '#fff'),
    line: v('--line-strong', '#999'),
    accent: v('--lv-focus', '#6e5c0f'),
    edge: v('--lv-graph-edge', '#888'),
    font: v('--font-sans', 'sans-serif'),
    kinds: map,
  };
}

export function createGraphEngine(
  canvas: HTMLCanvasElement,
  items: readonly ConceptItem[],
  edges: readonly ConceptEdge[],
  callbacks: GraphCallbacks,
): GraphEngine | null {
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;

  const nodes: SimNode[] = items.map((item) => ({ id: item.id, item, r: nodeRadius(item.surprisal), x: 0, y: 0 }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const links: SimLink[] = edges
    .filter((e) => byId.has(e.source) && byId.has(e.target))
    .map((e) => ({ source: e.source, target: e.target, weight: e.weight }));
  const kinds = [...new Set(items.map((i) => i.kind))];

  // 初始位置：依序排成螺旋，避免全部疊在原點而力導向算出退化結果
  nodes.forEach((n, i) => {
    const angle = i * 2.399963;
    const radius = 8 * Math.sqrt(i + 1);
    n.x = Math.cos(angle) * radius;
    n.y = Math.sin(angle) * radius;
  });

  const sim: Simulation<SimNode, SimLink> = forceSimulation<SimNode>(nodes)
    .force(
      'link',
      forceLink<SimNode, SimLink>(links)
        .id((d) => d.id)
        .distance((l) => 80 - Math.min(l.weight, 4) * 10)
        .strength((l) => Math.min(0.12 + 0.08 * l.weight, 0.7)),
    )
    .force('charge', forceManyBody<SimNode>().strength(-60).distanceMax(420))
    .force('x', forceX<SimNode>(0).strength(0.04))
    .force('y', forceY<SimNode>(0).strength(0.04))
    .force('collide', forceCollide<SimNode>((d) => d.r + 2))
    .alphaDecay(0.035)
    .stop();

  const reduceMotion = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

  let palette = readPalette(canvas, kinds);
  let t: ViewTransform = { x: 0, y: 0, k: 1 };
  let width = 0;
  let height = 0;
  let dpr = 1;
  let selectedId: string | null = null;
  let hoverId: string | null = null;
  let matches: Set<string> | null = null;
  let pointerX = 0;
  let pointerY = 0;
  let raf = 0;
  let docHidden = typeof document !== 'undefined' && document.hidden;
  let inView = true;
  let destroyed = false;

  const adjacent = new Map<string, Set<string>>();
  const link = (from: string, to: string) => {
    const set = adjacent.get(from) ?? new Set<string>();
    set.add(to);
    adjacent.set(from, set);
  };
  for (const e of edges) {
    if (!byId.has(e.source) || !byId.has(e.target)) continue;
    link(e.source, e.target);
    link(e.target, e.source);
  }

  function draw() {
    raf = 0;
    if (destroyed || width === 0 || height === 0) return;
    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx!.clearRect(0, 0, width, height);
    ctx!.save();
    ctx!.translate(t.x, t.y);
    ctx!.scale(t.k, t.k);

    const focusId = hoverId ?? selectedId;
    const focusSet = focusId ? adjacent.get(focusId) : undefined;
    const dimmedNode = (id: string) =>
      (matches !== null && !matches.has(id)) || (focusId !== null && id !== focusId && !focusSet?.has(id));

    ctx!.strokeStyle = palette.edge;
    for (const l of links) {
      const s = l.source as SimNode;
      const e = l.target as SimNode;
      const touching = focusId !== null && (s.id === focusId || e.id === focusId);
      const dim = focusId !== null ? !touching : matches !== null && !(matches.has(s.id) && matches.has(e.id));
      ctx!.globalAlpha = dim ? 0.1 : touching ? 0.9 : 0.5;
      ctx!.lineWidth = edgeWidth(l.weight);
      ctx!.beginPath();
      ctx!.moveTo(s.x, s.y);
      ctx!.lineTo(e.x, e.y);
      ctx!.stroke();
    }

    for (const n of nodes) {
      ctx!.globalAlpha = dimmedNode(n.id) ? 0.2 : 1;
      ctx!.beginPath();
      ctx!.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx!.fillStyle = palette.kinds.get(n.item.kind) ?? palette.soft;
      ctx!.fill();
      ctx!.lineWidth = 1.5;
      ctx!.strokeStyle = palette.card;
      ctx!.stroke();
      if (n.fx != null) {
        // 已固定的節點：外圈小點線
        ctx!.beginPath();
        ctx!.arc(n.x, n.y, n.r + 3, 0, Math.PI * 2);
        ctx!.lineWidth = 1;
        ctx!.strokeStyle = palette.ink;
        ctx!.setLineDash([2, 2]);
        ctx!.stroke();
        ctx!.setLineDash([]);
      }
      if (n.id === selectedId || n.id === hoverId) {
        ctx!.beginPath();
        ctx!.arc(n.x, n.y, n.r + 4, 0, Math.PI * 2);
        ctx!.lineWidth = n.id === selectedId ? 3 : 2;
        ctx!.strokeStyle = palette.accent;
        ctx!.stroke();
      }
    }
    ctx!.globalAlpha = 1;
    ctx!.restore();

    // 放大到一定程度才標文字；選取與 hover 的一定標
    ctx!.font = `12px ${palette.font}`;
    ctx!.textBaseline = 'middle';
    ctx!.fillStyle = palette.ink;
    for (const n of nodes) {
      const show = n.id === selectedId || n.id === hoverId || (t.k >= 2.2 && !dimmedNode(n.id));
      if (!show) continue;
      const [sx, sy] = worldToScreen(t, n.x, n.y);
      if (sx < -80 || sy < -20 || sx > width + 80 || sy > height + 20) continue;
      ctx!.fillText(summarize(n.item.statement, 14), sx + (n.r + 6) * t.k, sy);
    }

    const hovered = hoverId ? byId.get(hoverId) : undefined;
    if (hovered) drawTooltip(hovered);
  }

  function drawTooltip(n: SimNode) {
    const text = summarize(n.item.statement, 60) || n.id;
    const label = `${n.item.kind} · ${text}`;
    ctx!.font = `12px ${palette.font}`;
    const w = Math.min(ctx!.measureText(label).width + 16, width - 8);
    const h = 26;
    const x = Math.max(4, Math.min(pointerX + 14, width - w - 4));
    const y = Math.max(4, Math.min(pointerY + 14, height - h - 4));
    ctx!.fillStyle = palette.card;
    ctx!.strokeStyle = palette.line;
    ctx!.lineWidth = 1;
    ctx!.beginPath();
    ctx!.rect(x, y, w, h);
    ctx!.fill();
    ctx!.stroke();
    ctx!.fillStyle = palette.ink;
    ctx!.textBaseline = 'middle';
    ctx!.fillText(label, x + 8, y + h / 2, w - 16);
  }

  function requestDraw() {
    if (!raf && !destroyed) raf = requestAnimationFrame(draw);
  }

  const alphaLeft = () => sim.alpha() > sim.alphaMin();
  function syncRun() {
    if (destroyed) return;
    if (docHidden || !inView || reduceMotion) sim.stop();
    else if (alphaLeft()) sim.restart();
  }

  sim.on('tick', requestDraw);

  // 預跑：節點多時先算到接近穩定再顯示，避免一開始亂飛
  const pre = reduceMotion ? 300 : preTicks(nodes.length);
  for (let i = 0; i < pre; i++) sim.tick();

  const view = select(canvas);
  const zoom: ZoomBehavior<HTMLCanvasElement, unknown> = d3zoom<HTMLCanvasElement, unknown>()
    .scaleExtent([GRAPH_MIN_ZOOM, GRAPH_MAX_ZOOM])
    .filter((event: Event) => {
      const e = event as MouseEvent & { touches?: TouchList };
      if (e.type === 'dblclick') return false;
      if ((e.ctrlKey && e.type !== 'wheel') || e.button) return false;
      // 按在節點上的 mousedown／touchstart 留給拖曳節點
      if (e.type === 'mousedown' || e.type === 'touchstart') {
        const [px, py] = pointer(e, canvas);
        if (hitTest(nodes, t, px, py)) return false;
      }
      return true;
    })
    .on('zoom', (event: { transform: ViewTransform }) => {
      t = { x: event.transform.x, y: event.transform.y, k: event.transform.k };
      requestDraw();
    });
  view.call(zoom).on('dblclick.zoom', null);

  const dragBehavior = d3drag<HTMLCanvasElement, unknown, { x: number; y: number; node: SimNode } | undefined>()
    .container(canvas)
    .subject((event: { x: number; y: number }) => {
      const node = hitTest(nodes, t, event.x, event.y);
      if (!node) return undefined;
      const [sx, sy] = worldToScreen(t, node.x, node.y);
      return { x: sx, y: sy, node };
    })
    .on('start', (event) => {
      if (!event.subject) return;
      if (!event.active) sim.alphaTarget(0.3).restart();
      event.subject.node.fx = event.subject.node.x;
      event.subject.node.fy = event.subject.node.y;
    })
    .on('drag', (event) => {
      if (!event.subject) return;
      const [wx, wy] = screenToWorld(t, event.x, event.y);
      event.subject.node.fx = wx;
      event.subject.node.fy = wy;
    })
    .on('end', (event) => {
      // 放開後維持固定（fx／fy 不清），雙擊才解除
      if (!event.active) sim.alphaTarget(0);
      syncRun();
      requestDraw();
    });
  view.call(dragBehavior as never);

  function localPoint(e: MouseEvent): [number, number] {
    const [px, py] = pointer(e, canvas);
    return [px, py];
  }

  const onMove = (e: MouseEvent) => {
    const [px, py] = localPoint(e);
    pointerX = px;
    pointerY = py;
    const hit = hitTest(nodes, t, px, py);
    const next = hit?.id ?? null;
    canvas.classList.toggle('is-pointer', next !== null);
    if (next !== hoverId || next !== null) {
      hoverId = next;
      requestDraw();
    }
  };
  const onLeave = () => {
    if (hoverId !== null) {
      hoverId = null;
      canvas.classList.remove('is-pointer');
      requestDraw();
    }
  };
  const onClick = (e: MouseEvent) => {
    const [px, py] = localPoint(e);
    const hit = hitTest(nodes, t, px, py);
    callbacks.onSelect(hit?.id ?? null);
  };
  const onDblClick = (e: MouseEvent) => {
    const [px, py] = localPoint(e);
    const hit = hitTest(nodes, t, px, py);
    if (hit && hit.fx != null) {
      hit.fx = null;
      hit.fy = null;
      sim.alpha(Math.max(sim.alpha(), 0.2));
      syncRun();
      requestDraw();
    }
  };
  canvas.addEventListener('mousemove', onMove);
  canvas.addEventListener('mouseleave', onLeave);
  canvas.addEventListener('click', onClick);
  canvas.addEventListener('dblclick', onDblClick);

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const w = Math.round(rect.width);
    const h = Math.round(rect.height);
    if (w === 0 || h === 0 || (w === width && h === height)) return;
    const first = width === 0;
    const prevW = width;
    const prevH = height;
    width = w;
    height = h;
    dpr = (typeof devicePixelRatio === 'number' && devicePixelRatio) || 1;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    // 首次把原點放在畫布中央；之後縮放視窗維持中心不動
    const next = first ? { x: w / 2, y: h / 2, k: 1 } : { ...t, x: t.x + (w - prevW) / 2, y: t.y + (h - prevH) / 2 };
    view.call(zoom.transform, zoomIdentity.translate(next.x, next.y).scale(next.k));
    requestDraw();
  }
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
  ro?.observe(canvas);
  resize();

  const io =
    typeof IntersectionObserver === 'function'
      ? new IntersectionObserver((entries) => {
          inView = entries[entries.length - 1]?.isIntersecting ?? true;
          syncRun();
        })
      : null;
  io?.observe(canvas);
  const onVisibility = () => {
    docHidden = document.hidden;
    syncRun();
  };
  document.addEventListener('visibilitychange', onVisibility);

  // 主題切換（data-theme）或系統配色變更時重讀顏色
  const refreshPalette = () => {
    palette = readPalette(canvas, kinds);
    requestDraw();
  };
  const mo = typeof MutationObserver === 'function' ? new MutationObserver(refreshPalette) : null;
  mo?.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  syncRun();
  requestDraw();

  return {
    destroy() {
      destroyed = true;
      sim.stop();
      sim.on('tick', null);
      if (raf) cancelAnimationFrame(raf);
      ro?.disconnect();
      io?.disconnect();
      mo?.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
      canvas.removeEventListener('mousemove', onMove);
      canvas.removeEventListener('mouseleave', onLeave);
      canvas.removeEventListener('click', onClick);
      canvas.removeEventListener('dblclick', onDblClick);
      view.on('.zoom', null).on('.drag', null);
    },
    setSelected(id) {
      selectedId = id;
      requestDraw();
    },
    setMatches(ids) {
      matches = ids;
      requestDraw();
    },
    focus(id) {
      const n = byId.get(id);
      if (!n || width === 0) return;
      const target = centerOn(n.x, n.y, Math.max(t.k, 1.2), width, height);
      view.call(zoom.transform, zoomIdentity.translate(target.x, target.y).scale(target.k));
    },
    zoomBy(factor) {
      view.call(zoom.scaleBy, factor);
    },
    resetView() {
      view.call(zoom.transform, zoomIdentity.translate(width / 2, height / 2));
    },
    unpinAll() {
      for (const n of nodes) {
        n.fx = null;
        n.fy = null;
      }
      sim.alpha(Math.max(sim.alpha(), 0.3));
      syncRun();
      requestDraw();
    },
  };
}
