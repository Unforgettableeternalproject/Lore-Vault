// 審核頁：統一佇列，合併 agent 提案（刪除、修改人類筆記、移除連結、任務封存）、任務層待授權與 UEP 待審。
// 提案在頁內開面板直接處理；任務待授權導到任務頁；UEP 項目導到筆記詳情頁的審核面板。
// 分頁用服務給的不透明游標（載入更多），kind 篩選同步到網址。
import { useEffect, useState } from 'preact/hooks';

import { ChipGroup, FilterGroup, FilterPanel, queryChoice, screenQuery, useQuerySync } from '../components/Filters';
import { ProposalPanel, type ProposalOutcome } from '../components/ProposalPanel';
import { Badge, Banner, EmptyState, ErrorState, Loading } from '../components/ui';
import { VaultPicker } from '../components/VaultPicker';
import { useApp, vaultName } from '../lib/context';
import { authorLabel, formatTime, isAbort } from '../lib/format';
import { countTotal, itemHref, kindLabel, QUEUE_KINDS, QUEUE_PAGE_SIZE } from '../lib/proposals';
import { routePath } from '../lib/router';
import type { QueueCounts, QueueItem, QueueKind, QueueResult } from '../lib/types';

type KindFilter = QueueKind | 'all';

export function Review() {
  const { api, space, vault, vaults, navigate, toast, switchSpace } = useApp();
  const personal = space.id === 'personal';
  const [kind, setKind] = useState<KindFilter>(() => queryChoice(screenQuery('review'), 'kind', ['all', ...QUEUE_KINDS] as const, 'all'));
  const [items, setItems] = useState<QueueItem[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [counts, setCounts] = useState<QueueCounts | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [tick, setTick] = useState(0);
  const [open, setOpen] = useState<QueueItem | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useQuerySync('review', { kind: kind === 'all' ? null : kind });

  const scope = { space: space.id, vault };

  useEffect(() => {
    // personal space 不經審核，不查詢佇列
    if (personal) return;
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    const queue = api.post<QueueResult>('/v1/review/queue', { ...scope, kinds: kind === 'all' ? undefined : [kind], limit: QUEUE_PAGE_SIZE }, ctrl.signal);
    const count = api.post<QueueCounts>('/v1/review/counts', scope, ctrl.signal);
    Promise.all([queue, count])
      .then(([q, c]) => {
        setItems(q.data.items);
        setCursor(q.data.has_more ? q.data.next_cursor : null);
        setCounts(c.data);
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, space.id, vault, kind, tick, personal]);

  const loadMore = async () => {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const { data } = await api.post<QueueResult>('/v1/review/queue', {
        ...scope,
        kinds: kind === 'all' ? undefined : [kind],
        limit: QUEUE_PAGE_SIZE,
        cursor,
      });
      setItems((prev) => [...(prev ?? []), ...data.items]);
      setCursor(data.has_more ? data.next_cursor : null);
    } catch (err) {
      setError(err);
    } finally {
      setLoadingMore(false);
    }
  };

  const onPanelDone = (outcome: ProposalOutcome) => {
    setOpen(null);
    if (outcome.type === 'conflict') setNotice(outcome.message);
    else {
      setNotice(null);
      toast(outcome.type === 'approved' ? '已核准' : '已駁回', 'success');
    }
    setTick((t) => t + 1);
  };

  const openItem = (item: QueueItem) => {
    if (item.source === 'proposal') setOpen(item);
    else navigate(itemHref(item));
  };

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

  const total = countTotal(counts);
  const kindOptions = [
    { id: 'all' as KindFilter, label: `全部${counts ? ` ${total}` : ''}` },
    ...QUEUE_KINDS.map((k) => ({ id: k as KindFilter, label: `${kindLabel(k)}${counts ? ` ${counts.counts[k] ?? 0}` : ''}` })),
  ];

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
      <FilterPanel
        summary={`範圍：${kind === 'all' ? '全部種類' : kindLabel(kind)}${counts ? ` · 待審 ${kind === 'all' ? total : (counts.counts[kind] ?? 0)} 則` : ''}`}
        active={kind !== 'all'}
        onClear={() => setKind('all')}
      >
        <div class="lv-filter-panel__grid">
          <FilterGroup title="種類" wide testId="review-kind-filter">
            <ChipGroup label="種類" groupLabel="依種類篩選" options={kindOptions} value={kind} onChange={setKind} mono={false} />
          </FilterGroup>
        </div>
      </FilterPanel>

      {notice && (
        <Banner tone="warn" label="REVIEW" testId="review-notice">
          {notice}（已重新載入佇列）
        </Banner>
      )}
      {error !== null && <ErrorState error={error} onRetry={() => setTick((t) => t + 1)} />}
      {loading && !items && <Loading />}

      {items && items.length > 0 && (
        <>
          <p class="lv-hint" data-testid="review-count">
            已列出 {items.length} 則{cursor ? '，還有更多' : ''}
          </p>
          <div class="lv-table lv-table--notes" role="table" aria-label="待審項目">
            <div class="lv-table__head" role="row">
              <span role="columnheader">項目</span>
              <span role="columnheader">提出者</span>
              <span role="columnheader">vault</span>
              <span role="columnheader" class="is-right">
                建立
              </span>
            </div>
            {items.map((n) => (
              <a
                key={`${n.source}:${n.item_id}`}
                role="row"
                class="lv-table__row"
                data-testid="review-row"
                data-kind={n.kind}
                href={n.source === 'proposal' ? routePath('review') : itemHref(n)}
                onClick={(e) => {
                  e.preventDefault();
                  openItem(n);
                }}
              >
                <span role="cell" class="lv-table__main">
                  <span class="lv-table__title">{n.target.title ?? n.target.id}</span>
                  <span class="lv-table__sub">
                    <Badge tone={n.kind === 'note_delete' || n.kind === 'document_delete' ? 'warn' : 'kind'} label="種類" testId="review-kind">
                      {kindLabel(n.kind)}
                    </Badge>
                    {n.state === 'approved' && ' · 已同意，等待 agent 執行'}
                    {n.stale && (
                      <span class="lv-tag lv-tag--warn" data-testid="review-stale">
                        基準已變動
                      </span>
                    )}
                    {n.summary ? ` · ${n.summary}` : ''}
                  </span>
                </span>
                <span role="cell" class="lv-table__who">
                  <Badge tone={n.proposer.author ? 'author' : 'plain'} label="提出者" testId="review-author">
                    {authorLabel(n.proposer.author ?? n.proposer.principal)}
                  </Badge>
                </span>
                <span role="cell">
                  <Badge tone="vault" label="vault" title={n.vault}>
                    {vaultName({ vaults }, n.vault)}
                  </Badge>
                </span>
                <span role="cell" class="is-right">
                  <Badge tone="time" label="建立">
                    {formatTime(n.created_at)}
                  </Badge>
                </span>
              </a>
            ))}
          </div>
          {cursor && (
            <div class="lv-actions">
              <button type="button" class="btn-outline" data-testid="review-more" disabled={loadingMore} onClick={() => void loadMore()}>
                {loadingMore ? '載入中…' : '載入更多'}
              </button>
            </div>
          )}
        </>
      )}
      {items && items.length === 0 && !loading && <EmptyState testId="review-empty" title="沒有等待審核的項目" />}
      {open && <ProposalPanel item={open} onClose={() => setOpen(null)} onDone={onPanelDone} />}
    </section>
  );
}
