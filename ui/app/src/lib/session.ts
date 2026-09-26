// UI 本地登入（A21）：session 存在服務端、以 HttpOnly cookie 識別，前端碰不到任何憑證。
import { ApiError, type ApiClient } from './api';

export interface SessionInfo {
  authenticated: true;
  expires_at: string;
  idle_expires_at: string;
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

export async function login(api: ApiClient, key: string): Promise<void> {
  await api.post('/ui/api/login', { key });
}

export async function logout(api: ApiClient): Promise<void> {
  await api.post('/ui/api/logout');
}

/** 登入錯誤轉成給使用者看的文案。 */
export function describeLoginError(err: unknown): string {
  if (!(err instanceof ApiError)) return '登入時發生未預期的錯誤';
  switch (err.code) {
    case 'invalid_credentials':
      return '存取金鑰不正確';
    case 'too_many_attempts':
      return err.retryAfter
        ? `嘗試次數過多，請於 ${err.retryAfter} 秒後再試`
        : '嘗試次數過多，請稍後再試';
    case 'network_error':
      return '無法連線到 Lore Vault 服務';
    default:
      return `${err.message}（${err.code}）`;
  }
}
