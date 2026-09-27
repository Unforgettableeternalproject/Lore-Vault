// 頂列的 space 切換（設計稿 header 下拉）：切換是明確動作，目前 space 時時可見。
import { useEffect, useRef, useState } from 'preact/hooks';

import { SPACE_IDS, SPACES, type SpaceId } from '../lib/spaces';

interface Props {
  current: SpaceId;
  onPick: (space: SpaceId) => void;
}

export function SpaceSwitcher({ current, onPick }: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const sp = SPACES[current];

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div class="lv-space" ref={root}>
      <button
        type="button"
        class="lv-space__trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span class="lv-space__glyph" aria-hidden="true">
          {sp.glyph}
        </span>
        <span class="lv-space__text">
          <span class="lv-space__kicker">SPACE</span>
          <span class="lv-space__name">
            {sp.en} · {sp.name}
          </span>
        </span>
        <span class="lv-space__hint">切換 ▾</span>
      </button>
      {open && (
        <div class="lv-space__menu" role="menu">
          <div class="lv-space__menu-head">切換領域 · 所有列表會重新載入</div>
          {SPACE_IDS.map((id) => {
            const s = SPACES[id];
            return (
              <button
                key={id}
                type="button"
                role="menuitemradio"
                aria-checked={id === current}
                data-zone={s.zone}
                class={'lv-space__option' + (id === current ? ' is-current' : '')}
                onClick={() => {
                  setOpen(false);
                  if (id !== current) onPick(id);
                }}
              >
                <span class="lv-space__glyph lv-space__glyph--lg" aria-hidden="true">
                  {s.glyph}
                </span>
                <span class="lv-space__option-text">
                  <span class="lv-space__option-name">
                    {s.en} · {s.name}
                  </span>
                  <span class="lv-space__option-desc">{s.desc}</span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
