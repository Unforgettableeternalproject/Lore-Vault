// 記憶層瀏覽（T-84，唯讀）：concept 依 vault／scope／kind 篩選分頁（只顯示 statement 與 metadata），
// 以及收料概況（by_machine／by_vault）。concept 與 episode 只屬於 dev，其他 space 只顯示說明。
import { useEffect, useState } from 'preact/hooks';

import { ErrorState, Loading } from '../components/ui';
import { VaultPicker } from '../components/VaultPicker';
import { ALL, useApp, vaultName } from '../lib/context';
import { formatTime, isAbort } from '../lib/format';
import { formatAge, hoursSince } from '../lib/health';
import type { ConceptItem, ConceptPage, EpisodeGroup, EpisodeSummary } from '../lib/types';

const PAGE = 50;

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

export function Memory() {
  const { space } = useApp();
  return (
    <section class="lv-screen lv-screen--wide">
      <div class="lv-eyebrow">MEMORY LAYER · 唯讀 · {space.en}</div>
      <h1 class="lv-title">記憶層</h1>
      {space.id !== 'dev' ? (
        <div class="zone-state" role="status" data-testid="memory-dev-only">
          記憶層（concept 與收料 episode）只屬於 dev space：它們從 coding agent 的對話蒸餾而來、綁定 repo。{space.en}{' '}
          space 沒有記憶層，請切換到 DEV 檢視。
        </div>
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
  const [scopeInput, setScopeInput] = useState('');
  const [scope, setScope] = useState('');
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState<ConceptPage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => setCursors([null]), [vault, kind, scopeState, scope]);
  const cursor = cursors[cursors.length - 1] ?? null;

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    api
      .post<ConceptPage>(
        '/v1/concept_query',
        {
          space: space.id,
          vault,
          limit: PAGE,
          ...(kind ? { kind } : {}),
          ...(scopeState ? { scope_state: scopeState } : {}),
          ...(scope ? { scope } : {}),
          ...(cursor ? { cursor } : {}),
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
  }, [api, space.id, vault, kind, scopeState, scope, cursor, tick]);

  return (
    <div class="lv-memory-concepts">
      <div class="lv-filters lv-filters--tight">
        <VaultPicker />
        <label class="lv-filters__group">
          <span class="lv-filters__label">類型</span>
          <select class="lv-select" value={kind} aria-label="concept 類型" onChange={(e) => setKind((e.target as HTMLSelectElement).value)}>
            {KINDS.map((k) => (
              <option key={k.id} value={k.id}>
                {k.label}
              </option>
            ))}
          </select>
        </label>
        <div class="lv-chips" role="group" aria-label="scope 狀態">
          <span class="lv-filters__label">SCOPE</span>
          {SCOPE_STATES.map((s) => (
            <button
              key={s.id}
              type="button"
              class={'lv-chip lv-chip--mono' + (scopeState === s.id ? ' is-on' : '')}
              aria-pressed={scopeState === s.id}
              onClick={() => setScopeState(s.id)}
            >
              {s.label}
            </button>
          ))}
        </div>
        <form
          class="lv-inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            setScope(scopeInput.trim());
          }}
        >
          <input
            class="lv-input"
            aria-label="scope 名稱"
            placeholder="repo scope（不分大小寫）"
            value={scopeInput}
            onInput={(e) => setScopeInput((e.target as HTMLInputElement).value)}
          />
          <button type="submit" class="btn-outline btn-outline--sm">
            篩選
          </button>
        </form>
      </div>
      <p class="lv-muted lv-small">
        範圍：{vaultName({ vaults }, vault)}
        {scope ? ` · scope「${scope}」` : ''}
      </p>

      {error !== null && <ErrorState error={error} onRetry={() => setTick((t) => t + 1)} />}
      {loading && !page && <Loading />}
      {page && page.items.length === 0 && !loading && error === null && (
        <div class="zone-state lv-empty">沒有符合條件的 concept。</div>
      )}
      {page && page.items.length > 0 && (
        <ul class="lv-concepts" data-testid="concepts" aria-busy={loading}>
          {page.items.map((c) => (
            <ConceptRow key={c.id} concept={c} showVault={vault === ALL} />
          ))}
        </ul>
      )}
      {page && (cursors.length > 1 || page.next_cursor) && (
        <div class="lv-pager">
          <span>第 {cursors.length} 頁</span>
          <div class="lv-pager__btns">
            <button type="button" class="btn-terminal" disabled={cursors.length <= 1 || loading} onClick={() => setCursors((c) => c.slice(0, -1))}>
              ← 上一頁
            </button>
            <button
              type="button"
              class="btn-terminal"
              disabled={!page.next_cursor || loading}
              onClick={() => setCursors((c) => [...c, page.next_cursor])}
            >
              下一頁 →
            </button>
          </div>
        </div>
      )}
    </div>
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
  const anchors = anchorText(concept.anchors);
  const scopeText =
    concept.scope_state === 'global' ? '跨專案' : concept.scope_state === 'missing' ? '缺 scope' : (concept.scope ?? '—');
  return (
    <li class="lv-concept" data-kind={concept.kind}>
      <div class="lv-concept__main">
        <div class="lv-concept__statement">{concept.statement}</div>
        <div class="lv-concept__meta">
          <span class="lv-code-tag">{concept.kind}</span>
          <span class={'lv-mono' + (concept.scope_state === 'missing' ? ' lv-text-warn' : '')}>scope：{scopeText}</span>
          {showVault && <span class="lv-mono lv-muted">{concept.vault}</span>}
          {anchors.slice(0, 3).map((a) => (
            <span key={a} class="lv-mono lv-muted">
              {a}
            </span>
          ))}
          {anchors.length > 3 && <span class="lv-muted lv-small">+{anchors.length - 3} 個錨點</span>}
        </div>
      </div>
      <div class="lv-concept__side">
        <div class="lv-mono" title="surprisal">
          {typeof concept.surprisal === 'number' ? concept.surprisal.toFixed(2) : '—'}
        </div>
        <div class="lv-small lv-muted">{concept.usability_verdict ?? '未校準'}</div>
        <div class="lv-small lv-muted">{formatTime(concept.updated)}</div>
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
        <p class="lv-muted lv-small">沒有資料。</p>
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
