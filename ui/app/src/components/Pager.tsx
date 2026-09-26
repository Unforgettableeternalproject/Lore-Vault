// 頁碼分頁：左側每頁筆數（10／30／50／100）與總數，右側首頁／上一頁／頁碼／下一頁／末頁與跳頁。
// 搭配服務端的 offset／with_total（`/v1/list`、`/v1/concept_query`）；頁碼從 1 起算。
import { useEffect, useState } from 'preact/hooks';

export const PAGE_SIZES = [10, 30, 50, 100] as const;
export const DEFAULT_PAGE_SIZE = 30;

let seq = 0;

/** 頁碼視窗：總是含第一頁、最後一頁與目前頁前後各一頁，中間以 null（省略號）隔開。 */
export function pageWindow(page: number, pages: number): (number | null)[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
  const set = new Set([1, pages, page - 1, page, page + 1].filter((p) => p >= 1 && p <= pages));
  if (page <= 3) [2, 3, 4].forEach((p) => set.add(p));
  if (page >= pages - 2) [pages - 1, pages - 2, pages - 3].forEach((p) => set.add(p));
  const sorted = [...set].sort((a, b) => a - b);
  const out: (number | null)[] = [];
  sorted.forEach((p, i) => {
    if (i > 0 && p - sorted[i - 1]! > 1) out.push(null);
    out.push(p);
  });
  return out;
}

export function Pager({
  page,
  pageSize,
  total,
  onPage,
  onPageSize,
  loading = false,
  unit = '筆',
  label = '分頁',
}: {
  page: number;
  pageSize: number;
  total: number;
  onPage: (page: number) => void;
  onPageSize: (size: number) => void;
  loading?: boolean;
  unit?: string;
  label?: string;
}) {
  const ids = useState(() => `lv-pager-${++seq}`)[0];
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const [jump, setJump] = useState(String(page));
  useEffect(() => setJump(String(page)), [page]);

  const go = (p: number) => {
    const next = Math.min(pages, Math.max(1, p));
    if (next !== page) onPage(next);
  };
  const submitJump = (e: Event) => {
    e.preventDefault();
    const n = Number.parseInt(jump, 10);
    if (Number.isFinite(n)) go(n);
    else setJump(String(page));
  };
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);

  return (
    <nav class="lv-pager2" aria-label={label} data-testid="pager">
      <div class="lv-pager2__left">
        <label class="lv-pager2__size" for={`${ids}-size`}>
          每頁
        </label>
        <select
          id={`${ids}-size`}
          class="lv-input lv-select lv-pager2__select"
          value={String(pageSize)}
          aria-label="每頁筆數"
          onChange={(e) => onPageSize(Number((e.target as HTMLSelectElement).value))}
        >
          {PAGE_SIZES.map((n) => (
            <option key={n} value={String(n)}>
              {n}
            </option>
          ))}
        </select>
        <span class="lv-pager2__info" role="status" data-testid="pager-info">
          {total === 0 ? `共 0 ${unit}` : `第 ${from}–${to} ${unit}，共 ${total} ${unit}`}
          {loading ? ' · 更新中…' : ''}
        </span>
      </div>
      <div class="lv-pager2__right">
        <button type="button" class="btn-terminal lv-pager2__btn" disabled={page <= 1 || loading} onClick={() => go(1)} aria-label="第一頁">
          «
        </button>
        <button type="button" class="btn-terminal lv-pager2__btn" disabled={page <= 1 || loading} onClick={() => go(page - 1)} aria-label="上一頁">
          ‹
        </button>
        <ol class="lv-pager2__pages">
          {pageWindow(page, pages).map((p, i) =>
            p === null ? (
              <li key={`gap-${i}`} class="lv-pager2__gap" aria-hidden="true">
                …
              </li>
            ) : (
              <li key={p}>
                <button
                  type="button"
                  class={'btn-terminal lv-pager2__btn' + (p === page ? ' is-current' : '')}
                  aria-current={p === page ? 'page' : undefined}
                  aria-label={`第 ${p} 頁`}
                  disabled={loading && p !== page}
                  onClick={() => go(p)}
                >
                  {p}
                </button>
              </li>
            ),
          )}
        </ol>
        <button type="button" class="btn-terminal lv-pager2__btn" disabled={page >= pages || loading} onClick={() => go(page + 1)} aria-label="下一頁">
          ›
        </button>
        <button type="button" class="btn-terminal lv-pager2__btn" disabled={page >= pages || loading} onClick={() => go(pages)} aria-label="最後一頁">
          »
        </button>
        <form class="lv-pager2__jump" onSubmit={submitJump}>
          <label for={`${ids}-jump`}>跳至</label>
          <input
            id={`${ids}-jump`}
            class="lv-input lv-pager2__jump-input"
            type="number"
            min={1}
            max={pages}
            inputMode="numeric"
            aria-label={`跳至頁碼（共 ${pages} 頁）`}
            value={jump}
            onInput={(e) => setJump((e.target as HTMLInputElement).value)}
          />
          <span aria-hidden="true">／{pages} 頁</span>
          <button type="submit" class="btn-terminal lv-pager2__btn" disabled={loading}>
            前往
          </button>
        </form>
      </div>
    </nav>
  );
}

/** 本地日期（YYYY-MM-DD）→ 當天起點／終點的 UTC ISO，用於 since／until（含端點）。 */
export function dayStartIso(date: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  const [y, m, d] = date.split('-').map(Number) as [number, number, number];
  return new Date(y, m - 1, d, 0, 0, 0, 0).toISOString();
}

export function dayEndIso(date: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  const [y, m, d] = date.split('-').map(Number) as [number, number, number];
  return new Date(y, m - 1, d, 23, 59, 59, 999).toISOString();
}

export interface DateRangeValue {
  from: string;
  to: string;
}

/** 日期區間（起訖，含端點）；兩欄可各自留空。 */
export function DateRange({ value, onChange }: { value: DateRangeValue; onChange: (next: DateRangeValue) => void }) {
  const ids = useState(() => `lv-range-${++seq}`)[0];
  const invalid = value.from && value.to && value.from > value.to;
  return (
    <fieldset class="lv-daterange" data-testid="date-range">
      <legend class="lv-filters__label">日期</legend>
      <div class="lv-daterange__row">
        <input
          id={`${ids}-from`}
          class="lv-input lv-daterange__input"
          type="date"
          aria-label="起日"
          value={value.from}
          max={value.to || undefined}
          onInput={(e) => onChange({ ...value, from: (e.target as HTMLInputElement).value })}
        />
        <span aria-hidden="true">～</span>
        <input
          id={`${ids}-to`}
          class="lv-input lv-daterange__input"
          type="date"
          aria-label="訖日"
          value={value.to}
          min={value.from || undefined}
          onInput={(e) => onChange({ ...value, to: (e.target as HTMLInputElement).value })}
        />
        {(value.from || value.to) && (
          <button type="button" class="btn-terminal" onClick={() => onChange({ from: '', to: '' })}>
            清除
          </button>
        )}
      </div>
      {invalid && (
        <p class="lv-notice lv-notice--warn lv-daterange__warn" role="status">
          起日晚於訖日，沒有符合的資料。
        </p>
      )}
    </fieldset>
  );
}

/** 日期區間轉成 API 參數（空欄不送）。 */
export function rangeParams(range: DateRangeValue): { since?: string; until?: string } {
  const since = range.from ? dayStartIso(range.from) : null;
  const until = range.to ? dayEndIso(range.to) : null;
  return { ...(since ? { since } : {}), ...(until ? { until } : {}) };
}
