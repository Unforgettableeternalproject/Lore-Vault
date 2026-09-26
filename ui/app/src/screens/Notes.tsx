// 筆記列表（T-80）：依目前 vault 篩選分頁瀏覽，標籤與時間篩選走 `/v1/list` 的 topics／since；
// 標籤選項取自 `/v1/topics`（範圍內全部標籤與筆數）。摘要受 list 的 `budget` 限制：預算用完的
// note 摘要被省略（summary_source: omitted），整頁的 truncated／summaries_omitted 以橫幅呈現並可放大預算。
// 作者（A22 author）沒有值時顯示「未具名」，最後修改者（updated_by）與作者不同時另外標出；
// 更正鏈（supersedes／superseded_by）在列上標示。
import { useEffect, useState } from 'preact/hooks';

import { Banner, ErrorState, Loading, SourceTag } from '../components/ui';
import { useApp, vaultName } from '../lib/context';
import { authorLabel, daysAgoIso, describeError, formatTime, isAbort } from '../lib/format';
import { routePath } from '../lib/router';
import type { ListResult, NoteListItem, TopicsResult } from '../lib/types';

/** 列表每則摘要平均可用的字數：budget = 本頁筆數 × 此值（服務預設 4000／50 則過緊，列表會大量省略） */
export const LIST_SUMMARY_CHARS = 160;
/** 「顯示更多摘要」最多把預算放大到初始值的倍數 */
const BUDGET_GROWTH_CAP = 8;
const TIME_FILTERS = [
  { id: 'all', label: '全部', days: null },
  { id: '7d', label: '7 天', days: 7 },
  { id: '30d', label: '30 天', days: 30 },
] as const;
type TimeId = (typeof TIME_FILTERS)[number]['id'];

export function Notes() {
  const { api, space, vault, vaults, navigate, limits } = useApp();
  const PAGE = Math.min(limits.list_default_limit, limits.list_max_limit);
  const baseBudget = Math.max(limits.list_default_budget, PAGE * LIST_SUMMARY_CHARS);
  const [budget, setBudget] = useState(baseBudget);
  const [tag, setTag] = useState<string | null>(null);
  const [time, setTime] = useState<TimeId>('all');
  // cursor 堆疊：[0] 為第一頁（null）
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState<ListResult<NoteListItem> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [topics, setTopics] = useState<TopicsResult['topics'] | null>(null);
  const [topicsError, setTopicsError] = useState<string | null>(null);

  // 篩選變了回第一頁、摘要預算重置
  useEffect(() => {
    setCursors([null]);
    setBudget(baseBudget);
  }, [vault, tag, time]);

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

  const cursor = cursors[cursors.length - 1] ?? null;

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    const days = TIME_FILTERS.find((t) => t.id === time)?.days ?? null;
    api
      .post<ListResult<NoteListItem>>(
        '/v1/list',
        {
          space: space.id,
          vault,
          kinds: ['note'],
          limit: PAGE,
          budget,
          ...(cursor ? { cursor } : {}),
          ...(tag ? { topics: [tag] } : {}),
          ...(days ? { since: daysAgoIso(days) } : {}),
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
  }, [api, space.id, vault, tag, time, cursor, budget, tick]);

  const pageNo = cursors.length;
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

      <div class="lv-filters">
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
        <div class="lv-chips" role="group" aria-label="時間篩選">
          <span class="lv-filters__label">TIME</span>
          {TIME_FILTERS.map((t) => (
            <button key={t.id} type="button" class={'lv-chip' + (time === t.id ? ' is-on' : '')} aria-pressed={time === t.id} onClick={() => setTime(t.id)}>
              {t.label}
            </button>
          ))}
        </div>
      </div>
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
          title={
            page.summaries_omitted
              ? `摘要字數預算用完：本頁 ${page.summaries_omitted} 則的摘要沒有列出`
              : '摘要字數預算用完：有一則摘要被截短'
          }
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
          預算 {(page.budget ?? budget).toLocaleString()} 字、用掉 {(page.used_chars ?? 0).toLocaleString()} 字。筆記本身都有列出，只有摘要被省略或截短。
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
            {items.length === 0 && !loading && (
              <div class="zone-state lv-empty" data-testid="notes-empty">
                {tag || time !== 'all' ? '沒有符合篩選條件的筆記。' : '這裡還沒有筆記。'}
              </div>
            )}
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
                    </span>
                  )}
                  {vault === '*' && <span class="lv-table__sub">{vaultName({ vaults }, n.vault)}</span>}
                </span>
                <span role="cell" class="lv-mono lv-muted">
                  {n.topics.map((t) => `#${t}`).join(' ')}
                </span>
                <span role="cell" class="lv-table__who">
                  <span class={'lv-mono' + (n.author ? '' : ' lv-muted')} data-testid="note-author">
                    {authorLabel(n.author)}
                  </span>
                  {n.updated_by && n.updated_by !== n.author && (
                    <span class="lv-mono lv-muted lv-small" data-testid="note-updated-by">
                      最後修改 {n.updated_by}
                    </span>
                  )}
                </span>
                <span role="cell" class="lv-mono lv-muted is-right">
                  {formatTime(n.updated)}
                </span>
              </a>
            ))}
          </div>
          <div class="lv-pager">
            <span>
              第 {pageNo} 頁 · 本頁 {items.length} 則{loading ? ' · 更新中…' : ''}
            </span>
            <div class="lv-pager__btns">
              <button type="button" class="btn-terminal" disabled={pageNo <= 1 || loading} onClick={() => setCursors((c) => c.slice(0, -1))}>
                ← 上頁
              </button>
              <button
                type="button"
                class="btn-terminal"
                disabled={!page.next_cursor || loading}
                onClick={() => page.next_cursor && setCursors((c) => [...c, page.next_cursor])}
              >
                下頁 →
              </button>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
