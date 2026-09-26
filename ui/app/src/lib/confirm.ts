// 兩段式確認（note_delete／document_delete 等 ⚠ 端點）：
// 不帶 confirm_token → 只規劃；以「完全相同的參數」加上 token 再送一次才執行。
import type { ApiClient } from './api';
import type { TwoPhaseResponse } from './types';

export interface TwoPhasePlan {
  plan: Record<string, unknown>;
  token: string;
  expiresAt: string | null;
}

export async function planTwoPhase(
  api: ApiClient,
  path: string,
  args: Record<string, unknown>,
): Promise<TwoPhasePlan> {
  const { data } = await api.post<TwoPhaseResponse>(path, args);
  if (data.executed || !data.confirm_token) {
    // 規劃請求不該直接執行；若服務行為變了要大聲失敗，不當成功
    throw new Error('服務在規劃階段沒有回傳確認憑證（confirm_token）');
  }
  return { plan: data.plan, token: data.confirm_token, expiresAt: data.expires_at ?? null };
}

export async function executeTwoPhase(
  api: ApiClient,
  path: string,
  args: Record<string, unknown>,
  token: string,
): Promise<TwoPhaseResponse> {
  const { data } = await api.post<TwoPhaseResponse>(path, { ...args, confirm_token: token });
  if (!data.executed) throw new Error('服務回應未執行（executed: false）');
  return data;
}
