// 登入頁：帳號＋密碼（A23，DB 內的 UI 帳號）換 HttpOnly session cookie。
// 密碼只在送出時放進請求 body，不寫進任何瀏覽器儲存。全域失敗 3 次即鎖定、需在主機人工解鎖；
// 尚未設定帳號時直接顯示在主機設定的指令，不讓人亂猜。
import { useEffect, useState } from 'preact/hooks';

import type { ApiClient } from '../lib/api';
import {
  describeLoginError,
  LOCKED_MESSAGE,
  login,
  loginErrorDetail,
  loginState,
  type LoginState,
} from '../lib/session';

interface Props {
  api: ApiClient;
  expired: boolean;
  onLoggedIn: () => void;
}

export function Login({ api, expired, onLoggedIn }: Props) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [state, setState] = useState<LoginState | null>(null);

  useEffect(() => {
    let alive = true;
    loginState(api)
      .then((s) => {
        if (alive) setState(s);
      })
      .catch(() => {
        // 取不到狀態不擋登入：送出時服務端仍會回報鎖定或無帳號
      });
    return () => {
      alive = false;
    };
  }, [api]);

  const noAccount = state !== null && !state.account_configured;
  const locked = state?.locked === true;

  const submit = async (event: Event) => {
    event.preventDefault();
    if (!username || !password || busy || locked || noAccount) return;
    setBusy(true);
    setError(null);
    try {
      await login(api, username, password);
      setPassword('');
      onLoggedIn();
    } catch (err) {
      setPassword('');
      setError(describeLoginError(err));
      const detail = loginErrorDetail(err);
      if (detail.locked) setState((s) => (s ? { ...s, locked: true, remaining: 0 } : s));
      else if (detail.remaining !== null) setState((s) => (s ? { ...s, remaining: detail.remaining! } : s));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="lv-login" data-zone="concepts">
      <form class="lv-login__card" onSubmit={submit} aria-labelledby="lv-login-title">
        <div class="lv-brand">
          <div class="uep-brand-mark lv-brand__mark">L</div>
          <div>
            <div class="uep-brand-title">Lore Vault</div>
            <div class="uep-brand-subtitle">PM · 紀錄與記憶層</div>
          </div>
        </div>
        <div class="lv-eyebrow">SIGN IN</div>
        <h1 id="lv-login-title" class="lv-title">登入</h1>
        {expired && (
          <p class="lv-notice lv-notice--warn" role="status">
            登入已過期或已在其他地方登出，請重新登入。
          </p>
        )}
        {noAccount && (
          <div class="lv-notice lv-notice--warn" role="status" data-testid="login-no-account">
            <p>尚未設定帳號，請在主機執行：</p>
            <code class="lv-mono">{state?.setup_command}</code>
          </div>
        )}
        {locked && !error && (
          <p class="lv-notice lv-notice--error" role="alert">
            {LOCKED_MESSAGE}
          </p>
        )}
        <label class="lv-field">
          <span class="lv-field__label">帳號</span>
          <input
            class="lv-input"
            type="text"
            name="username"
            autoComplete="username"
            autoCapitalize="none"
            spellcheck={false}
            autoFocus
            required
            disabled={noAccount}
            value={username}
            onInput={(e) => setUsername((e.target as HTMLInputElement).value)}
          />
        </label>
        <label class="lv-field">
          <span class="lv-field__label">密碼</span>
          <input
            class="lv-input"
            type="password"
            name="password"
            autoComplete="current-password"
            required
            disabled={noAccount}
            value={password}
            onInput={(e) => setPassword((e.target as HTMLInputElement).value)}
          />
        </label>
        {error && (
          <p class="lv-notice lv-notice--error" role="alert">
            {error}
          </p>
        )}
        <div class="lv-actions">
          <button
            type="submit"
            class="btn-outline btn-outline--gold"
            disabled={busy || !username || !password || locked || noAccount}
          >
            {busy ? '登入中…' : '登入'}
          </button>
        </div>
        <p class="lv-hint">
          {state && !noAccount && !locked
            ? `全域失敗 ${state.max_failures} 次即鎖定、需人工解鎖（目前剩餘 ${state.remaining} 次）。`
            : '全域失敗 3 次即鎖定、需人工解鎖。'}
          登入後以 HttpOnly cookie 維持，瀏覽器不保存密碼。
        </p>
      </form>
    </div>
  );
}
