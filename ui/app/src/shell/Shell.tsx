// App Shell：頂列（品牌、space 切換、連線狀態、降級徽章、深淺色、登出）＋
// 側欄（8 項導覽、目前 space 的 vault 列表篩選、目前寫入位置）＋ 主內容（依路由切換畫面）。
// 窄螢幕（≤760px，T-86）側欄收合成抽屜：頂列的選單鈕開關，深淺色／快捷鍵／登出移進抽屜底部。
// 全站快捷鍵（T-87）見 lib/shortcuts.ts；`?` 開說明面板。
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';

import { Dialog, EmptyState } from '../components/ui';
import type { ApiClient, Notice } from '../lib/api';
import { ALL, AppContext, type AppEnv, type HealthBadge, type ToastKind, type VaultsState } from '../lib/context';
import { describeError, recallDegradedBadge } from '../lib/format';
import { healthBadge } from '../lib/health';
import { loadSpace, loadVaultsCollapsed, saveSpace, saveVaultsCollapsed, type Theme } from '../lib/prefs';
import { SCREENS, routePath, useRoute, type ScreenId } from '../lib/router';
import { createShortcutHandler, SHORTCUTS } from '../lib/shortcuts';
import { SPACES, type SpaceId } from '../lib/spaces';
import type { SessionLimits, StatusResult, VaultSummary } from '../lib/types';
import { DocDetail } from '../screens/DocDetail';
import { Docs } from '../screens/Docs';
import { Health } from '../screens/Health';
import { Maint } from '../screens/Maint';
import { Memory } from '../screens/Memory';
import { NoteDetail } from '../screens/NoteDetail';
import { NoteNew } from '../screens/NoteNew';
import { Notes } from '../screens/Notes';
import { Search, SEARCH_INPUT_ID } from '../screens/Search';
import { Settings } from '../screens/Settings';
import { Vaults } from '../screens/Vaults';
import { SpaceSwitcher } from './SpaceSwitcher';

interface Props {
  api: ApiClient;
  /** 登入帳號與顯示名稱（取自 /ui/api/session） */
  principal: string;
  author: string;
  limits: SessionLimits;
  theme: Theme;
  onToggleTheme: () => void;
  degraded: Notice | null;
  onLogout: () => void;
}

interface ToastItem {
  id: number;
  message: string;
  kind: ToastKind;
}

const TOAST_ICON: Record<ToastKind, string> = { success: '✓', error: '✕', warning: '!', info: 'i' };

export function Shell({ api, principal, author, limits, theme, onToggleTheme, degraded, onLogout }: Props) {
  const [route, navigate] = useRoute();
  const [spaceId, setSpaceId] = useState<SpaceId>(loadSpace);
  const [vault, setVault] = useState<string>(ALL);
  const [vaults, setVaults] = useState<VaultsState>({ items: [], loading: true, error: null });
  const [vaultsTick, setVaultsTick] = useState(0);
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const toastSeq = useRef(0);
  const [health, setHealth] = useState<HealthBadge | null>(null);
  const space = SPACES[spaceId];
  const screen = route.screen;
  // 窄螢幕的側欄抽屜與快捷鍵說明
  const [navOpen, setNavOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  // 側欄 vault 區段：可收合（記住狀態），展開時限高捲動，讓下方寫入位置留在首屏
  const [vaultsCollapsed, setVaultsCollapsed] = useState(loadVaultsCollapsed);
  const vaultList = useRef<HTMLUListElement>(null);
  const toggleVaults = () =>
    setVaultsCollapsed((c) => {
      saveVaultsCollapsed(!c);
      return !c;
    });
  const menuBtn = useRef<HTMLButtonElement>(null);
  const sidebar = useRef<HTMLElement>(null);

  // 換頁即收起抽屜
  useEffect(() => setNavOpen(false), [route]);

  // 抽屜開啟：焦點移入；Esc 關閉並把焦點還給選單鈕
  useEffect(() => {
    if (!navOpen) return;
    sidebar.current?.querySelector<HTMLElement>('a, button')?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setNavOpen(false);
        menuBtn.current?.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [navOpen]);

  // 快捷鍵：處理器只建一次，動作透過 ref 取最新的導覽狀態
  const shortcutActions = useRef({ focusSearch: () => {}, goto: (_s: ScreenId) => {}, openHelp: () => {} });
  shortcutActions.current = {
    focusSearch: () => {
      const focus = () => document.getElementById(SEARCH_INPUT_ID)?.focus();
      if (screen === 'search' && route.params.length === 0) focus();
      else {
        navigate(routePath('search'));
        window.setTimeout(focus, 0);
      }
    },
    goto: (target) => navigate(routePath(target)),
    openHelp: () => setHelpOpen(true),
  };
  useEffect(() => {
    const handle = createShortcutHandler({
      focusSearch: () => shortcutActions.current.focusSearch(),
      goto: (target) => shortcutActions.current.goto(target),
      openHelp: () => shortcutActions.current.openHelp(),
    });
    const onKey = (e: KeyboardEvent) => void handle(e);
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

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

  // 頂列／側欄的健檢徽章：登入後取一次，之後由系統健康頁重新整理時同步
  useEffect(() => {
    const ctrl = new AbortController();
    api
      .post<StatusResult>('/v1/status', { space: spaceId }, ctrl.signal)
      .then(({ data }) => setHealth(healthBadge(data)))
      .catch((err) => {
        if (ctrl.signal.aborted) return;
        setHealth({ ok: false, fail: 0, warn: 0, checkedAt: null, error: describeError(err) });
      });
    return () => ctrl.abort();
    // doctor 為全域對帳，不隨 space 重取
  }, [api]);

  const toast = useCallback((message: string, kind: ToastKind = 'success') => {
    const id = ++toastSeq.current;
    setToasts((t) => [...t, { id, message, kind }]);
    // 錯誤與警示停留較久；都可手動關閉
    const ttl = kind === 'error' || kind === 'warning' ? 9000 : 4000;
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), ttl);
  }, []);

  const switchSpace = (next: SpaceId, path?: string) => {
    setSpaceId(next);
    saveSpace(next);
    setVault(ALL);
    // 詳情頁的項目屬於舊 space：回到列表（或呼叫端指定的位置）
    if (path) navigate(path);
    else if (route.params.length > 0) navigate(routePath(screen));
    toast(`已切換至 ${SPACES[next].en} · ${SPACES[next].name}`, 'info');
  };

  const env: AppEnv = useMemo(
    () => ({
      api,
      principal,
      author,
      limits,
      space,
      vaults,
      vault,
      setVault,
      refreshVaults: () => setVaultsTick((t) => t + 1),
      navigate,
      toast,
      health,
      reportHealth: setHealth,
      recallDegraded: degraded,
      switchSpace,
    }),
    // navigate／switchSpace 每次 render 都是新函式，但行為不變；不列入以免畫面重掛
    [api, principal, author, limits, space, vaults, vault, toast, health, degraded],
  );

  // 目前選的 vault 捲進可視範圍（限高清單，選到下面的項目時不必自己捲）。
  // 只調整清單自己的 scrollTop：scrollIntoView 會把瀏覽器的鍵盤起點移到該項目，頁面載入後第一個 Tab 就不是「跳到主內容」
  useEffect(() => {
    const list = vaultList.current;
    if (vaultsCollapsed || !list) return;
    const active = list.querySelector<HTMLElement>('.lv-vaults__item.is-active');
    if (!active) return;
    const top = active.offsetTop - list.offsetTop;
    const bottom = top + active.offsetHeight;
    if (top < list.scrollTop) list.scrollTop = top;
    else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
  }, [vault, vaultsCollapsed, vaults.items.length]);

  const degradedBadge = degraded ? recallDegradedBadge(degradedReason(degraded)) : null;

  const vaultLabel =
    vault === ALL ? '本 space 全部' : (vaults.items.find((v) => v.key === vault)?.display ?? vault);

  return (
    <AppContext.Provider value={env}>
      <div class={'lv-app' + (navOpen ? ' is-nav-open' : '')} data-zone={space.zone}>
        <a class="lv-skip" href="#lv-main">
          跳到主內容
        </a>
        <header class="lv-header">
          <div class="lv-header__zone-bar" aria-hidden="true" />
          <button
            ref={menuBtn}
            type="button"
            class="btn-outline btn-outline--sm lv-icon-btn lv-menu-btn"
            aria-expanded={navOpen}
            aria-controls="lv-sidebar"
            aria-label={navOpen ? '關閉導覽選單' : '開啟導覽選單'}
            onClick={() => setNavOpen((o) => !o)}
          >
            <span aria-hidden="true">{navOpen ? '✕' : '☰'}</span>
          </button>
          <a
            class="uep-topbar__brand"
            href="/ui/search"
            onClick={(e) => {
              e.preventDefault();
              navigate(routePath('search'));
            }}
          >
            <div class="uep-brand-mark lv-brand__mark" aria-hidden="true">
              L
            </div>
            <div class="lv-brand__text">
              <div class="uep-brand-title">Lore Vault</div>
              <div class="uep-brand-subtitle">PM · 紀錄與記憶層</div>
            </div>
          </a>
          <SpaceSwitcher current={spaceId} onPick={(next) => switchSpace(next)} />
          <div class="lv-header__spacer" />
          <button type="button" class="lv-conn" onClick={() => navigate(routePath('settings'))} title="連線設定">
            <span class="lv-visually-hidden">連線設定：</span>
            <span class="lv-conn__dot" aria-hidden="true" />
            {window.location.host}
          </button>
          {health && (health.fail > 0 || health.error) && (
            <button
              type="button"
              class="lv-degraded-badge lv-health-badge"
              data-testid="header-health-badge"
              onClick={() => navigate(routePath('health'))}
              title={health.error ?? '系統健檢有失敗項目'}
            >
              {health.error ? '健檢無法取得' : `健檢 ${health.fail} 項失敗`}
            </button>
          )}
          {degradedBadge && (
            <button
              type="button"
              class="lv-degraded-badge"
              data-testid="header-recall-badge"
              onClick={() => navigate(routePath('health'))}
              title={degradedBadge.title}
            >
              {degradedBadge.label}
            </button>
          )}
          <div class="lv-header__tools">
            <button
              type="button"
              class="btn-outline btn-outline--sm lv-icon-btn"
              onClick={() => setHelpOpen(true)}
              aria-label="快捷鍵說明"
              title="快捷鍵說明（?）"
            >
              <span aria-hidden="true">?</span>
            </button>
            <button
              type="button"
              class="btn-outline btn-outline--sm lv-icon-btn"
              onClick={onToggleTheme}
              aria-label={theme === 'dark' ? '切換為淺色' : '切換為深色'}
              title={theme === 'dark' ? '切換為淺色' : '切換為深色'}
            >
              <span aria-hidden="true">{theme === 'dark' ? '☀' : '☾'}</span>
            </button>
            <button
              type="button"
              class="btn-outline btn-outline--sm lv-icon-btn"
              onClick={onLogout}
              aria-label="登出"
              title="登出"
            >
              <LogoutIcon />
            </button>
          </div>
        </header>

        <div class="lv-body">
          {navOpen && (
            <div
              class="lv-scrim"
              aria-hidden="true"
              onClick={() => {
                setNavOpen(false);
                menuBtn.current?.focus();
              }}
            />
          )}
          <aside class="lv-sidebar" id="lv-sidebar" ref={sidebar} aria-label="導覽與 vault">
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
                  {s.id === 'health' && health && health.fail > 0 && (
                    <span class="lv-nav__badge" aria-label={`${health.fail} 項失敗`}>
                      {health.fail}
                    </span>
                  )}
                </a>
              ))}
            </nav>
            <div class="lv-vaults">
              <div class="lv-vaults__head">
                <button
                  type="button"
                  class="lv-vaults__toggle"
                  id="lv-vaults-label"
                  aria-expanded={!vaultsCollapsed}
                  aria-controls="lv-vaults-body"
                  onClick={toggleVaults}
                >
                  <span class="lv-vaults__caret" aria-hidden="true">
                    {vaultsCollapsed ? '▸' : '▾'}
                  </span>
                  VAULTS · {space.en}
                  {vaults.items.length > 0 && <span class="lv-vaults__total">{vaults.items.length}</span>}
                </button>
                {vaultsCollapsed && vault !== ALL && (
                  <span class="lv-vaults__current" title={vault}>
                    {vaultLabel}
                  </span>
                )}
              </div>
              <div id="lv-vaults-body" hidden={vaultsCollapsed}>
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
                <EmptyState size="sm" title="還沒有 vault" />
              )}
              {vaults.items.length > 0 && (
                <ul class="lv-vaults__list" aria-labelledby="lv-vaults-label" ref={vaultList}>
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
            </div>
            <div class="lv-write-target">
              <div class="lv-write-target__label">目前寫入位置</div>
              <div class="lv-write-target__value">
                {space.en} / {vaultLabel}
              </div>
              {vault === ALL && <div class="lv-write-target__hint">寫入與上傳前需選定單一 vault</div>}
            </div>
            {/* 抽屜底部工具：圖示按鈕排成一列，名稱走 aria-label、滑鼠提示走 title */}
            <div class="lv-sidebar__tools" role="group" aria-label="介面工具">
              <button
                type="button"
                class="btn-outline btn-outline--sm lv-icon-btn"
                onClick={onToggleTheme}
                aria-label={theme === 'dark' ? '切換為淺色' : '切換為深色'}
                title={theme === 'dark' ? '切換為淺色' : '切換為深色'}
              >
                <span aria-hidden="true">{theme === 'dark' ? '☀' : '☾'}</span>
              </button>
              <button
                type="button"
                class="btn-outline btn-outline--sm lv-icon-btn"
                onClick={() => setHelpOpen(true)}
                aria-label="快捷鍵說明"
                title="快捷鍵說明（?）"
              >
                <span aria-hidden="true">?</span>
              </button>
              <button type="button" class="btn-outline btn-outline--sm lv-icon-btn" onClick={onLogout} aria-label="登出" title="登出">
                <LogoutIcon />
              </button>
            </div>
          </aside>

          <main class="lv-main" id="lv-main" tabIndex={-1}>
            <ScreenView
              screen={screen}
              params={route.params}
              query={route.query}
              spaceKey={spaceId}
              onLogout={onLogout}
            />
          </main>
        </div>

        {helpOpen && (
          <Dialog
            title="快捷鍵"
            onClose={() => setHelpOpen(false)}
            actions={
              <button type="button" class="uep-dialog__btn uep-dialog__btn--confirm" onClick={() => setHelpOpen(false)}>
                關閉
              </button>
            }
          >
            <p class="lv-muted">焦點在輸入框、正在用輸入法組字或有對話框開著時，快捷鍵不會觸發。</p>
            <dl class="lv-kbd-list" data-testid="shortcut-help">
              {SHORTCUTS.map((s) => (
                <div key={s.keys} class="lv-kbd-list__row">
                  <dt>
                    {s.keys.split(' ').map((k, i) => (
                      <kbd key={i} class="lv-kbd">
                        {k}
                      </kbd>
                    ))}
                  </dt>
                  <dd>{s.label}</dd>
                </div>
              ))}
            </dl>
          </Dialog>
        )}

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
  onLogout,
}: {
  screen: ScreenId;
  params: string[];
  query: URLSearchParams;
  spaceKey: SpaceId;
  onLogout: () => void;
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
    case 'vaults':
      return <Vaults key={spaceKey} />;
    case 'maint':
      return <Maint key={`${spaceKey}:${params[0] ?? ''}`} vaultKey={params[0] ?? null} />;
    case 'health':
      return <Health />;
    case 'memory':
      return <Memory key={spaceKey} />;
    case 'settings':
      return <Settings onLogout={onLogout} />;
  }
}

/** 登出圖示（門＋向外箭頭）；沒有合適的字型符號，用 inline SVG（CSP 只禁 style 屬性，不禁 SVG 元素） */
function LogoutIcon() {
  return (
    <svg class="lv-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">
      <path d="M6 2.5H3.5a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1H6" fill="none" stroke="currentColor" stroke-width="1.4" />
      <path d="M10 5l3 3-3 3M13 8H6.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="square" />
    </svg>
  );
}

function degradedReason(notice: Notice): string | null {
  const detail = notice.detail as { reason?: unknown } | undefined;
  return typeof detail?.reason === 'string' ? detail.reason : null;
}
