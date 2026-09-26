// App Shell：頂列（品牌、space 切換、連線狀態、降級徽章、深淺色、登出）＋
// 側欄（8 項導覽、目前 space 的 vault 列表篩選、目前寫入位置）＋ 主內容（依路由切換畫面）。
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';

import type { ApiClient, Notice } from '../lib/api';
import { ALL, AppContext, type AppEnv, type ToastKind, type VaultsState } from '../lib/context';
import { describeError } from '../lib/format';
import { loadSpace, saveSpace, type Theme } from '../lib/prefs';
import { SCREENS, routePath, useRoute, type ScreenId } from '../lib/router';
import { SPACES, type SpaceId } from '../lib/spaces';
import type { VaultSummary } from '../lib/types';
import { DocDetail } from '../screens/DocDetail';
import { Docs } from '../screens/Docs';
import { NoteDetail } from '../screens/NoteDetail';
import { NoteNew } from '../screens/NoteNew';
import { Notes } from '../screens/Notes';
import { Placeholder } from '../screens/Placeholder';
import { Search } from '../screens/Search';
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

interface ToastItem {
  id: number;
  message: string;
  kind: ToastKind;
}

const TOAST_ICON: Record<ToastKind, string> = { success: '✓', error: '✕', warning: '!', info: 'i' };

export function Shell({ api, theme, onToggleTheme, degraded, onLogout }: Props) {
  const [route, navigate] = useRoute();
  const [spaceId, setSpaceId] = useState<SpaceId>(loadSpace);
  const [vault, setVault] = useState<string>(ALL);
  const [vaults, setVaults] = useState<VaultsState>({ items: [], loading: true, error: null });
  const [vaultsTick, setVaultsTick] = useState(0);
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const toastSeq = useRef(0);
  const space = SPACES[spaceId];
  const screen = route.screen;

  useEffect(() => {
    const ctrl = new AbortController();
    setVaults((v) => ({ ...v, loading: true, error: null }));
    api
      .post<{ vaults: VaultSummary[] }>('/v1/vault_list', { space: spaceId }, ctrl.signal)
      .then(({ data }) => setVaults({ items: data.vaults, loading: false, error: null }))
      .catch((err) => {
        if (ctrl.signal.aborted) return;
        setVaults({ items: [], loading: false, error: describeError(err) });
      });
    return () => ctrl.abort();
  }, [api, spaceId, vaultsTick]);

  const toast = useCallback((message: string, kind: ToastKind = 'success') => {
    const id = ++toastSeq.current;
    setToasts((t) => [...t, { id, message, kind }]);
    // 錯誤與警示停留較久；都可手動關閉
    const ttl = kind === 'error' || kind === 'warning' ? 9000 : 4000;
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), ttl);
  }, []);

  const pickSpace = (next: SpaceId) => {
    setSpaceId(next);
    saveSpace(next);
    setVault(ALL);
    // 詳情頁的項目屬於舊 space：回到列表
    if (route.params.length > 0) navigate(routePath(screen));
    toast(`已切換至 ${SPACES[next].en} · ${SPACES[next].name}`, 'info');
  };

  const env: AppEnv = useMemo(
    () => ({
      api,
      space,
      vaults,
      vault,
      setVault,
      refreshVaults: () => setVaultsTick((t) => t + 1),
      navigate,
      toast,
    }),
    // navigate 每次 render 都是新函式，但行為不變；不列入以免畫面重掛
    [api, space, vaults, vault, toast],
  );

  const vaultLabel =
    vault === ALL ? '本 space 全部' : (vaults.items.find((v) => v.key === vault)?.display ?? vault);

  return (
    <AppContext.Provider value={env}>
      <div class="lv-app" data-zone={space.zone}>
        <a class="lv-skip" href="#lv-main">
          跳到主內容
        </a>
        <header class="lv-header">
          <div class="lv-header__zone-bar" aria-hidden="true" />
          <a
            class="uep-topbar__brand"
            href="/ui/search"
            onClick={(e) => {
              e.preventDefault();
              navigate(routePath('search'));
            }}
          >
            <div class="uep-brand-mark lv-brand__mark">L</div>
            <div>
              <div class="uep-brand-title">Lore Vault</div>
              <div class="uep-brand-subtitle">PM · 紀錄與記憶層</div>
            </div>
          </a>
          <SpaceSwitcher current={spaceId} onPick={pickSpace} />
          <div class="lv-header__spacer" />
          <button type="button" class="lv-conn" onClick={() => navigate(routePath('settings'))} title="連線設定">
            <span class="lv-conn__dot" aria-hidden="true" />
            {window.location.host}
          </button>
          {degraded && (
            <button
              type="button"
              class="lv-degraded-badge"
              onClick={() => navigate(routePath('health'))}
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
                    navigate(routePath(s.id));
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
                <span id="lv-vaults-label">VAULTS · {space.en}</span>
              </div>
              {vaults.loading && <p class="lv-vaults__empty">載入中…</p>}
              {vaults.error && (
                <p class="lv-vaults__empty lv-vaults__error" role="alert">
                  vault 列表載入失敗：{vaults.error}
                  <button type="button" class="lv-link-btn" onClick={() => setVaultsTick((t) => t + 1)}>
                    重試
                  </button>
                </p>
              )}
              {!vaults.loading && !vaults.error && vaults.items.length === 0 && (
                <p class="lv-vaults__empty">這個 space 還沒有 vault。</p>
              )}
              {vaults.items.length > 0 && (
                <ul class="lv-vaults__list" aria-labelledby="lv-vaults-label">
                  <li>
                    <button
                      type="button"
                      class={'lv-vaults__item' + (vault === ALL ? ' is-active' : '')}
                      aria-pressed={vault === ALL}
                      onClick={() => setVault(ALL)}
                    >
                      <span class="lv-vaults__name">本 space 全部</span>
                    </button>
                  </li>
                  {vaults.items.map((v) => (
                    <li key={v.key}>
                      <button
                        type="button"
                        class={'lv-vaults__item' + (vault === v.key ? ' is-active' : '')}
                        aria-pressed={vault === v.key}
                        title={v.key}
                        onClick={() => setVault(vault === v.key ? ALL : v.key)}
                      >
                        <span class="lv-vaults__name">{v.display}</span>
                        <span class="lv-vaults__count">
                          {v.note_count}
                          {typeof v.document_count === 'number' ? ` · ${v.document_count}` : ''}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div class="lv-write-target">
              <div class="lv-write-target__label">目前寫入位置</div>
              <div class="lv-write-target__value">
                {space.en} / {vaultLabel}
              </div>
              {vault === ALL && <div class="lv-write-target__hint">寫入與上傳前需選定單一 vault</div>}
            </div>
          </aside>

          <main class="lv-main" id="lv-main" tabIndex={-1}>
            <ScreenView screen={screen} params={route.params} query={route.query} spaceKey={spaceId} />
          </main>
        </div>

        <div class="uep-toast-container lv-toasts" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} class={`uep-toast uep-toast--${t.kind}`} role={t.kind === 'error' ? 'alert' : 'status'}>
              <span class="uep-toast__icon" aria-hidden="true">
                {TOAST_ICON[t.kind]}
              </span>
              <p class="uep-toast__msg">{t.message}</p>
              <button
                type="button"
                class="uep-toast__close"
                aria-label="關閉通知"
                onClick={() => setToasts((all) => all.filter((x) => x.id !== t.id))}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      </div>
    </AppContext.Provider>
  );
}

function ScreenView({
  screen,
  params,
  query,
  spaceKey,
}: {
  screen: ScreenId;
  params: string[];
  query: URLSearchParams;
  spaceKey: SpaceId;
}) {
  // key 帶 space：切換 space 時畫面重新掛載，不殘留上一個 space 的資料
  switch (screen) {
    case 'search':
      return <Search key={spaceKey} initialQuery={query.get('q') ?? ''} />;
    case 'notes':
      if (params[0] === 'new') return <NoteNew key={spaceKey} supersedes={query.get('supersedes')} />;
      if (params[0]) return <NoteDetail key={`${spaceKey}:${params[0]}`} id={params[0]} />;
      return <Notes key={spaceKey} />;
    case 'docs':
      if (params[0]) {
        const chunk = query.get('chunk');
        return (
          <DocDetail
            key={`${spaceKey}:${params[0]}`}
            id={params[0]}
            chunk={chunk !== null && /^\d+$/.test(chunk) ? Number(chunk) : null}
            fromQuery={query.get('q')}
          />
        );
      }
      return <Docs key={spaceKey} />;
    default: {
      const meta = SCREEN_META[screen];
      return <Placeholder eyebrow={meta.eyebrow} title={meta.title} space={SPACES[spaceKey]} card={meta.card} />;
    }
  }
}

function describeDegraded(notice: Notice): string {
  const detail = notice.detail as { reason?: unknown } | undefined;
  return typeof detail?.reason === 'string' ? `降級：${detail.reason}` : '降級：只走關鍵字檢索';
}
