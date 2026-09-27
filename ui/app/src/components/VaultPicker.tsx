// 頁面內的 vault 篩選器：可搜尋的下拉（APG combobox：input 為主體 + listbox 彈出）。
// 狀態就是 AppEnv 的 vault／setVault，與側欄 vault 列表共用同一份，兩處切換結果一致。
// 以輸入框為主體：全域快捷鍵在焦點於輸入框時不觸發，打字搜尋不會誤觸單鍵快捷鍵。
import { useMemo, useRef, useState } from 'preact/hooks';

import { ALL, useApp, vaultName } from '../lib/context';
import type { VaultSummary } from '../lib/types';

interface Option {
  key: string;
  label: string;
  sub: string | null;
  count: string | null;
}

let seq = 0;

function toOption(v: VaultSummary): Option {
  const docs = typeof v.document_count === 'number' ? ` · ${v.document_count} 文件` : '';
  return { key: v.key, label: v.display, sub: v.key, count: `${v.note_count} 筆記${docs}` };
}

export function VaultPicker({ class: className }: { class?: string }) {
  const { vault, setVault, vaults, space } = useApp();
  const ids = useRef(`lv-vpick-${++seq}`).current;
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);

  const all: Option = { key: ALL, label: '本 space 全部', sub: `${space.en} 的所有 vault`, count: null };
  const options = useMemo(() => {
    const q = query.trim().toLowerCase();
    const items = vaults.items.map(toOption);
    if (!q) return [all, ...items];
    const hit = (o: Option) => o.label.toLowerCase().includes(q) || (o.sub ?? '').toLowerCase().includes(q);
    return [all, ...items].filter((o) => (o.key === ALL ? '本 space 全部 all *'.includes(q) || hit(o) : hit(o)));
    // all 依 space 變動；space 變了 vaults 也會重取
  }, [query, vaults.items, space.en]);

  const current = vaultName({ vaults }, vault);

  const show = () => {
    if (open) return;
    setQuery('');
    const idx = [all, ...vaults.items.map(toOption)].findIndex((o) => o.key === vault);
    setActive(idx < 0 ? 0 : idx);
    setOpen(true);
  };
  const close = () => {
    setOpen(false);
    setQuery('');
  };
  const pick = (o: Option | undefined) => {
    if (!o) return;
    setVault(o.key);
    close();
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.isComposing) return;
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        if (!open) show();
        else setActive((i) => Math.min(options.length - 1, i + 1));
        break;
      case 'ArrowUp':
        e.preventDefault();
        if (!open) show();
        else setActive((i) => Math.max(0, i - 1));
        break;
      case 'Home':
        if (open) {
          e.preventDefault();
          setActive(0);
        }
        break;
      case 'End':
        if (open) {
          e.preventDefault();
          setActive(options.length - 1);
        }
        break;
      case 'Enter':
        if (open) {
          e.preventDefault();
          pick(options[active]);
        }
        break;
      case 'Escape':
        if (open) {
          // 只關閉下拉，不讓外層（抽屜、對話框）一起收起
          e.preventDefault();
          e.stopPropagation();
          close();
        }
        break;
      case 'Tab':
        close();
        break;
    }
  };

  const listId = `${ids}-list`;
  const optId = (i: number) => `${ids}-opt-${i}`;
  const activeOpt = open && options[active] ? optId(active) : undefined;

  return (
    <div class={'lv-vpick' + (open ? ' is-open' : '') + (className ? ` ${className}` : '')} data-testid="vault-picker">
      <label class="lv-filters__label" for={`${ids}-input`}>
        VAULT
      </label>
      <div class="lv-vpick__box">
        <input
          ref={inputRef}
          id={`${ids}-input`}
          class="lv-vpick__input"
          type="text"
          role="combobox"
          aria-label="vault 篩選"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeOpt}
          autoComplete="off"
          spellcheck={false}
          placeholder={open ? '搜尋名稱或 key…' : current}
          value={open ? query : current}
          title={vault === ALL ? undefined : vault}
          onFocus={show}
          onClick={show}
          onBlur={close}
          onInput={(e) => {
            setQuery((e.target as HTMLInputElement).value);
            setActive(0);
            if (!open) setOpen(true);
          }}
          onKeyDown={onKeyDown}
        />
        <span class="lv-vpick__caret" aria-hidden="true">
          ▾
        </span>
        {open && (
          <ul class="lv-vpick__list" id={listId} role="listbox" aria-label={`${space.en} 的 vault`}>
            {options.length === 0 && (
              <li class="lv-vpick__empty" role="presentation">
                沒有符合「{query}」的 vault
              </li>
            )}
            {options.map((o, i) => (
              <li
                key={o.key}
                id={optId(i)}
                role="option"
                aria-selected={o.key === vault}
                class={'lv-vpick__opt' + (i === active ? ' is-active' : '') + (o.key === vault ? ' is-current' : '')}
                // mousedown 預設會讓輸入框失焦、先關閉清單；擋掉才點得到
                onMouseDown={(e) => e.preventDefault()}
                onMouseMove={() => setActive(i)}
                onClick={() => pick(o)}
              >
                <span class="lv-vpick__opt-main">
                  <span class="lv-vpick__opt-label">{o.label}</span>
                  {o.sub && <span class="lv-vpick__opt-sub">{o.sub}</span>}
                </span>
                {o.count && <span class="lv-vpick__opt-count">{o.count}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
