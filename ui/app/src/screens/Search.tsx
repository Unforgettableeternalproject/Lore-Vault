// 檢索頁：兩個分頁——檢索（recall）與問答（ask，見 Ask.tsx）。分頁狀態反映在網址 `?mode=ask`。
// 檢索分頁：查詢框、vault 篩選（VaultPicker，與側欄共用）、結果列（類型、摘要來源、vault、更新、分數），
// 降級／截斷／不支援的種類／缺向量都要明確呈現。「載入其餘結果」＝提高 budget（必要時 limit）重查。
import { useEffect, useRef, useState } from 'preact/hooks';

import { Ask } from './Ask';

import { Badge, Banner, EmptyState, ErrorState, Loading, SourceTag } from '../components/ui';
import { VaultPicker } from '../components/VaultPicker';
import { useApp, vaultName } from '../lib/context';
import { authorLabel, describeDegradedReason, formatTime, isAbort, locatorLabel } from '../lib/format';
import { routePath } from '../lib/router';
import type { RecallItem, RecallResult } from '../lib/types';

// limit 的預設與上限、budget 的預設取 session limits（recall_*）；
// budget 服務端沒有硬上限，「載入其餘結果」最多放大到預設的 32 倍，避免一次拉太多
const BUDGET_GROWTH_CAP = 32;

interface Params {
  query: string;
  vault: string;
  limit: number;
  budget: number;
}

/** 快捷鍵 `/` 聚焦的查詢框 id */
export const SEARCH_INPUT_ID = 'lv-search-input';

const KIND_LABEL: Record<string, string> = { note: '筆記', chunk: '文件段落', concept: '記憶概念' };

export type SearchMode = 'recall' | 'ask';

const TABS: { id: SearchMode; label: string }[] = [
  { id: 'recall', label: '檢索' },
  { id: 'ask', label: '問答' },
];

function parseMode(value: string | null | undefined): SearchMode {
  return value === 'ask' ? 'ask' : 'recall';
}

export function Search({ initialQuery, initialMode }: { initialQuery: string; initialMode?: string | null }) {
  const { space, navigate } = useApp();
  const [mode, setMode] = useState<SearchMode>(parseMode(initialMode));
  // 各分頁最後送出的查詢：切換分頁時寫回網址，回來時不遺失
  const lastQuery = useRef<Record<SearchMode, string>>({
    recall: parseMode(initialMode) === 'recall' ? initialQuery.trim() : '',
    ask: parseMode(initialMode) === 'ask' ? initialQuery.trim() : '',
  });
  const tabRefs = useRef<Record<SearchMode, HTMLButtonElement | null>>({ recall: null, ask: null });

  // 網址變了（上一頁、外部連結帶 ?q=）：跟著切分頁
  useEffect(() => {
    const next = parseMode(initialMode);
    if (initialQuery.trim()) lastQuery.current[next] = initialQuery.trim();
    setMode(next);
  }, [initialMode, initialQuery]);

  const syncUrl = (m: SearchMode, q: string) =>
    navigate(routePath('search', [], { mode: m === 'ask' ? 'ask' : undefined, q }), { replace: true });

  const select = (m: SearchMode, focus = false) => {
    if (focus) tabRefs.current[m]?.focus();
    if (m === mode) return;
    setMode(m);
    syncUrl(m, lastQuery.current[m]);
  };

  // APG tabs：左右鍵／Home／End 移動並啟用（自動啟用），分頁本身用 roving tabindex
  const onTabKey = (e: KeyboardEvent) => {
    const i = TABS.findIndex((t) => t.id === mode);
    let next: number | null = null;
    if (e.key === 'ArrowRight') next = (i + 1) % TABS.length;
    else if (e.key === 'ArrowLeft') next = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = TABS.length - 1;
    if (next === null) return;
    e.preventDefault();
    select(TABS[next]!.id, true);
  };

  const submitted = (m: SearchMode) => (q: string) => {
    lastQuery.current[m] = q;
    syncUrl(m, q);
  };

  return (
    <section class="lv-screen" aria-labelledby="lv-search-title">
      <div class="lv-eyebrow">
        {mode === 'ask' ? 'ASK' : 'RECALL'} · {space.en} SPACE
      </div>
      <h1 id="lv-search-title" class="lv-title">
        檢索
      </h1>
      <div class="lv-tabs" role="tablist" aria-label="檢索模式">
        {TABS.map((t) => (
          <button
            key={t.id}
            ref={(el) => {
              tabRefs.current[t.id] = el;
            }}
            type="button"
            role="tab"
            id={`lv-search-tab-${t.id}`}
            class={'lv-tab' + (mode === t.id ? ' is-on' : '')}
            aria-selected={mode === t.id}
            aria-controls={`lv-search-panel-${t.id}`}
            tabIndex={mode === t.id ? 0 : -1}
            onClick={() => select(t.id)}
            onKeyDown={onTabKey}
          >
            {t.label}
          </button>
        ))}
      </div>
      {/* 兩個分頁都保持掛載（切換不丟結果、不重問），非作用中的用 hidden 隱藏 */}
      <div
        role="tabpanel"
        id="lv-search-panel-recall"
        aria-labelledby="lv-search-tab-recall"
        hidden={mode !== 'recall'}
      >
        <RecallPanel
          active={mode === 'recall'}
          initialQuery={parseMode(initialMode) === 'recall' ? initialQuery : ''}
          inputId={mode === 'recall' ? SEARCH_INPUT_ID : undefined}
          onSubmitted={submitted('recall')}
        />
      </div>
      <div role="tabpanel" id="lv-search-panel-ask" aria-labelledby="lv-search-tab-ask" hidden={mode !== 'ask'}>
        <Ask
          active={mode === 'ask'}
          initialQuestion={parseMode(initialMode) === 'ask' ? initialQuery : ''}
          inputId={mode === 'ask' ? SEARCH_INPUT_ID : undefined}
          onSubmitted={submitted('ask')}
        />
      </div>
    </section>
  );
}

function RecallPanel({
  active,
  initialQuery,
  inputId,
  onSubmitted,
}: {
  active: boolean;
  initialQuery: string;
  inputId?: string;
  onSubmitted: (query: string) => void;
}) {
  const { api, space, vault, vaults, navigate, limits } = useApp();
  const DEFAULT_LIMIT = limits.recall_default_limit;
  const DEFAULT_BUDGET = limits.recall_default_budget;
  const MAX_LIMIT = limits.recall_max_limit;
  const MAX_BUDGET = DEFAULT_BUDGET * BUDGET_GROWTH_CAP;
  const [input, setInput] = useState(initialQuery);
  const [params, setParams] = useState<Params | null>(
    initialQuery.trim() ? { query: initialQuery.trim(), vault, limit: DEFAULT_LIMIT, budget: DEFAULT_BUDGET } : null,
  );
  const [result, setResult] = useState<RecallResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [retryTick, setRetryTick] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // 外部帶 ?q= 進來（例如點未解析的 [[標題]]）
  useEffect(() => {
    const q = initialQuery.trim();
    if (q && q !== params?.query) {
      setInput(q);
      setParams({ query: q, vault, limit: DEFAULT_LIMIT, budget: DEFAULT_BUDGET });
    }
  }, [initialQuery]);

  // vault 篩選變了：沿用目前查詢重查（預算重置）
  useEffect(() => {
    setParams((p) => (p && p.vault !== vault ? { ...p, vault, limit: DEFAULT_LIMIT, budget: DEFAULT_BUDGET } : p));
  }, [vault]);

  useEffect(() => {
    if (!params) return;
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    api
      .post<RecallResult>(
        '/v1/recall',
        { space: space.id, query: params.query, vault: params.vault, limit: params.limit, budget: params.budget },
        ctrl.signal,
      )
      .then(({ data }) => {
        setResult(data);
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
        setResult(null);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, space.id, params, retryTick]);

  const submit = (e: Event) => {
    e.preventDefault();
    const q = input.trim();
    if (!q) return;
    setParams({ query: q, vault, limit: DEFAULT_LIMIT, budget: DEFAULT_BUDGET });
    onSubmitted(q);
  };

  const loadMore = () => {
    if (!params || !result) return;
    const budget = Math.min(MAX_BUDGET, Math.max(params.budget * 2, result.used_chars + 2000));
    const limit = Math.min(MAX_LIMIT, Math.max(params.limit, result.items.length + result.omitted));
    setParams({ ...params, budget, limit });
  };

  const open = (item: RecallItem) => {
    if (item.kind === 'chunk' && item.document_id) {
      const idx = item.chunk_id?.split(':').pop();
      navigate(routePath('docs', [item.document_id], { chunk: idx, q: params?.query }));
    } else if (item.kind === 'note') {
      navigate(routePath('notes', [item.id]));
    }
  };

  const degraded = result?.degraded === true;
  const semantic = result ? result.legs.some((l) => l.includes('vector')) : false;
  const modeLabel = !result ? '' : degraded ? 'KEYWORD ONLY' : semantic ? 'HYBRID · FTS + 語意' : `MODE · ${result.mode}`;
  const atCap = params ? params.budget >= MAX_BUDGET && params.limit >= MAX_LIMIT : false;

  return (
    <>
      <form class="lv-search" role="search" onSubmit={submit}>
        <span class="lv-search__prompt" aria-hidden="true">
          &gt;
        </span>
        <input
          ref={inputRef}
          id={inputId}
          class="lv-search__input"
          type="search"
          name="q"
          aria-label="檢索查詢"
          placeholder="關鍵詞、中英混合皆可，Enter 查詢（快捷鍵 /）"
          value={input}
          onInput={(e) => setInput((e.target as HTMLInputElement).value)}
        />
        {modeLabel && (
          <span class={'lv-search__mode' + (degraded ? ' is-degraded' : '')} data-testid="recall-mode">
            {modeLabel}
          </span>
        )}
        <button type="submit" class="btn-outline btn-outline--sm" disabled={!input.trim()}>
          查詢
        </button>
      </form>

      <div class="lv-filters">
        {active && <VaultPicker />}
      </div>

      {degraded && result && (
        <Banner
          tone="warn"
          label="DEGRADED"
          testId="recall-degraded"
          title="降級檢索：只有關鍵字比對"
          action={
            <button type="button" class="btn-outline btn-outline--sm" onClick={() => navigate(routePath('health'))}>
              查看健檢
            </button>
          }
        >
          原因：{describeDegradedReason(result.degraded_reason)}
          {result.degraded_detail ? `（${result.degraded_detail}）` : ''}。以下結果沒有經過語意排序，同義詞與換句話說的內容不會出現。
        </Banner>
      )}

      {result && result.unsupported_kinds.length > 0 && (
        <Banner tone="warn" label="PARTIAL" testId="recall-unsupported">
          這次沒有查到：{result.unsupported_kinds.map((k) => KIND_LABEL[k] ?? k).join('、')}
          {degraded ? '（降級模式下不支援）' : '（服務目前不支援）'}。
        </Banner>
      )}

      {result && !degraded && missingVectors(result) && (
        <Banner tone="info" label="INDEX" testId="recall-missing-vectors">
          {missingVectors(result)}
        </Banner>
      )}

      {!params && (
        <EmptyState title="輸入關鍵詞開始檢索">結果只列標題與摘要，點開看全文。快捷鍵 / 可隨時回到查詢框。</EmptyState>
      )}
      {loading && !result && <Loading label="檢索中…" />}
      {error !== null && <ErrorState error={error} onRetry={() => setRetryTick((t) => t + 1)} />}

      {result && params && (
        <>
          <div class="lv-list-head">
            <span>
              {result.items.length} 筆結果 · {vaultName({ vaults }, params.vault)}
              {loading ? ' · 更新中…' : ''}
            </span>
            <span>只列標題與摘要 · 點開看全文</span>
          </div>
          {result.items.length === 0 && (
            <EmptyState testId="recall-empty" title={`沒有符合「${params.query}」的結果`}>
              {degraded ? '降級中只做了關鍵字比對，換個說法可能查得到。' : '換個關鍵詞，或把 vault 篩選改成「本 space 全部」。'}
            </EmptyState>
          )}
          <ol class="lv-results">
            {result.items.map((item) => (
              <li key={item.id}>
                <ResultRow item={item} degraded={degraded} vaultLabel={vaultName({ vaults }, item.vault)} onOpen={() => open(item)} />
              </li>
            ))}
          </ol>
          {result.truncated && (
            <div class="lv-truncated" role="status" data-testid="recall-truncated">
              <span class="lv-truncated__label">TRUNCATED</span>
              <span class="lv-truncated__text">
                已達字數預算 {result.budget.toLocaleString()} 字（用掉 {result.used_chars.toLocaleString()}）。
                {result.omitted > 0 ? (
                  <>
                    另有 <strong>{result.omitted} 筆</strong>結果未列出。
                  </>
                ) : (
                  <>結果都列出了，但部分摘要被截短。</>
                )}
              </span>
              <button type="button" class="btn-outline btn-outline--sm" onClick={loadMore} disabled={loading || atCap}>
                {atCap ? '已達前端上限' : '載入其餘結果'}
              </button>
            </div>
          )}
        </>
      )}
    </>
  );
}

function missingVectors(result: RecallResult): string | null {
  const parts: string[] = [];
  if (result.missing_embeddings) parts.push(`${result.missing_embeddings} 則筆記`);
  if (result.missing_chunk_embeddings) parts.push(`${result.missing_chunk_embeddings} 個文件段落`);
  if (parts.length === 0) return null;
  return `範圍內有 ${parts.join('、')}尚無向量（背景補算中），語意那一路查不到它們，只能靠關鍵字命中。`;
}

function ResultRow({
  item,
  degraded,
  vaultLabel,
  onOpen,
}: {
  item: RecallItem;
  degraded: boolean;
  vaultLabel: string;
  onOpen: () => void;
}) {
  const isChunk = item.kind === 'chunk';
  const typeLabel = item.kind === 'note' ? 'NOTE' : isChunk ? 'DOC · 段落' : item.kind.toUpperCase();
  return (
    <button type="button" class="lv-result" onClick={onOpen} data-kind={item.kind}>
      <span class="lv-result__type">
        <span class={'lv-result__kind' + (isChunk ? ' is-doc' : '')}>{typeLabel}</span>
        {isChunk && <span class="lv-result__loc">{locatorLabel(item.locator)}</span>}
      </span>
      <span class="lv-result__main">
        <span class="lv-result__title">{item.title}</span>
        <span class="lv-result__summary">
          <SourceTag source={item.summary_source} />
          <span class="lv-result__text">{item.summary ?? '（沒有摘要可顯示）'}</span>
        </span>
      </span>
      <span class="lv-result__meta">
        <Badge tone="vault" label="vault" title={item.vault}>
          {vaultLabel}
        </Badge>
        {item.kind === 'note' && (
          <Badge tone={item.author ? 'author' : 'plain'} label="寫入者" testId="result-author">
            {authorLabel(item.author)}
          </Badge>
        )}
        <Badge tone="time" label="更新">
          {formatTime(item.updated)}
        </Badge>
        <Badge tone={degraded ? 'degraded' : 'score'} label="檢索模式與分數" title="RRF 融合分數">
          {degraded ? '關鍵字' : '混合'} · {item.score.toFixed(4)}
        </Badge>
      </span>
    </button>
  );
}
