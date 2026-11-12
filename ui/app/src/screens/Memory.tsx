// 記憶層瀏覽（T-84，唯讀）：concept 依 vault／scope／kind 篩選分頁（只顯示 statement 與 metadata），
// 以及收料概況（by_machine／by_vault）。concept 與 episode 只屬於 dev，其他 space 只顯示說明。
import type { ComponentType } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import { ChipGroup, FilterGroup, FilterPanel, TextFilter } from '../components/Filters';
import { DateRange, DEFAULT_PAGE_SIZE, Pager, rangeParams, type DateRangeValue } from '../components/Pager';
import { Badge, EmptyState, ErrorState, Loading } from '../components/ui';
import { VaultPicker } from '../components/VaultPicker';
import { ALL, useApp, vaultName } from '../lib/context';
import { formatTime, isAbort } from '../lib/format';
import { GRAPH_MAX_NODES, scopeLabel } from '../lib/graph';
import { routePath } from '../lib/router';
import { formatAge, hoursSince } from '../lib/health';
import type { ConceptGraphData, ConceptItem, ConceptScopeCount, ConceptPage, EpisodeGroup, EpisodeSummary } from '../lib/types';

const KINDS = [
  { id: '', label: '全部類型' },
  { id: 'project-fact', label: 'project-fact' },
  { id: 'belief-correction', label: 'belief-correction' },
  { id: 'user-stance', label: 'user-stance' },
] as const;

const SCOPE_STATES = [
  { id: '', label: '全部' },
  { id: 'repo', label: 'repo' },
  { id: 'global', label: '跨專案' },
  { id: 'missing', label: '缺 scope' },
] as const;

const VIEWS = [
  { id: 'list', label: '列表' },
  { id: 'graph', label: '圖譜' },
] as const;
type View = (typeof VIEWS)[number]['id'];

export function Memory() {
  const { space, switchSpace } = useApp();
  return (
    <section class="lv-screen lv-screen--wide">
      <div class="lv-eyebrow">MEMORY LAYER · 唯讀 · {space.en}</div>
      <h1 class="lv-title">記憶層</h1>
      {space.id !== 'dev' ? (
        <EmptyState
          testId="memory-dev-only"
          title={`${space.en} space 沒有記憶層`}
          action={
            <button type="button" class="btn-outline btn-outline--gold" onClick={() => switchSpace('dev', routePath('memory'))}>
              切換到 DEV 檢視
            </button>
          }
        >
          記憶層（concept 與收料 episode）只屬於 dev space：它們從 coding agent 的對話蒸餾而來、綁定 repo。
        </EmptyState>
      ) : (
        <>
          <p class="lv-section__desc">
            從 episode 蒸餾出模型容易犯錯的地方；agent 編輯相符錨點的檔案時自動注入。這裡只列 statement 與 metadata，不含引用對話的欄位。
          </p>
          <div class="lv-memory-grid">
            <ConceptList />
            <EpisodePanel />
          </div>
        </>
      )}
    </section>
  );
}

function ConceptList() {
  const { api, space, vault, vaults } = useApp();
  const [kind, setKind] = useState('');
  const [scopeState, setScopeState] = useState('');
  const [scope, setScope] = useState('');
  const [range, setRange] = useState<DateRangeValue>({ from: '', to: '' });
  const [pageNo, setPageNo] = useState(1);
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);
  const [page, setPage] = useState<ConceptPage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [view, setView] = useState<View>('list');
  const [graph, setGraph] = useState<ConceptGraphData | null>(null);
  const [graphError, setGraphError] = useState<unknown>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const [scopes, setScopes] = useState<ConceptScopeCount[]>([]);

  // 篩選或每頁筆數變了回第一頁
  useEffect(() => setPageNo(1), [vault, kind, scopeState, scope, range.from, range.to, pageSize]);

  useEffect(() => {
    if (view !== 'list') return;
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    api
      .post<ConceptPage>(
        '/v1/concept_query',
        {
          space: space.id,
          vault,
          limit: pageSize,
          offset: (pageNo - 1) * pageSize,
          with_total: true,
          ...(kind ? { kind } : {}),
          ...(scopeState ? { scope_state: scopeState } : {}),
          ...(scope ? { scope } : {}),
          ...rangeParams(range),
        },
        ctrl.signal,
      )
      .then(({ data }) => {
        setPage(data);
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, view, space.id, vault, kind, scopeState, scope, range.from, range.to, pageNo, pageSize, tick]);

  // scope 下拉選單的選項（依目前 vault、類型、scope 狀態）
  useEffect(() => {
    if (view !== 'graph') return;
    const ctrl = new AbortController();
    api
      .post<{ scopes: ConceptScopeCount[] }>(
        '/v1/concept_scopes',
        { space: space.id, vault, ...(kind ? { kind } : {}), ...(scopeState ? { scope_state: scopeState } : {}) },
        ctrl.signal,
      )
      .then(({ data }) => setScopes(data.scopes))
      .catch(() => {
        // 選項取不到不阻擋圖譜：下拉退回只有「全部」與目前 scope
        if (!ctrl.signal.aborted) setScopes([]);
      });
    return () => ctrl.abort();
  }, [api, view, space.id, vault, kind, scopeState, tick]);

  // 圖譜：一次只畫一個 scope；scope 未指定（全部）不查詢，只顯示提示
  useEffect(() => {
    if (view !== 'graph' || !scope) {
      setGraph(null);
      setGraphError(null);
      setGraphLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setGraphLoading(true);
    setGraphError(null);
    api
      .post<ConceptGraphData>(
        '/v1/concept_graph',
        {
          space: space.id,
          vault,
          scope,
          max_nodes: GRAPH_MAX_NODES,
          ...(kind ? { kind } : {}),
          ...(scopeState ? { scope_state: scopeState } : {}),
          ...rangeParams(range),
        },
        ctrl.signal,
      )
      .then(({ data }) => {
        setGraph(data);
        setGraphLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setGraphError(err);
        setGraphLoading(false);
      });
    return () => ctrl.abort();
  }, [api, view, space.id, vault, kind, scopeState, scope, range.from, range.to, tick]);

  const total = page?.total ?? page?.items.length ?? 0;

  return (
    <div class="lv-memory-concepts">
      <FilterPanel
        summary={
          <>
            範圍：{vaultName({ vaults }, vault)}
            {scope ? ` · scope「${scope}」` : ''}
            {range.from || range.to ? ` · ${range.from || '最早'}～${range.to || '今天'}` : ''}
          </>
        }
      >
        <div class="lv-filter-panel__grid" data-testid="filter-groups">
          <FilterGroup title="檢視" testId="filter-group-view">
            <ChipGroup label="檢視" groupLabel="檢視方式" options={VIEWS} value={view} onChange={setView} />
          </FilterGroup>
          <FilterGroup title="範圍" testId="filter-group-scope">
            <VaultPicker />
            <DateRange value={range} onChange={setRange} />
          </FilterGroup>
          <FilterGroup title="分類" testId="filter-group-category">
            <label class="lv-filters__group">
              <span class="lv-filters__label">類型</span>
              <select class="lv-input lv-select" value={kind} aria-label="concept 類型" onChange={(e) => setKind((e.target as HTMLSelectElement).value)}>
                {KINDS.map((k) => (
                  <option key={k.id} value={k.id}>
                    {k.label}
                  </option>
                ))}
              </select>
            </label>
            <ChipGroup label="SCOPE 狀態" groupLabel="scope 狀態" options={SCOPE_STATES} value={scopeState} onChange={setScopeState} />
            {view === 'graph' ? (
              <ScopeSelect scopes={scopes} value={scope} onChange={setScope} />
            ) : (
              <TextFilter value={scope} onApply={setScope} inputLabel="scope 名稱" placeholder="repo scope（不分大小寫）" submitLabel="套用 scope" />
            )}
          </FilterGroup>
        </div>
      </FilterPanel>

      {view === 'graph' ? (
        <GraphPane
          scope={scope}
          graph={graph}
          error={graphError}
          loading={graphLoading}
          onRetry={() => setTick((t) => t + 1)}
        />
      ) : (
        <>
        {error !== null && <ErrorState error={error} onRetry={() => setTick((t) => t + 1)} />}
        {loading && !page && <Loading />}
        {page && page.items.length === 0 && !loading && error === null && (
          <EmptyState title="沒有符合條件的 concept">調整類型、scope、日期或 vault 篩選再試一次。</EmptyState>
        )}
        {page && page.items.length > 0 && (
          <ul class="lv-concepts" data-testid="concepts" aria-busy={loading}>
            {page.items.map((c) => (
              <ConceptRow key={c.id} concept={c} showVault={vault === ALL} />
            ))}
          </ul>
        )}
        {page && total > 0 && (
          <Pager
            page={pageNo}
            pageSize={pageSize}
            total={total}
            loading={loading}
            unit="則"
            label="concept 分頁"
            onPage={setPageNo}
            onPageSize={setPageSize}
          />
        )}
        </>
      )}
    </div>
  );
}

function ScopeSelect({
  scopes,
  value,
  onChange,
}: {
  scopes: ConceptScopeCount[];
  value: string;
  onChange: (scope: string) => void;
}) {
  const named = scopes.filter((s): s is { scope: string; count: number } => s.scope !== null);
  const generic = scopes.find((s) => s.scope === null);
  // 目前的 scope（例如從列表帶過來的輸入）不在選項裡時補一個，避免下拉顯示成「全部」
  const missing = value && !named.some((s) => s.scope.toLowerCase() === value.toLowerCase());
  return (
    <label class="lv-filters__group">
      <span class="lv-filters__label">SCOPE 名稱</span>
      <select class="lv-input lv-select" value={value} aria-label="scope 名稱" onChange={(e) => onChange((e.target as HTMLSelectElement).value)}>
        <option value="">全部</option>
        {missing && <option value={value}>{value}</option>}
        {named.map((s) => (
          <option key={s.scope} value={s.scope}>
            {s.scope}（{s.count}）
          </option>
        ))}
        {generic && (
          <option value="" disabled>
            通用・無 scope（{generic.count}，不能畫圖）
          </option>
        )}
      </select>
    </label>
  );
}

function GraphPane({
  scope,
  graph,
  error,
  loading,
  onRetry,
}: {
  scope: string;
  graph: ConceptGraphData | null;
  error: unknown;
  loading: boolean;
  onRetry: () => void;
}) {
  if (!scope) {
    return (
      <EmptyState testId="graph-need-scope" title="請先選擇 scope">
        圖譜一次只畫一個 scope 的 concept。請在上方 scope 下拉選單選一個 scope；選「全部」時不顯示圖譜。
      </EmptyState>
    );
  }
  return <GraphBody graph={graph} error={error} loading={loading} onRetry={onRetry} />;
}

type GraphComponent = ComponentType<{ data: ConceptGraphData; maxNodes: number }>;

function GraphBody({
  graph,
  error,
  loading,
  onRetry,
}: {
  graph: ConceptGraphData | null;
  error: unknown;
  loading: boolean;
  onRetry: () => void;
}) {
  // 圖譜元件連同 d3 在切到圖譜時才載入，不進主 bundle
  const [Graph, setGraph] = useState<GraphComponent | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    import('../components/ConceptGraph')
      .then((m) => alive && setGraph(() => m.ConceptGraph))
      .catch(() => alive && setLoadFailed(true));
    return () => {
      alive = false;
    };
  }, []);
  return (
    <>
      {error !== null && <ErrorState error={error} onRetry={onRetry} />}
      {loadFailed && <ErrorState error={new Error('圖譜元件載入失敗，請重新整理頁面')} />}
      {(loading || (!Graph && !loadFailed)) && !graph && <Loading />}
      {graph && graph.nodes.length === 0 && !loading && error === null && (
        <EmptyState title="這個 scope 沒有符合條件的 concept">調整類型、日期或 vault 篩選再試一次。</EmptyState>
      )}
      {graph && graph.nodes.length > 0 && Graph && <Graph data={graph} maxNodes={GRAPH_MAX_NODES} />}
    </>
  );
}

function anchorText(anchors: unknown): string[] {
  if (!Array.isArray(anchors)) return [];
  return anchors.map((a) => {
    if (typeof a === 'string') return a;
    if (a && typeof a === 'object') {
      const o = a as Record<string, unknown>;
      const file = typeof o.file === 'string' ? o.file : typeof o.path === 'string' ? o.path : '';
      const symbol = typeof o.symbol === 'string' ? o.symbol : '';
      if (file || symbol) return symbol ? `${file}#${symbol}` : file;
    }
    return JSON.stringify(a);
  });
}

function ConceptRow({ concept, showVault }: { concept: ConceptItem; showVault: boolean }) {
  const { vaults } = useApp();
  const anchors = anchorText(concept.anchors);
  const scopeText = scopeLabel(concept.scope_state, concept.scope);
  return (
    <li class="lv-concept" data-kind={concept.kind}>
      <div class="lv-concept__main">
        <div class="lv-concept__statement">{concept.statement}</div>
        <div class="lv-concept__meta">
          {concept.kind && (
            <Badge tone="kind" label="類型">
              {concept.kind}
            </Badge>
          )}
          <Badge tone={concept.scope_state === 'missing' ? 'warn' : 'tag'} label="scope">
            scope：{scopeText}
          </Badge>
          {showVault && (
            <Badge tone="vault" label="vault" title={concept.vault}>
              {vaultName({ vaults }, concept.vault)}
            </Badge>
          )}
          {anchors.slice(0, 3).map((a) => (
            <Badge key={a} tone="anchor" label="錨點">
              {a}
            </Badge>
          ))}
          {anchors.length > 3 && <span class="lv-muted lv-small">+{anchors.length - 3} 個錨點</span>}
        </div>
      </div>
      <div class="lv-concept__side">
        <Badge tone="score" label="surprisal">
          {typeof concept.surprisal === 'number' ? concept.surprisal.toFixed(2) : '—'}
        </Badge>
        <Badge
          tone={concept.usability_verdict ? 'tag' : 'plain'}
          label="可用性"
          title="注入實驗判定，非 surprisal 校準"
        >
          {concept.usability_verdict ?? '未驗證'}
        </Badge>
        <Badge tone="time" label="更新">
          {formatTime(concept.updated)}
        </Badge>
      </div>
    </li>
  );
}

function EpisodePanel() {
  const { api, space, vault } = useApp();
  const [data, setData] = useState<EpisodeSummary | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const ctrl = new AbortController();
    setError(null);
    api
      .post<EpisodeSummary>('/v1/episode_summary', { space: space.id, vault }, ctrl.signal)
      .then(({ data }) => setData(data))
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
      });
    return () => ctrl.abort();
  }, [api, space.id, vault, tick]);

  return (
    <aside class="lv-side-block lv-memory-episodes" aria-label="收料概況">
      <div class="lv-side-block__label">收料概況 · EPISODES</div>
      {error !== null && <ErrorState error={error} onRetry={() => setTick((t) => t + 1)} />}
      {!data && error === null && <Loading />}
      {data && (
        <>
          <p class="lv-small" data-testid="episode-total">
            共 {data.total} 輪 · 最近收料 {data.last_recorded ? formatTime(data.last_recorded) : '—'}
          </p>
          <GroupList title="依機器" groups={data.by_machine} field="machine" />
          <GroupList title="依 vault" groups={data.by_vault} field="vault" />
        </>
      )}
    </aside>
  );
}

function GroupList({ title, groups, field }: { title: string; groups: EpisodeGroup[]; field: 'machine' | 'vault' }) {
  return (
    <div class="lv-episode-group">
      <div class="lv-filters__label">{title}</div>
      {groups.length === 0 ? (
        <EmptyState size="sm" title="沒有資料" />
      ) : (
        <ul class="lv-machines">
          {groups.map((g) => (
            <li key={g[field] ?? '(null)'} class="lv-machine">
              <span class="lv-machine__name lv-mono">{g[field] ?? '（未記錄）'}</span>
              <span class="lv-machine__n lv-mono">{g.episodes}</span>
              <span class="lv-machine__last">{formatAge(hoursSince(g.last_recorded))}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
