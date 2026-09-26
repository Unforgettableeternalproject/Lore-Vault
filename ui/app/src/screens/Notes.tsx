// 筆記列表（T-80）：依目前 vault 篩選分頁瀏覽，標籤與時間篩選走 `/v1/list` 的 topics／since。
// list 回應不含摘要與摘要來源（見回報的 API 缺口）；有 `author` 欄位才顯示作者，否則顯示「未具名」。
import { useEffect, useState } from 'preact/hooks';

import { Banner, ErrorState, Loading, SourceTag } from '../components/ui';
import { useApp, vaultName } from '../lib/context';
import { authorLabel, daysAgoIso, formatTime, isAbort } from '../lib/format';
import { routePath } from '../lib/router';
import type { ListResult, NoteListItem } from '../lib/types';

const PAGE = 50;
const TIME_FILTERS = [
  { id: 'all', label: '全部', days: null },
  { id: '7d', label: '7 天', days: 7 },
  { id: '30d', label: '30 天', days: 30 },
] as const;
type TimeId = (typeof TIME_FILTERS)[number]['id'];

export function Notes() {
  const { api, space, vault, vaults, navigate } = useApp();
  const [tag, setTag] = useState<string | null>(null);
  const [time, setTime] = useState<TimeId>('all');
  // cursor 堆疊：[0] 為第一頁（null）
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState<ListResult<NoteListItem> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [knownTags, setKnownTags] = useState<string[]>([]);

  // 篩選變了回第一頁
  useEffect(() => setCursors([null]), [vault, tag, time]);

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
          ...(cursor ? { cursor } : {}),
          ...(tag ? { topics: [tag] } : {}),
          ...(days ? { since: daysAgoIso(days) } : {}),
        },
        ctrl.signal,
      )
      .then(({ data }) => {
        setPage(data);
        setLoading(false);
        setKnownTags((prev) => {
          const next = new Set(prev);
          data.items.forEach((n) => n.topics.forEach((t) => next.add(t)));
          return next.size === prev.length ? prev : [...next].sort((a, b) => a.localeCompare(b));
        });
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, space.id, vault, tag, time, cursor, tick]);

  const pageNo = cursors.length;
  const items = page?.items ?? [];

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
          {knownTags.map((t) => (
            <button
              key={t}
              type="button"
              class={'lv-chip lv-chip--mono' + (tag === t ? ' is-on' : '')}
              aria-pressed={tag === t}
              onClick={() => setTag(tag === t ? null : t)}
            >
              #{t}
            </button>
          ))}
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
      <p class="lv-hint lv-hint--inline">標籤選項取自已載入的筆記（服務沒有標籤清單端點）。</p>

      {page && page.unsupported_kinds.length > 0 && (
        <Banner tone="warn" label="PARTIAL" testId="list-unsupported">
          這次列不出：{page.unsupported_kinds.join('、')}。
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
                  {n.summary_source && (
                    <span class="lv-table__summary">
                      <SourceTag source={n.summary_source} />
                      <span>{n.summary ?? ''}</span>
                    </span>
                  )}
                  {vault === '*' && <span class="lv-table__sub">{vaultName({ vaults }, n.vault)}</span>}
                </span>
                <span role="cell" class="lv-mono lv-muted">
                  {n.topics.map((t) => `#${t}`).join(' ')}
                </span>
                <span role="cell" class={'lv-mono' + (n.author ? '' : ' lv-muted')} data-testid="note-author">
                  {authorLabel(n.author)}
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
