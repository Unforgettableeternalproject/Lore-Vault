// 列表頁共用的篩選元件（記憶層、筆記、文件）：篩選區塊外框（摘要＋清除篩選）、單選 chip 組、
// 送出才套用的文字篩選，以及篩選條件與網址查詢字串的同步（重新整理、返回上一頁都保留）。
import type { ComponentChildren } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import { BASE, routePath, type ScreenId } from '../lib/router';

/** 文字篩選值的長度上限（與服務端 list 篩選一致） */
export const MAX_FILTER_TEXT = 200;

/** 篩選區塊：各列控制項放 children；summary 是目前範圍的一句話，有 onClear 且 active 時附「清除篩選」。 */
export function FilterPanel({
  children,
  summary,
  active = false,
  onClear,
}: {
  children: ComponentChildren;
  summary: ComponentChildren;
  active?: boolean;
  onClear?: () => void;
}) {
  const text = <p class="lv-filter-panel__summary lv-muted lv-small">{summary}</p>;
  return (
    <section class="lv-filter-panel" aria-label="篩選條件" data-testid="filter-panel">
      {children}
      {onClear ? (
        <div class="lv-filter-panel__foot">
          {text}
          {active && (
            <button type="button" class="btn-terminal" data-testid="filter-clear" onClick={onClear}>
              清除篩選
            </button>
          )}
        </div>
      ) : (
        text
      )}
    </section>
  );
}

export interface ChipOption<T extends string> {
  id: T;
  label: string;
}

/** 單選 chip 組（aria-pressed）；label 是可見的欄名，groupLabel 給螢幕閱讀器。 */
export function ChipGroup<T extends string>({
  label,
  groupLabel,
  options,
  value,
  onChange,
  mono = true,
}: {
  label: string;
  groupLabel: string;
  options: readonly ChipOption<T>[];
  value: T;
  onChange: (next: T) => void;
  mono?: boolean;
}) {
  return (
    <div class="lv-chips" role="group" aria-label={groupLabel}>
      <span class="lv-filters__label">{label}</span>
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          class={'lv-chip' + (mono ? ' lv-chip--mono' : '') + (value === o.id ? ' is-on' : '')}
          aria-pressed={value === o.id}
          onClick={() => onChange(o.id)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** 文字篩選：輸入不即時送出，按按鈕或 Enter 才套用（去頭尾空白）；外部清除時輸入框跟著清空。 */
export function TextFilter({
  value,
  onApply,
  inputLabel,
  placeholder,
  submitLabel,
  testId,
}: {
  value: string;
  onApply: (next: string) => void;
  inputLabel: string;
  placeholder: string;
  submitLabel: string;
  testId?: string;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <form
      class="lv-filter-panel__scope"
      data-testid={testId}
      onSubmit={(e) => {
        e.preventDefault();
        onApply(draft.trim().slice(0, MAX_FILTER_TEXT));
      }}
    >
      <input
        class="lv-input"
        aria-label={inputLabel}
        placeholder={placeholder}
        maxLength={MAX_FILTER_TEXT}
        value={draft}
        onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
      />
      <button type="submit" class="btn-outline btn-outline--sm">
        {submitLabel}
      </button>
    </form>
  );
}

// ── 網址同步 ──

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** 目前網址正好是這個畫面的列表時，讀出查詢字串；否則（例如測試或別的畫面）視為空。 */
export function screenQuery(screen: ScreenId): URLSearchParams {
  if (typeof window === 'undefined' || window.location.pathname !== BASE + screen) return new URLSearchParams();
  return new URLSearchParams(window.location.search);
}

/** 查詢字串 → 文字篩選值（去空白、截長度；空字串當沒有）。 */
export function queryText(query: URLSearchParams, key: string): string {
  return (query.get(key) ?? '').trim().slice(0, MAX_FILTER_TEXT);
}

/** 查詢字串 → 白名單內的值，其餘當預設。 */
export function queryChoice<T extends string>(query: URLSearchParams, key: string, allowed: readonly T[], fallback: T): T {
  const v = query.get(key);
  return v !== null && (allowed as readonly string[]).includes(v) ? (v as T) : fallback;
}

/** 查詢字串 → 日期（YYYY-MM-DD），格式不對當空。 */
export function queryDate(query: URLSearchParams, key: string): string {
  const v = query.get(key) ?? '';
  return DATE_RE.test(v) ? v : '';
}

/**
 * 篩選條件寫回網址（replaceState，不新增歷史紀錄、不捲動、不重新掛載畫面）。
 * 只在網址仍是這個畫面的列表時寫，避免離開畫面的瞬間覆寫別頁網址；空值省略。
 */
export function useQuerySync(screen: ScreenId, values: Record<string, string | null | undefined>) {
  const target = routePath(screen, [], values);
  useEffect(() => {
    if (window.location.pathname !== BASE + screen) return;
    if (window.location.pathname + window.location.search === target) return;
    window.history.replaceState(window.history.state, '', target);
  }, [screen, target]);
}
