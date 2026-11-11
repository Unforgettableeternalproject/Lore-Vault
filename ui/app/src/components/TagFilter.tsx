// 標籤篩選（Notes）：全部／已選 chip 固定在最前面，其餘 chip 在限高區塊內捲動；搜尋框即時過濾標籤名稱。
// 排序沿用呼叫端給的順序（依筆數）；點選行為由呼叫端的 onChange 決定（含寫回網址）。
import { useState } from 'preact/hooks';

export interface TagOption {
  topic: string;
  count: number;
}

/** 過濾用的正規化：去頭尾空白、忽略開頭的 #、不分大小寫（中文原樣比對） */
export function normalizeTagQuery(value: string): string {
  return value.trim().replace(/^#+/, '').trim().toLowerCase();
}

export function filterTags<T extends TagOption>(options: readonly T[], query: string): T[] {
  const q = normalizeTagQuery(query);
  return q ? options.filter((t) => t.topic.toLowerCase().includes(q)) : [...options];
}

export function TagFilter({
  options,
  value,
  onChange,
}: {
  options: readonly TagOption[];
  value: string | null;
  onChange: (tag: string | null) => void;
}) {
  const [query, setQuery] = useState('');
  const selected = value === null ? undefined : options.find((t) => t.topic === value);
  // 已選的固定顯示在最前面，不在捲動清單重複出現（即使選的標籤不在選項內也看得到）
  const matched = filterTags(options, query).filter((t) => t.topic !== value);
  const searching = normalizeTagQuery(query) !== '';

  return (
    <div class="lv-tagfilter" role="group" aria-label="標籤篩選">
      <div class="lv-chips lv-tagfilter__pinned">
        <span class="lv-filters__label">TAGS</span>
        <input
          type="search"
          class="lv-input lv-tagfilter__search"
          value={query}
          placeholder="搜尋標籤…"
          aria-label="搜尋標籤"
          data-testid="tag-search"
          onInput={(e) => setQuery((e.currentTarget as HTMLInputElement).value)}
        />
        <button type="button" class={'lv-chip lv-chip--mono' + (value === null ? ' is-on' : '')} aria-pressed={value === null} onClick={() => onChange(null)}>
          全部
        </button>
        {value !== null && (
          <button
            type="button"
            class="lv-chip lv-chip--mono is-on"
            aria-pressed="true"
            aria-label={selected ? `#${selected.topic}（${selected.count} 則）` : undefined}
            data-testid="tag-selected"
            onClick={() => onChange(null)}
          >
            #{value}
            {selected && (
              <span class="lv-chip__count" aria-hidden="true">
                {selected.count}
              </span>
            )}
          </button>
        )}
      </div>
      {matched.length > 0 && (
        <div class="lv-chips lv-tagfilter__list" data-testid="tag-list">
          {matched.map((t) => (
            <button
              key={t.topic}
              type="button"
              class="lv-chip lv-chip--mono"
              aria-pressed="false"
              aria-label={`#${t.topic}（${t.count} 則）`}
              onClick={() => onChange(t.topic)}
            >
              #{t.topic}
              <span class="lv-chip__count" aria-hidden="true">
                {t.count}
              </span>
            </button>
          ))}
        </div>
      )}
      {searching && matched.length === 0 && (
        <p class="lv-tagfilter__empty" role="status" data-testid="tag-empty">
          沒有符合的 tag
        </p>
      )}
    </div>
  );
}
