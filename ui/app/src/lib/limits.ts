// 服務端限制值：以 `/ui/api/session` 的 `limits` 為準（與服務端實際檢查同一來源）。
// 舊版服務不回 limits 時才退回下列值（與服務預設相同）；缺漏的個別欄位同樣逐項補齊，不讓畫面拿到 undefined。
import type { SessionLimits } from './types';

export const FALLBACK_LIMITS: SessionLimits = {
  max_file_bytes: 25 * 1024 * 1024,
  max_chars: 10_000_000,
  author_max_chars: 64,
  get_max_ids: 50,
  get_default_budget: 12_000,
  list_max_limit: 200,
  list_default_limit: 50,
  list_default_budget: 4000,
  recall_max_limit: 100,
  recall_default_limit: 10,
  recall_default_budget: 2000,
};

export function resolveLimits(limits: Partial<SessionLimits> | null | undefined): SessionLimits {
  const out: SessionLimits = { ...FALLBACK_LIMITS };
  if (!limits) return out;
  for (const key of Object.keys(FALLBACK_LIMITS) as (keyof SessionLimits)[]) {
    const value = limits[key];
    if (typeof value === 'number' && Number.isFinite(value) && value > 0) out[key] = value;
  }
  return out;
}
