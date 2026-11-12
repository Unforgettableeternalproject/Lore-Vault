// 審核佇列：列出 UEP 送來等待審核的筆記（review_state 為 pending 與 pending_delete）。
// 服務的 review_state 篩選一次只收一個值，所以分兩次查再合併，依更新時間新到舊排序。
// 點進筆記後在詳情頁核准或駁回。
import { useEffect, useState } from 'preact/hooks';

import { UnverifiedTag } from '../components/ReviewBadge';
import { Badge, EmptyState, ErrorState, Loading } from '../components/ui';
import { VaultPicker } from '../components/VaultPicker';
import { useApp, vaultName } from '../lib/context';
import { authorLabel, formatTime, isAbort } from '../lib/format';
import { routePath } from '../lib/router';
import type { ListResult, NoteListItem, ReviewState } from '../lib/types';

const QUEUE_STATES: ReviewState[] = ['pending', 'pending_delete'];

export function Review() {
  const { api, space, vault, vaults, navigate, limits, switchSpace } = useApp();
  const personal = space.id === 'personal';
  const [items, setItems] = useState<NoteListItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    // personal space 不經審核，不查詢佇列
    if (personal) return;
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all(
      QUEUE_STATES.map((state) =>
        api.post<ListResult<NoteListItem>>(
          '/v1/list',
          { space: space.id, vault, kinds: ['note'], limit: limits.list_max_limit, with_total: true, review_state: state },
          ctrl.signal,
        ),
      ),
    )
      .then((results) => {
        const merged = results.flatMap(({ data }) => data.items).sort((a, b) => b.updated.localeCompare(a.updated));
        setItems(merged);
        setTotal(results.reduce((sum, { data }) => sum + (data.total ?? data.items.length), 0));
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, space.id, vault, limits.list_max_limit, tick, personal]);

  if (personal) {
    return (
      <section class="lv-screen lv-screen--wide" aria-labelledby="lv-review-title">
        <div class="lv-eyebrow">REVIEW · {space.en}</div>
        <h1 id="lv-review-title" class="lv-title">
          審核佇列
        </h1>
        <EmptyState
          testId="review-personal-none"
          title={`${space.en} space 沒有審核`}
          action={
            <button type="button" class="btn-outline btn-outline--gold" onClick={() => switchSpace('dev', routePath('review'))}>
              切換到 DEV 檢視
            </button>
          }
        >
          personal space 的內容不經審核，agent 的寫入直接生效。
        </EmptyState>
      </section>
    );
  }

  return (
    <section class="lv-screen lv-screen--wide" aria-labelledby="lv-review-title">
      <div class="lv-screen__head">
        <div>
          <div class="lv-eyebrow">
            REVIEW · {space.en} / {vaultName({ vaults }, vault)}
          </div>
          <h1 id="lv-review-title" class="lv-title lv-title--tight">
            審核佇列
          </h1>
        </div>
        <button type="button" class="btn-outline" disabled={loading} onClick={() => setTick((t) => t + 1)}>
          重新整理
        </button>
      </div>
      <div class="lv-filters">
        <VaultPicker />
      </div>

      {error !== null && <ErrorState error={error} onRetry={() => setTick((t) => t + 1)} />}
      {loading && !items && <Loading />}

      {items && items.length > 0 && (
        <>
          <p class="lv-hint" data-testid="review-count">
            共 {total} 則等待審核{total > items.length ? `（只列出最新 ${items.length} 則，處理後再重新整理）` : ''}
          </p>
          <div class="lv-table lv-table--notes" role="table" aria-label="待審筆記">
            <div class="lv-table__head" role="row">
              <span role="columnheader">標題</span>
              <span role="columnheader">寫入者</span>
              <span role="columnheader">vault</span>
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
                  <UnverifiedTag item={n} />
                </span>
                <span role="cell" class="lv-table__who">
                  <Badge tone={n.author ? 'author' : 'plain'} label="寫入者" testId="review-author">
                    {authorLabel(n.author)}
                  </Badge>
                </span>
                <span role="cell">
                  <Badge tone="vault" label="vault" title={n.vault}>
                    {vaultName({ vaults }, n.vault)}
                  </Badge>
                </span>
                <span role="cell" class="is-right">
                  <Badge tone="time" label="更新">
                    {formatTime(n.updated)}
                  </Badge>
                </span>
              </a>
            ))}
          </div>
        </>
      )}
      {items && items.length === 0 && !loading && <EmptyState testId="review-empty" title="沒有等待審核的項目" />}
    </section>
  );
}
