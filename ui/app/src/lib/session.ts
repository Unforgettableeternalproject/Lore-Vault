// UI 本地登入（A21／A23）：帳號密碼換服務端 session，以 HttpOnly cookie 識別，前端碰不到任何憑證。
// 全域失敗 3 次即鎖定、需在主機人工解鎖；鎖定與剩餘次數由服務端回報，前端只負責顯示。
import { ApiError, type ApiClient } from './api';
import type { SessionLimits } from './types';

export interface SessionInfo {
  authenticated: true;
  /** 登入帳號（= principal，A22／A23） */
  principal?: string;
  /** 登入帳號的顯示名稱：UI 寫入 note 的署名 */
  display_name?: string;
  expires_at: string;
  idle_expires_at: string;
  /** 前端需要的限制值（與服務端檢查同一來源）；舊版服務可能沒有 */
  limits?: Partial<SessionLimits>;
}

/** 登入頁在送出前需要的公開狀態（`GET /ui/api/login`）。 */
export interface LoginState {
  account_configured: boolean;
  locked: boolean;
  remaining: number;
  max_failures: number;
  /** 尚未設定帳號時，服務建議在主機執行的指令 */
  setup_command: string | null;
}

/** 目前 cookie 是否對應有效 session；未登入回 null，其他錯誤照常丟出。 */
export async function checkSession(api: ApiClient): Promise<SessionInfo | null> {
  try {
    return (await api.get<SessionInfo>('/ui/api/session')).data;
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return null;
    throw err;
  }
}

export async function loginState(api: ApiClient): Promise<LoginState> {
  return (await api.get<LoginState>('/ui/api/login')).data;
}

export async function login(api: ApiClient, username: string, password: string): Promise<void> {
  await api.post('/ui/api/login', { username, password });
}

export async function logout(api: ApiClient): Promise<void> {
  await api.post('/ui/api/logout');
}

/** 服務端錯誤 body 內的附加欄位（剩餘次數、鎖定）。 */
export function loginErrorDetail(err: unknown): { remaining: number | null; locked: boolean } {
  if (!(err instanceof ApiError)) return { remaining: null, locked: false };
  const body = err.body as { error?: { remaining?: unknown; locked?: unknown } } | null;
  const remaining = body?.error?.remaining;
  return {
    remaining: typeof remaining === 'number' ? remaining : null,
    locked: err.code === 'locked' || body?.error?.locked === true,
  };
}

export const LOCKED_MESSAGE = '登入已鎖定（失敗達 3 次），需在主機人工解鎖後才能再登入。';

/** 登入錯誤轉成給使用者看的文案。 */
export function describeLoginError(err: unknown): string {
  if (!(err instanceof ApiError)) return '登入時發生未預期的錯誤';
  const { remaining, locked } = loginErrorDetail(err);
  if (locked) return LOCKED_MESSAGE;
  switch (err.code) {
    case 'invalid_credentials':
      return remaining === null
        ? '帳號或密碼錯誤'
        : `帳號或密碼錯誤，剩餘 ${remaining} 次；失敗 3 次將鎖定，需人工解鎖`;
    case 'no_account':
      return '尚未設定帳號，請在主機執行 ui-set-password 建立帳號';
    case 'network_error':
      return '無法連線到 Lore Vault 服務';
    default:
      return `${err.message}（${err.code}）`;
  }
}
