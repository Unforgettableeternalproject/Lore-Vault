// App Shell（T-78 骨架）：頂列（品牌、space 切換、連線狀態、降級徽章、深淺色、登出）＋
// 側欄（8 項導覽、vault 列表區、目前寫入位置）＋ 主內容（各畫面目前為佔位）。
import { useState } from 'preact/hooks';

import type { ApiClient, Notice } from '../lib/api';
import { saveSpace, loadSpace, type Theme } from '../lib/prefs';
import { SCREENS, useScreen, type ScreenId } from '../lib/router';
import { SPACES, type SpaceId } from '../lib/spaces';
import { Placeholder } from '../screens/Placeholder';
import { SpaceSwitcher } from './SpaceSwitcher';

interface Props {
  api: ApiClient;
  theme: Theme;
  onToggleTheme: () => void;
  degraded: Notice | null;
  onLogout: () => void;
}

const SCREEN_META: Record<ScreenId, { eyebrow: string; title: string; card: string }> = {
  search: { eyebrow: 'RECALL', title: '檢索', card: 'T-79' },
  notes: { eyebrow: 'NOTES', title: '筆記', card: 'T-80' },
  docs: { eyebrow: 'DOCUMENTS', title: '文件', card: 'T-81' },
  memory: { eyebrow: 'MEMORY', title: '記憶層', card: 'T-84' },
  vaults: { eyebrow: 'VAULTS', title: 'Vault', card: 'T-82' },
  maint: { eyebrow: 'MAINTENANCE', title: '維護', card: 'T-82' },
  health: { eyebrow: 'HEALTH', title: '系統健康', card: 'T-83' },
  settings: { eyebrow: 'CONNECTION', title: '連線設定', card: 'T-85' },
};

export function Shell({ theme, onToggleTheme, degraded, onLogout }: Props) {
  const [screen, go] = useScreen();
  const [spaceId, setSpaceId] = useState<SpaceId>(loadSpace);
  const space = SPACES[spaceId];
  const meta = SCREEN_META[screen];

  const pickSpace = (next: SpaceId) => {
    setSpaceId(next);
    saveSpace(next);
  };

  return (
    <div class="lv-app" data-zone={space.zone}>
      <header class="lv-header">
        <div class="lv-header__zone-bar" aria-hidden="true" />
        <a class="uep-topbar__brand" href="/ui/search" onClick={(e) => { e.preventDefault(); go('search'); }}>
          <div class="uep-brand-mark lv-brand__mark">L</div>
          <div>
            <div class="uep-brand-title">Lore Vault</div>
            <div class="uep-brand-subtitle">PM · 紀錄與記憶層</div>
          </div>
        </a>
        <SpaceSwitcher current={spaceId} onPick={pickSpace} />
        <div class="lv-header__spacer" />
        <button type="button" class="lv-conn" onClick={() => go('settings')} title="連線設定">
          <span class="lv-conn__dot" aria-hidden="true" />
          {window.location.host}
        </button>
        {degraded && (
          <button
            type="button"
            class="lv-degraded-badge"
            onClick={() => go('health')}
            title={describeDegraded(degraded)}
          >
            語意檢索離線
          </button>
        )}
        <button
          type="button"
          class="btn-outline btn-outline--sm lv-icon-btn"
          onClick={onToggleTheme}
          aria-label={theme === 'dark' ? '切換為淺色' : '切換為深色'}
        >
          {theme === 'dark' ? '☀' : '☾'}
        </button>
        <button type="button" class="btn-outline btn-outline--sm" onClick={onLogout}>
          登出
        </button>
      </header>

      <div class="lv-body">
        <aside class="lv-sidebar">
          <nav class="lv-nav" aria-label="主導覽">
            {SCREENS.map((s) => (
              <a
                key={s.id}
                href={`/ui/${s.id}`}
                class={'lv-nav__item' + (s.id === screen ? ' is-active' : '')}
                aria-current={s.id === screen ? 'page' : undefined}
                onClick={(e) => {
                  e.preventDefault();
                  go(s.id);
                }}
              >
                <span class="lv-nav__glyph" aria-hidden="true">
                  {s.glyph}
                </span>
                <span class="lv-nav__label">{s.label}</span>
              </a>
            ))}
          </nav>
          <div class="lv-vaults">
            <div class="lv-vaults__head">
              <span>VAULTS · {space.en}</span>
            </div>
            <p class="lv-vaults__empty">vault 列表待 T-70（vault_list）與 T-78 接上。</p>
          </div>
          <div class="lv-write-target">
            <div class="lv-write-target__label">目前寫入位置</div>
            <div class="lv-write-target__value">{space.en} / 本 space 全部</div>
          </div>
        </aside>

        <main class="lv-main">
          <Placeholder eyebrow={meta.eyebrow} title={meta.title} space={space} card={meta.card} />
        </main>
      </div>
    </div>
  );
}

function describeDegraded(notice: Notice): string {
  const detail = notice.detail as { reason?: unknown } | undefined;
  return typeof detail?.reason === 'string' ? `降級：${detail.reason}` : '降級：只走關鍵字檢索';
}
