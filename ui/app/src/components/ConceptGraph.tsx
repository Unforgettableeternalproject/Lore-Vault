// 概念關聯圖：canvas 畫圖（d3-force 排版），旁邊的側欄是無障礙的文字替代——
// 節點清單（可搜尋、可用鍵盤選取）與選取節點的完整內容、相鄰節點。canvas 本身對輔助工具是空白的。
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';

import { createGraphEngine, type GraphEngine } from '../lib/graphEngine';
import { anchorStrings, kindToken, matchNodes, neighborMap, scopeLabel, sortByDegree, summarize } from '../lib/graph';
import type { ConceptGraphData } from '../lib/types';
import { Badge, Banner } from './ui';

/** 側欄清單最多一次列出的節點數；更多時請用搜尋縮小 */
export const LIST_LIMIT = 300;

export function ConceptGraph({ data, maxNodes }: { data: ConceptGraphData; maxNodes: number }) {
  const { nodes, edges, total, truncated } = data;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<GraphEngine | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState('');

  const neighbors = useMemo(() => neighborMap(edges), [edges]);
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const sorted = useMemo(() => sortByDegree(nodes, neighbors), [nodes, neighbors]);
  const matches = useMemo(() => matchNodes(nodes, query), [nodes, query]);
  const kinds = useMemo(() => [...new Set(nodes.map((n) => n.kind))].sort(), [nodes]);
  const listed = useMemo(() => (matches ? sorted.filter((n) => matches.has(n.id)) : sorted), [sorted, matches]);
  const selected = selectedId ? (byId.get(selectedId) ?? null) : null;
  const near = selected ? (neighbors.get(selected.id) ?? []) : [];

  // 引擎建立時要帶入目前的選取與搜尋（資料換了會重建、重新排版）；
  // 選取的 id 不在新資料裡時 selected 為 null，不需另外重設
  const selectedRef = useRef(selectedId);
  const matchesRef = useRef(matches);
  selectedRef.current = selectedId;
  matchesRef.current = matches;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const engine = createGraphEngine(canvas, nodes, edges, { onSelect: setSelectedId });
    engine?.setSelected(selectedRef.current);
    engine?.setMatches(matchesRef.current);
    engineRef.current = engine;
    return () => {
      engine?.destroy();
      engineRef.current = null;
    };
  }, [nodes, edges]);

  useEffect(() => engineRef.current?.setSelected(selectedId), [selectedId]);
  useEffect(() => engineRef.current?.setMatches(matches), [matches]);

  const choose = (id: string) => {
    setSelectedId(id);
    engineRef.current?.focus(id);
  };

  return (
    <div class="lv-graph" data-testid="concept-graph">
      <p class="lv-small lv-graph__count" data-testid="graph-count">
        <span class="lv-mono">
          {nodes.length} / {total}
        </span>{' '}
        個 concept · {edges.length} 條關聯
      </p>
      {truncated && (
        <Banner tone="info" label="TRUNCATED" testId="graph-truncated">
          concept 數量超過 {maxNodes}，只畫關聯最多的 {nodes.length} 個（共 {total} 個）。縮小類型或日期範圍可看到更完整的圖。
        </Banner>
      )}
      <div class="lv-graph__body">
        <div class="lv-graph__stage">
          <canvas
            ref={canvasRef}
            class="lv-graph__canvas"
            role="img"
            aria-label={`概念關聯圖：${nodes.length} 個節點、${edges.length} 條關聯。請使用右側的節點清單瀏覽內容。`}
            data-testid="graph-canvas"
          />
          <div class="lv-graph__tools" role="group" aria-label="圖譜視圖">
            <button type="button" class="btn-outline" aria-label="放大" onClick={() => engineRef.current?.zoomBy(1.4)}>
              ＋
            </button>
            <button type="button" class="btn-outline" aria-label="縮小" onClick={() => engineRef.current?.zoomBy(1 / 1.4)}>
              －
            </button>
            <button type="button" class="btn-outline" onClick={() => engineRef.current?.resetView()}>
              重設視圖
            </button>
            <button type="button" class="btn-outline" onClick={() => engineRef.current?.unpinAll()}>
              解除固定
            </button>
          </div>
          <Legend kinds={kinds} />
          <p class="lv-muted lv-small lv-graph__hint">
            滾輪縮放、拖曳平移；拖曳節點可固定位置，雙擊節點解除。節點越大 surprisal 越高，連線越粗共用的錨點越多。
          </p>
        </div>

        <aside class="lv-graph__side" aria-label="節點清單與詳情">
          {selected ? (
            <section class="lv-graph__detail" data-testid="graph-detail" aria-label="選取的 concept">
              <div class="lv-graph__detail-head">
                <div class="lv-side-block__label">已選取</div>
                <button type="button" class="btn-terminal" onClick={() => setSelectedId(null)}>
                  取消選取
                </button>
              </div>
              <p class="lv-concept__statement" data-testid="graph-detail-statement">
                {selected.statement}
              </p>
              <div class="lv-concept__meta">
                {selected.kind && (
                  <Badge tone="kind" label="類型">
                    {selected.kind}
                  </Badge>
                )}
                <Badge tone={selected.scope_state === 'missing' ? 'warn' : 'tag'} label="scope">
                  scope：{scopeLabel(selected.scope_state, selected.scope)}
                </Badge>
                <Badge tone="score" label="surprisal">
                  {typeof selected.surprisal === 'number' ? selected.surprisal.toFixed(2) : '—'}
                </Badge>
              </div>
              <div class="lv-filters__label">錨點（{anchorStrings(selected.anchors).length}）</div>
              <ul class="lv-graph__anchors" data-testid="graph-detail-anchors">
                {anchorStrings(selected.anchors).map((a) => (
                  <li key={a} class="lv-mono">
                    {a}
                  </li>
                ))}
              </ul>
              <div class="lv-filters__label">相鄰節點（{near.length}）</div>
              {near.length === 0 ? (
                <p class="lv-muted lv-small">沒有共用錨點的 concept（孤立節點）。</p>
              ) : (
                <ul class="lv-graph__list" data-testid="graph-neighbors">
                  {near.map((nb) => {
                    const other = byId.get(nb.id);
                    return (
                      <li key={nb.id}>
                        <button type="button" class="lv-graph__item" onClick={() => choose(nb.id)}>
                          <span class="lv-graph__item-text">{summarize(other?.statement, 48) || nb.id}</span>
                          <span class="lv-graph__item-meta lv-mono">
                            共用 {nb.weight} · {nb.via.join('、')}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          ) : (
            <p class="lv-muted lv-small" data-testid="graph-detail-empty">
              點選圖上的節點，或從下方清單選一個 concept 查看內容與相鄰節點。
            </p>
          )}

          <section class="lv-graph__nodes" aria-label="節點清單">
            <label class="lv-filters__group">
              <span class="lv-filters__label">搜尋節點</span>
              <input
                class="lv-input"
                type="search"
                value={query}
                aria-label="搜尋節點"
                placeholder="statement、類型或錨點"
                onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
              />
            </label>
            <p class="lv-muted lv-small" data-testid="graph-list-count">
              {matches ? `符合 ${listed.length} 個` : `共 ${nodes.length} 個`}（相鄰節點多者在前）
            </p>
            <ul class="lv-graph__list" data-testid="graph-nodes">
              {listed.slice(0, LIST_LIMIT).map((n) => (
                <li key={n.id}>
                  <button
                    type="button"
                    class="lv-graph__item"
                    aria-pressed={n.id === selectedId}
                    data-kind={n.kind}
                    onClick={() => choose(n.id)}
                  >
                    <span class="lv-graph__item-text">{summarize(n.statement, 48) || n.id}</span>
                    <span class="lv-graph__item-meta lv-mono">
                      {n.kind} · {neighbors.get(n.id)?.length ?? 0} 條關聯
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            {listed.length > LIST_LIMIT && (
              <p class="lv-muted lv-small">只列出前 {LIST_LIMIT} 個，其餘請用搜尋縮小。</p>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}

function Legend({ kinds }: { kinds: string[] }) {
  return (
    <ul class="lv-graph__legend" aria-label="圖例" data-testid="graph-legend">
      {kinds.map((k) => (
        <li key={k} class="lv-graph__legend-item">
          <span class="lv-graph__swatch" data-token={kindToken(k)} aria-hidden="true" />
          <span class="lv-mono">{k || '（未分類）'}</span>
        </li>
      ))}
    </ul>
  );
}
