import { describe, expect, it } from 'vitest';

import { ApiError } from './api';
import { isReviewConflict, isUnverified, reviewBadgeInfo } from './review';

describe('審核狀態判斷', () => {
  it('徽章依狀態顯示對應文案', () => {
    expect(reviewBadgeInfo({ review_state: 'pending' })?.text).toBe('待審（UEP）');
    expect(reviewBadgeInfo({ review_state: 'pending_delete' })?.text).toContain('待刪除');
    expect(reviewBadgeInfo({ review_state: 'rejected', review_reason: '內容重複' })?.text).toBe('已駁回：內容重複');
    expect(reviewBadgeInfo({ review_state: 'verified', reviewed_by: 'Bernie' })?.text).toBe('已核准 by Bernie');
    expect(reviewBadgeInfo({ review_state: 'none' })).toBeNull();
    expect(reviewBadgeInfo({})).toBeNull();
  });

  it('未審核：review 標記或待審／待刪除狀態；已核准與無狀態不是', () => {
    expect(isUnverified({ review: 'unverified' })).toBe(true);
    expect(isUnverified({ review_state: 'pending' })).toBe(true);
    expect(isUnverified({ review_state: 'pending_delete' })).toBe(true);
    expect(isUnverified({ review_state: 'verified' })).toBe(false);
    expect(isUnverified({ review_state: 'rejected' })).toBe(false);
    expect(isUnverified({})).toBe(false);
  });

  it('只有 HTTP 409 算審核衝突', () => {
    expect(isReviewConflict(new ApiError(409, 'conflict', 'x'))).toBe(true);
    expect(isReviewConflict(new ApiError(500, 'x', 'x'))).toBe(false);
    expect(isReviewConflict(new Error('x'))).toBe(false);
  });
});
