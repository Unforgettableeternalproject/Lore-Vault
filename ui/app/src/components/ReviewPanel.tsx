// 待審筆記的核准／駁回面板（NoteDetail 用）。pending：核准／駁回；pending_delete：核准刪除／保留。
// 駁回（保留）必須填原因，沒填不送出；409 交給父層重新載入並提示。
import { useState } from 'preact/hooks';

import { useApp } from '../lib/context';
import { describeError } from '../lib/format';
import { isReviewConflict, submitReview, type ReviewAction } from '../lib/review';
import type { NoteFull } from '../lib/types';
import { Banner } from './ui';

export type ReviewOutcome = { action: ReviewAction; deleted: boolean } | 'conflict';

export function ReviewPanel({ note, onDone }: { note: NoteFull; onDone: (outcome: ReviewOutcome) => void }) {
  const { api, space } = useApp();
  const deleting = note.review_state === 'pending_delete';
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const approveLabel = deleting ? '核准刪除' : '核准';
  const rejectLabel = deleting ? '保留' : '駁回';

  const run = async (action: ReviewAction) => {
    const trimmed = reason.trim();
    if (action === 'reject' && !trimmed) {
      setReasonError(true);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await submitReview(api, action, { space: space.id, vault: note.vault, id: note.id, expectedUpdated: note.updated, reason: trimmed });
      onDone({ action, deleted: deleting && action === 'approve' });
    } catch (err) {
      if (isReviewConflict(err)) onDone('conflict');
      else setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Banner
      tone="warn"
      label={deleting ? 'DELETE REQUEST' : 'PENDING REVIEW'}
      testId="review-panel"
      title={deleting ? 'UEP 請求刪除這則筆記，等待你的審核' : 'UEP 寫入的內容等待你的審核'}
    >
      {deleting
        ? '核准刪除會真的刪除這則筆記（留下墓碑，可復原）；保留會讓筆記維持原樣並記下原因。'
        : '核准後內容視為已確認；駁回要填原因。直接編輯並儲存，也會視為核准。'}
      {rejecting && (
        <label class="lv-field">
          <span class="lv-field__label">{deleting ? '保留的原因（必填）' : '駁回原因（必填）'}</span>
          <textarea
            class="lv-textarea"
            aria-label={deleting ? '保留原因' : '駁回原因'}
            value={reason}
            onInput={(e) => {
              setReason((e.target as HTMLTextAreaElement).value);
              setReasonError(false);
            }}
          />
        </label>
      )}
      {reasonError && (
        <p class="lv-notice lv-notice--error" role="alert" data-testid="review-reason-required">
          請先填寫原因，才能{rejectLabel}。
        </p>
      )}
      {error !== null && (
        <p class="lv-notice lv-notice--error" role="alert" data-testid="review-error">
          {describeError(error)}
        </p>
      )}
      <div class="lv-actions lv-actions--wrap">
        <button type="button" class="btn-outline btn-outline--gold" disabled={busy} onClick={() => void run('approve')}>
          {approveLabel}
        </button>
        {!rejecting ? (
          <button type="button" class="btn-outline" disabled={busy} onClick={() => setRejecting(true)}>
            {rejectLabel}…
          </button>
        ) : (
          <>
            <button type="button" class="btn-outline" disabled={busy} onClick={() => void run('reject')}>
              確認{rejectLabel}
            </button>
            <button
              type="button"
              class="btn-outline"
              disabled={busy}
              onClick={() => {
                setRejecting(false);
                setReason('');
                setReasonError(false);
              }}
            >
              取消
            </button>
          </>
        )}
      </div>
    </Banner>
  );
}
