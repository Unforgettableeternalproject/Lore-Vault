// UEP 寫入審核的共用判斷與文案：徽章、未審核標記、核准／駁回請求與 409 說明。
import { ApiError, type ApiClient } from './api';
import type { ReviewState } from './types';

export interface ReviewFields {
  review_state?: ReviewState;
  reviewed_by?: string | null;
  review_reason?: string | null;
  review?: 'unverified' | null;
}

/** 未審核：服務帶 review: unverified，或狀態為待審／待刪除 */
export function isUnverified(item: ReviewFields): boolean {
  return item.review === 'unverified' || item.review_state === 'pending' || item.review_state === 'pending_delete';
}

export function isPendingReview(state: ReviewState | undefined): boolean {
  return state === 'pending' || state === 'pending_delete';
}

export interface ReviewBadgeInfo {
  tone: 'pending' | 'danger' | 'ok' | 'rejected';
  text: string;
}

/** 審核狀態徽章文案；none／沒有狀態不顯示 */
export function reviewBadgeInfo(item: ReviewFields): ReviewBadgeInfo | null {
  switch (item.review_state) {
    case 'pending':
      return { tone: 'pending', text: '待審（UEP）' };
    case 'pending_delete':
      return { tone: 'danger', text: '待刪除（UEP 請求刪除）' };
    case 'rejected':
      return { tone: 'rejected', text: `已駁回：${item.review_reason?.trim() || '未填原因'}` };
    case 'verified':
      return { tone: 'ok', text: item.reviewed_by ? `已核准 by ${item.reviewed_by}` : '已核准' };
    default:
      return null;
  }
}

export type ReviewAction = 'approve' | 'reject';

/** 409：狀態與請求不符（例如已被別處核准、或內容已被改成其他狀態） */
export function isReviewConflict(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409;
}

export const REVIEW_CONFLICT_TEXT = '這則的審核狀態已經改變（可能已在別處處理），內容已更新，請重新檢視（已重新載入最新版本）後再操作。';

/** 送出核准或駁回；reason 只在駁回時送出 */
export async function submitReview(
  api: ApiClient,
  action: ReviewAction,
  args: { space: string; vault: string; id: string; expectedUpdated: string; reason?: string },
): Promise<void> {
  const body: Record<string, unknown> = { space: args.space, vault: args.vault, id: args.id, expected_updated: args.expectedUpdated };
  if (action === 'reject') body.reason = args.reason;
  await api.post(`/v1/review/${action}`, body);
}
