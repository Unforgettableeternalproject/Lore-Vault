// 審核狀態徽章與列表用的「未審核」標記。
import { isUnverified, reviewBadgeInfo, type ReviewFields } from '../lib/review';

export function ReviewBadge({ item }: { item: ReviewFields }) {
  const info = reviewBadgeInfo(item);
  if (!info) return null;
  return (
    <span class={`lv-review-badge lv-review-badge--${info.tone}`} data-testid="review-badge" data-review-state={item.review_state}>
      {info.text}
    </span>
  );
}

/** 列表與搜尋結果裡的未審核標記；已審核或無審核狀態不顯示 */
export function UnverifiedTag({ item }: { item: ReviewFields }) {
  if (!isUnverified(item)) return null;
  const deleting = item.review_state === 'pending_delete';
  return (
    <span
      class="lv-tag lv-tag--warn"
      data-testid="unverified-tag"
      title="外部寫入者（UEP）的內容尚未經審核，不要當作已確認的事實"
    >
      {deleting ? '待刪除' : '未審核'}
    </span>
  );
}
