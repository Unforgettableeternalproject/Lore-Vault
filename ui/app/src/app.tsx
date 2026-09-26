// 根元件：檢查 session → 登入頁或 App Shell。任何 API 回 401 都回到登入頁。
import { useEffect, useMemo, useState } from 'preact/hooks';

import { createApiClient, type Notice } from './lib/api';
import { loadTheme, saveTheme, type Theme } from './lib/prefs';
import { checkSession, logout, type SessionInfo } from './lib/session';
import { Login } from './screens/Login';
import { Shell } from './shell/Shell';

type AuthState = 'checking' | 'anonymous' | 'authenticated' | 'error';

export function App() {
  const [auth, setAuth] = useState<AuthState>('checking');
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [expired, setExpired] = useState(false);
  const [theme, setTheme] = useState<Theme>(loadTheme);
  // 依端點記錄最近一次回應是否降級；任一為真就在 header 常駐徽章
  const [degradedByPath, setDegradedByPath] = useState<Record<string, Notice | null>>({});

  const api = useMemo(
    () =>
      createApiClient({
        onUnauthorized: () => {
          setAuth((prev) => {
            if (prev === 'authenticated') setExpired(true);
            return 'anonymous';
          });
        },
        onNotices: (notices, path) => {
          const degraded = notices.find((n) => n.kind === 'degraded') ?? null;
          setDegradedByPath((prev) =>
            (prev[path] ?? null) === degraded ? prev : { ...prev, [path]: degraded },
          );
        },
      }),
    [],
  );

  useEffect(() => {
    // color-scheme 由 app.css 依 data-theme 設定（不寫行內樣式）
    document.documentElement.dataset.theme = theme;
    saveTheme(theme);
  }, [theme]);

  // 取 session（登入帳號與顯示名稱）；登入成功後也走這裡，署名一律用服務回報的顯示名稱
  const refreshSession = () =>
    checkSession(api)
      .then((info) => {
        setSession(info);
        setAuth(info ? 'authenticated' : 'anonymous');
      })
      .catch(() => setAuth('error'));

  useEffect(() => {
    void refreshSession();
  }, [api]);

  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'));
  const degraded = Object.values(degradedByPath).find((n) => n !== null) ?? null;

  if (auth === 'checking') {
    return <div class="lv-boot" role="status">正在確認登入狀態…</div>;
  }
  if (auth === 'error') {
    return (
      <div class="lv-boot lv-boot--error" role="alert">
        無法連線到 Lore Vault 服務。
        <button type="button" class="btn-outline btn-outline--sm" onClick={() => location.reload()}>
          重新載入
        </button>
      </div>
    );
  }
  if (auth === 'anonymous') {
    return (
      <Login
        api={api}
        expired={expired}
        onLoggedIn={() => {
          setExpired(false);
          setAuth('checking');
          void refreshSession();
        }}
      />
    );
  }
  return (
    <Shell
      api={api}
      principal={session?.principal ?? ''}
      author={session?.display_name ?? session?.principal ?? ''}
      theme={theme}
      onToggleTheme={toggleTheme}
      degraded={degraded}
      onLogout={async () => {
        try {
          await logout(api);
        } finally {
          // 登出請求失敗也回登入頁；服務端 session 仍會依期限失效
          setDegradedByPath({});
          setAuth('anonymous');
        }
      }}
    />
  );
}
