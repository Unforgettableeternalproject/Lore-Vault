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

/**
 * 409 plan_changed 的回應：`error.plan` 為目前規劃；服務若同時簽發新 token（`error.confirm_token`），
 * 使用者可直接確認新規劃；沒有 token 時只能重新規劃。
 */
export function planChangedInfo(body: unknown): { plan: Record<string, unknown> | null; next: TwoPhasePlan | null } {
  const error = (body as { error?: Record<string, unknown> } | null)?.error ?? null;
  const plan = error && typeof error.plan === 'object' && error.plan !== null ? (error.plan as Record<string, unknown>) : null;
  const token = typeof error?.confirm_token === 'string' && error.confirm_token ? error.confirm_token : null;
  const expiresAt = typeof error?.expires_at === 'string' ? error.expires_at : null;
  return { plan, next: plan && token ? { plan, token, expiresAt } : null };
}
