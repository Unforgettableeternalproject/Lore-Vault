// 登入頁：存取金鑰（= 服務的 LORE_VAULT_API_TOKEN）換 HttpOnly session cookie。
// 金鑰只在送出時放進請求 body，不寫進任何瀏覽器儲存。
import { useState } from 'preact/hooks';

import type { ApiClient } from '../lib/api';
import { describeLoginError, login } from '../lib/session';

interface Props {
  api: ApiClient;
  expired: boolean;
  onLoggedIn: () => void;
}

export function Login({ api, expired, onLoggedIn }: Props) {
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: Event) => {
    event.preventDefault();
    if (!key || busy) return;
    setBusy(true);
    setError(null);
    try {
      await login(api, key);
      setKey('');
      onLoggedIn();
    } catch (err) {
      setError(describeLoginError(err));
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
        <label class="lv-field">
          <span class="lv-field__label">存取金鑰</span>
          <input
            class="lv-input"
            type="password"
            name="key"
            autoComplete="current-password"
            autoFocus
            required
            value={key}
            onInput={(e) => setKey((e.target as HTMLInputElement).value)}
          />
        </label>
        {error && (
          <p class="lv-notice lv-notice--error" role="alert">
            {error}
          </p>
        )}
        <div class="lv-actions">
          <button type="submit" class="btn-outline btn-outline--gold" disabled={busy || !key}>
            {busy ? '登入中…' : '登入'}
          </button>
        </div>
        <p class="lv-hint">金鑰即服務設定的 LORE_VAULT_API_TOKEN；登入後以 HttpOnly cookie 維持，瀏覽器不保存金鑰。</p>
      </form>
    </div>
  );
}
