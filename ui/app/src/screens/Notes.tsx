// 筆記列表（T-80）：依目前 vault 篩選分頁瀏覽；篩選（與記憶層同一套元件）走 `/v1/list`：
// 標籤 topics、更新日期區間 since／until、標題 title、作者 author（部分符合）與 author_state
// （已具名／未具名），條件同步到網址查詢字串（vault 仍由側欄共用狀態決定）。
// 標籤選項取自 `/v1/topics`（範圍內全部標籤與筆數）。摘要受 list 的 `budget` 限制，後端在本頁公平分配：
// 超過配額的摘要被截短（summary_truncated，列上標「截短」），連下限都給不起的尾端 note 摘要被省略
// （summary_source: omitted）；整頁的 truncated／summaries_truncated／summaries_omitted 以橫幅呈現並可放大預算。
// 作者（A22 author）沒有值時顯示「未具名」，最後修改者（updated_by）與作者不同時另外標出；
// 更正鏈（supersedes／superseded_by）在列上標示。
import { useEffect, useState } from 'preact/hooks';

import { Badge, Banner, EmptyState, ErrorState, Loading, SourceTag } from '../components/ui';
import {
  ChipGroup,
  FilterPanel,
  queryChoice,
  queryDate,
  queryText,
  screenQuery,
  TextFilter,
  useQuerySync,
} from '../components/Filters';
import { DateRange, DEFAULT_PAGE_SIZE, Pager, rangeParams, type DateRangeValue } from '../components/Pager';
import { VaultPicker } from '../components/VaultPicker';
import { useApp, vaultName } from '../lib/context';
import { authorLabel, describeError, formatTime, isAbort } from '../lib/format';
import { routePath } from '../lib/router';
import type { ListResult, NoteListItem, TopicsResult } from '../lib/types';

/** 列表每則摘要平均可用的字數：budget = 本頁筆數 × 此值（摘要平均約 200 字；服務預設 4000／50 則每則只剩 80 字） */
export const LIST_SUMMARY_CHARS = 280;
/** 「顯示更多摘要」最多把預算放大到初始值的倍數 */
const BUDGET_GROWTH_CAP = 8;
const AUTHOR_STATES = [
  { id: '', label: '全部' },
  { id: 'named', label: '已具名' },
  { id: 'missing', label: '未具名' },
] as const;
type AuthorState = (typeof AUTHOR_STATES)[number]['id'];
const AUTHOR_STATE_IDS = AUTHOR_STATES.map((a) => a.id);

/** 截斷橫幅標題：分別列出截短與省略的筆數 */
function truncatedTitle(page: ListResult<NoteListItem>): string {
  const parts: string[] = [];
  if (page.summaries_truncated) parts.push(`${page.summaries_truncated} 則摘要被截短`);
  if (page.summaries_omitted) parts.push(`${page.summaries_omitted} 則的摘要沒有列出`);
  return `摘要字數預算不足：本頁${parts.length ? ' ' + parts.join('、') : '有摘要被截短'}`;
}

export function Notes() {
  const { api, space, vault, vaults, navigate, limits } = useApp();
  // 頁碼分頁（與文件、記憶層同一個分頁元件）；每頁筆數不超過服務上限
  const [pageSize, setPageSize] = useState<number>(Math.min(DEFAULT_PAGE_SIZE, limits.list_max_limit));
  const [pageNo, setPageNo] = useState(1);
  const baseBudget = Math.max(limits.list_default_budget, pageSize * LIST_SUMMARY_CHARS);
  const [budget, setBudget] = useState(baseBudget);
  // 篩選初值取自網址（重新整理、從筆記返回都保留）
  const [initial] = useState(() => screenQuery('notes'));
  const [tag, setTag] = useState<string | null>(() => queryText(initial, 'tag') || null);
  const [range, setRange] = useState<DateRangeValue>(() => ({ from: queryDate(initial, 'from'), to: queryDate(initial, 'to') }));
  const [title, setTitle] = useState(() => queryText(initial, 'title'));
  const [author, setAuthor] = useState(() => queryText(initial, 'author'));
  const [authorState, setAuthorState] = useState<AuthorState>(() => queryChoice(initial, 'author_state', AUTHOR_STATE_IDS, ''));
  const [page, setPage] = useState<ListResult<NoteListItem> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [topics, setTopics] = useState<TopicsResult['topics'] | null>(null);
  const [topicsError, setTopicsError] = useState<string | null>(null);

  useQuerySync('notes', { tag, from: range.from, to: range.to, title, author, author_state: authorState });
  const filtered = Boolean(tag || range.from || range.to || title || author || authorState);
  const clearFilters = () => {
    setTag(null);
    setRange({ from: '', to: '' });
    setTitle('');
    setAuthor('');
    setAuthorState('');
  };

  // 篩選或每頁筆數變了回第一頁、摘要預算重置
  useEffect(() => {
    setPageNo(1);
    setBudget(baseBudget);
  }, [vault, tag, range.from, range.to, title, author, authorState, pageSize]);

  // 標籤選項：範圍內全部標籤（不是只看已載入的那一頁）
  useEffect(() => {
    const ctrl = new AbortController();
    setTopicsError(null);
    api
      .post<TopicsResult>('/v1/topics', { space: space.id, vault }, ctrl.signal)
      .then(({ data }) => setTopics(data.topics))
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setTopics(null);
        setTopicsError(describeError(err));
      });
    return () => ctrl.abort();
  }, [api, space.id, vault]);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    api
      .post<ListResult<NoteListItem>>(
        '/v1/list',
        {
          space: space.id,
          vault,
          kinds: ['note'],
          limit: Math.min(pageSize, limits.list_max_limit),
          offset: (pageNo - 1) * pageSize,
          with_total: true,
          budget,
          ...(tag ? { topics: [tag] } : {}),
          ...(title ? { title } : {}),
          ...(author ? { author } : {}),
          ...(authorState ? { author_state: authorState } : {}),
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
  }, [api, space.id, vault, tag, range.from, range.to, title, author, authorState, pageNo, pageSize, budget, tick]);

  const items = page?.items ?? [];
  const maxBudget = baseBudget * BUDGET_GROWTH_CAP;
  // 目前選的標籤不在清單裡（例如剛被改名）時仍要顯示，才能取消
  const tagOptions = topics ?? [];
  const tagMissing = tag !== null && !tagOptions.some((t) => t.topic === tag);

  return (
    <section class="lv-screen lv-screen--wide" aria-labelledby="lv-notes-title">
      <div class="lv-screen__head">
        <div>
          <div class="lv-eyebrow">
            NOTES · {space.en} / {vaultName({ vaults }, vault)}
          </div>
          <h1 id="lv-notes-title" class="lv-title lv-title--tight">
            筆記
          </h1>
        </div>
        <button type="button" class="btn-outline btn-outline--gold" onClick={() => navigate(routePath('notes', ['new']))}>
          + 新增筆記
        </button>
      </div>

      <FilterPanel
        active={filtered}
        onClear={clearFilters}
        summary={
          <>
            範圍：{vaultName({ vaults }, vault)}
            {tag ? ` · #${tag}` : ''}
            {title ? ` · 標題含「${title}」` : ''}
            {author ? ` · 作者含「${author}」` : ''}
            {authorState === 'named' ? ' · 已具名' : authorState === 'missing' ? ' · 未具名' : ''}
            {range.from || range.to ? ` · ${range.from || '最早'}～${range.to || '今天'}` : ''}
          </>
        }
      >
        <div class="lv-filter-panel__row">
          <VaultPicker />
          <DateRange value={range} onChange={setRange} />
        </div>
        <div class="lv-filter-panel__row">
          <div class="lv-chips" role="group" aria-label="標籤篩選">
            <span class="lv-filters__label">TAGS</span>
            <button type="button" class={'lv-chip lv-chip--mono' + (tag === null ? ' is-on' : '')} aria-pressed={tag === null} onClick={() => setTag(null)}>
              全部
            </button>
            {tagOptions.map((t) => (
              <button
                key={t.topic}
                type="button"
                class={'lv-chip lv-chip--mono' + (tag === t.topic ? ' is-on' : '')}
                aria-pressed={tag === t.topic}
                aria-label={`#${t.topic}（${t.count} 則）`}
                onClick={() => setTag(tag === t.topic ? null : t.topic)}
              >
                #{t.topic}
                <span class="lv-chip__count" aria-hidden="true">
                  {t.count}
                </span>
              </button>
            ))}
            {tagMissing && (
              <button type="button" class="lv-chip lv-chip--mono is-on" aria-pressed="true" onClick={() => setTag(null)}>
                #{tag}
              </button>
            )}
          </div>
        </div>
        <div class="lv-filter-panel__row">
          <TextFilter value={title} onApply={setTitle} inputLabel="標題關鍵字" placeholder="標題含…（不分大小寫）" submitLabel="套用標題" testId="filter-title" />
          <ChipGroup label="作者" groupLabel="作者狀態" options={AUTHOR_STATES} value={authorState} onChange={setAuthorState} />
          <TextFilter value={author} onApply={setAuthor} inputLabel="作者名稱" placeholder="作者含…（不分大小寫）" submitLabel="套用作者" testId="filter-author" />
        </div>
      </FilterPanel>
      {topicsError && (
        <p class="lv-notice lv-notice--warn" role="status" data-testid="topics-error">
          標籤清單載入失敗：{topicsError}（仍可瀏覽筆記，只是不能依標籤篩選）
        </p>
      )}

      {page && page.unsupported_kinds.length > 0 && (
        <Banner tone="warn" label="PARTIAL" testId="list-unsupported">
          這次列不出：{page.unsupported_kinds.join('、')}。
        </Banner>
      )}

      {page?.truncated && (
        <Banner
          tone="warn"
          label="TRUNCATED"
          testId="list-truncated"
          title={truncatedTitle(page)}
          action={
            <button
              type="button"
              class="btn-outline btn-outline--sm"
              disabled={loading || budget >= maxBudget}
              onClick={() => setBudget((b) => Math.min(maxBudget, b * 2))}
            >
              {budget >= maxBudget ? '已達前端上限' : '顯示更多摘要'}
            </button>
          }
        >
          預算 {(page.budget ?? budget).toLocaleString()} 字、用掉 {(page.used_chars ?? 0).toLocaleString()} 字。預算平均分給本頁每則，筆記本身都有列出，只有摘要被截短或省略；完整內容請開啟筆記。
        </Banner>
      )}

      {error !== null && <ErrorState error={error} onRetry={() => setTick((t) => t + 1)} />}
      {loading && !page && <Loading />}

      {page && (
        <>
          <div class="lv-table lv-table--notes" role="table" aria-label="筆記列表">
            <div class="lv-table__head" role="row">
              <span role="columnheader">標題</span>
              <span role="columnheader">標籤</span>
              <span role="columnheader">寫入者</span>
              <span role="columnheader" class="is-right">
                更新
              </span>
            </div>
            {items.map((n) => (
              <a
                key={n.id}
                role="row"
                class="lv-table__row"
                href={routePath('notes', [n.id])}
                onClick={(e) => {
                  e.preventDefault();
                  navigate(routePath('notes', [n.id]));
                }}
              >
                <span role="cell" class="lv-table__main">
                  <span class="lv-table__title">{n.title}</span>
                  {(n.supersedes || n.superseded_by) && (
                    <span class="lv-table__chain">
                      {n.superseded_by && (
                        <span class="lv-tag lv-tag--warn" data-testid="note-superseded">
                          已被更正取代
                        </span>
                      )}
                      {n.supersedes && (
                        <span class="lv-tag" data-testid="note-supersedes">
                          更正版
                        </span>
                      )}
                    </span>
                  )}
                  {n.summary_source && (
                    <span class="lv-table__summary">
                      <SourceTag source={n.summary_source} />
                      <span>{n.summary ?? (n.summary_source === 'omitted' ? '（預算用完，未列出摘要）' : '')}</span>
                      {n.summary_truncated && (
                        <span class="lv-tag" data-testid="summary-truncated" title="摘要超過本頁平均分到的字數，已截短；開啟筆記看全文">
                          截短
                        </span>
                      )}
                    </span>
                  )}
                  {vault === '*' && (
                    <span class="lv-table__sub">
                      <Badge tone="vault" label="vault" title={n.vault}>
                        {vaultName({ vaults }, n.vault)}
                      </Badge>
                    </span>
                  )}
                </span>
                <span role="cell" class="lv-badges">
                  {n.topics.map((t) => (
                    <Badge key={t} tone="tag" label="標籤">
                      #{t}
                    </Badge>
                  ))}
                </span>
                <span role="cell" class="lv-table__who">
                  <Badge tone={n.author ? 'author' : 'plain'} label="寫入者" testId="note-author">
                    {authorLabel(n.author)}
                  </Badge>
                  {n.updated_by && n.updated_by !== n.author && (
                    <span class="lv-mono lv-muted lv-small" data-testid="note-updated-by">
                      最後修改 {n.updated_by}
                    </span>
                  )}
                </span>
                <span role="cell" class="is-right">
                  <Badge tone="time" label="更新">
                    {formatTime(n.updated)}
                  </Badge>
                </span>
              </a>
            ))}
          </div>
          {items.length === 0 && !loading && (
            <EmptyState
              testId="notes-empty"
              title={filtered ? '沒有符合篩選條件的筆記' : '這裡還沒有筆記'}
              action={
                filtered ? (
                  <button type="button" class="btn-outline" onClick={clearFilters}>
                    清除篩選
                  </button>
                ) : (
                  <button type="button" class="btn-outline btn-outline--gold" onClick={() => navigate(routePath('notes', ['new']))}>
                    + 新增筆記
                  </button>
                )
              }
            />
          )}
          {(page.total ?? items.length) > 0 && (
            <Pager
              page={pageNo}
              pageSize={pageSize}
              total={page.total ?? items.length}
              loading={loading}
              unit="則"
              label="筆記分頁"
              onPage={setPageNo}
              onPageSize={setPageSize}
            />
          )}
        </>
      )}
    </section>
  );
}
