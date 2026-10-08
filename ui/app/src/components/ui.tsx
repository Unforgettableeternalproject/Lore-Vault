// 畫面共用的小元件：摘要來源標籤、狀態框、對話框、兩段式確認。
import type { ComponentChildren } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';

import { ApiError } from '../lib/api';
import { executeTwoPhase, planChangedInfo, planTwoPhase, type TwoPhasePlan } from '../lib/confirm';
import { useApp } from '../lib/context';
import { describeError, formatTime, summarySource } from '../lib/format';
import type { SummarySource, TwoPhaseResponse } from '../lib/types';

export function SourceTag({ source }: { source: SummarySource | string | undefined }) {
  const tag = summarySource(source);
  return (
    <span class={`lv-src lv-src--${tag.tone}`} title={tag.note} data-source={source}>
      {tag.label}
    </span>
  );
}

/**
 * metadata 標籤（vault、寫入者、日期、模式／分數、標籤、錨點…）：依種類分色，讓列表的 metadata 不再像一串純文字。
 * label 給螢幕閱讀器與滑鼠提示（例如「vault」「寫入者」），畫面上只顯示值；testId 放在值本身，textContent 只含值。
 */
export type BadgeTone =
  | 'vault'
  | 'author'
  | 'time'
  | 'score'
  | 'degraded'
  | 'tag'
  | 'kind'
  | 'warn'
  | 'anchor'
  | 'plain'
  // 任務層狀態：可開工＝成功、待授權＝金色（實心）、無法判定＝錯誤；被擋住用 warn、已完成用 plain
  | 'ready'
  | 'auth'
  | 'error';

export function Badge({
  tone,
  label,
  children,
  title,
  testId,
}: {
  tone: BadgeTone;
  label: string;
  children: ComponentChildren;
  title?: string;
  testId?: string;
}) {
  return (
    <span class={`lv-badge lv-badge--${tone}`} title={title ?? label}>
      <span class="lv-visually-hidden">{label}：</span>
      <span class="lv-badge__value" data-testid={testId}>
        {children}
      </span>
    </span>
  );
}

type Tone = 'warn' | 'error' | 'info';

/** 狀態橫幅：降級、截斷、缺漏等。label 為等寬大寫標籤（DEGRADED、TRUNCATED…）。 */
export function Banner({
  tone,
  label,
  title,
  children,
  action,
  testId,
}: {
  tone: Tone;
  label: string;
  title?: ComponentChildren;
  children?: ComponentChildren;
  action?: ComponentChildren;
  testId?: string;
}) {
  return (
    <div class={`lv-banner lv-banner--${tone}`} role={tone === 'error' ? 'alert' : 'status'} data-testid={testId}>
      <span class="lv-banner__label">{label}</span>
      <div class="lv-banner__body">
        {title && <div class="lv-banner__title">{title}</div>}
        {children && <div class="lv-banner__text">{children}</div>}
      </div>
      {action && <div class="lv-banner__action">{action}</div>}
    </div>
  );
}

/**
 * 空狀態：大字置中顯示在空白區，可附一行補充與動作按鈕。
 * size="sm" 給側欄、小區塊（仍比內文大、置中），預設給主內容區。
 */
export function EmptyState({
  title,
  children,
  action,
  size = 'lg',
  testId,
}: {
  title: ComponentChildren;
  children?: ComponentChildren;
  action?: ComponentChildren;
  size?: 'lg' | 'sm';
  testId?: string;
}) {
  return (
    <div class={`lv-empty-state lv-empty-state--${size}`} role="status" data-testid={testId}>
      <p class="lv-empty-state__title">{title}</p>
      {children && <p class="lv-empty-state__text">{children}</p>}
      {action && <div class="lv-empty-state__action">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div class="zone-state zone-state--error" role="alert">
      {describeError(error)}
      {onRetry && (
        <button type="button" onClick={onRetry}>
          重試
        </button>
      )}
    </div>
  );
}

export function Loading({ label = '載入中…' }: { label?: string }) {
  return (
    <div class="zone-state lv-loading" role="status" aria-live="polite">
      {label}
    </div>
  );
}

// ── 對話框 ──

export function Dialog({
  title,
  children,
  onClose,
  actions,
  tone = 'default',
}: {
  title: string;
  children: ComponentChildren;
  onClose: () => void;
  actions: ComponentChildren;
  tone?: 'default' | 'danger';
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useRef(`lv-dialog-${Math.random().toString(36).slice(2)}`).current;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const first = ref.current?.querySelector<HTMLElement>(
      'input, textarea, button:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    first?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      } else if (e.key === 'Tab' && ref.current) {
        // 焦點留在對話框內
        const items = Array.from(
          ref.current.querySelectorAll<HTMLElement>('input, textarea, button:not([disabled]), a[href]'),
        );
        if (items.length === 0) return;
        const firstItem = items[0]!;
        const lastItem = items[items.length - 1]!;
        if (e.shiftKey && document.activeElement === firstItem) {
          e.preventDefault();
          lastItem.focus();
        } else if (!e.shiftKey && document.activeElement === lastItem) {
          e.preventDefault();
          firstItem.focus();
        }
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      // 焦點還給開啟前的元素；由快捷鍵從頁面空白處開啟（焦點在 body）時改交給主內容，
      // 不讓焦點與鍵盤起點落在已移除的對話框位置（下一個 Tab 會跳出頁面）
      if (previous && previous !== document.body && previous.isConnected) previous.focus?.();
      else document.getElementById('lv-main')?.focus();
    };
    // onClose 變動不重綁：對話框存在期間行為固定
  }, []);
  return (
    <div class="uep-dialog-overlay lv-dialog-overlay">
      <div
        ref={ref}
        class={'uep-dialog lv-dialog' + (tone === 'danger' ? ' lv-dialog--danger' : '')}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <div class="uep-dialog__accent" />
        <h2 id={titleId} class="uep-dialog__title">
          {title}
        </h2>
        <div class="uep-dialog__message">{children}</div>
        <div class="uep-dialog__actions">{actions}</div>
      </div>
    </div>
  );
}

// ── 兩段式確認 ──

const COUNT_LABEL: Record<string, string> = {
  notes: '筆記',
  note_embeddings: '筆記向量',
  note_fts: '筆記全文索引',
  documents: '文件',
  chunks: '文件段落',
  document_chunks: '文件段落',
  chunk_fts: '段落全文索引',
  document_chunk_embeddings: '段落向量',
  // 通用側載（不檢索的小型機器狀態，例如任務層快照）：刪除計數與換 space 的改寫欄位
  sidecar_blobs: '側載狀態',
  'sidecar_blobs.vault': '側載狀態（vault）',
  'sidecar_blobs.space': '側載狀態（space）',
};

function Meta({ label, value }: { label: string; value: ComponentChildren }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}

export function PlanView({ plan }: { plan: Record<string, unknown> }) {
  const counts = (plan.counts ?? {}) as Record<string, number>;
  const entries = Object.entries(counts).filter(([, n]) => typeof n === 'number');
  const aliases =
    typeof plan.aliases === 'object' && plan.aliases !== null && !Array.isArray(plan.aliases)
      ? Object.entries(plan.aliases as Record<string, string>)
      : [];
  const noteIds = Array.isArray(plan.note_ids) ? plan.note_ids : null;
  return (
    <div class="lv-plan" data-testid="delete-plan">
      <dl class="lv-plan__meta">
        {'new_key' in plan ? (
          <>
            <Meta label="key" value={<span class="lv-mono">{String(plan.key)} → {String(plan.new_key)}</span>} />
            <Meta label="space" value={`${String(plan.from)} → ${String(plan.to)}`} />
          </>
        ) : (
          'vault' in plan && <Meta label="vault" value={<span class="lv-mono">{String(plan.vault)}</span>} />
        )}
        {'filename' in plan && <Meta label="檔名" value={String(plan.filename)} />}
        {noteIds && <Meta label="轉為墓碑" value={`${noteIds.length} 則筆記`} />}
      </dl>
      {aliases.length > 0 && (
        <ul class="lv-plan__counts" aria-label="別名改寫">
          {aliases.map(([from, to]) => (
            <li key={from}>
              <span class="lv-mono">{from}</span>
              <span class="lv-mono">→ {to}</span>
            </li>
          ))}
        </ul>
      )}
      {entries.length > 0 && (
        <ul class="lv-plan__counts" aria-label="受影響筆數">
          {entries.map(([k, n]) => (
            <li key={k}>
              <span>{COUNT_LABEL[k] ?? k}</span>
              <span class="lv-mono">{n}</span>
            </li>
          ))}
        </ul>
      )}
      {Array.isArray(plan.relinked) && plan.relinked.length > 0 && (
        <p class="lv-plan__warn">刪掉現行版本後，前一版會回到索引（{plan.relinked.length} 份文件重新連結）。</p>
      )}
      {plan.blob_still_referenced === false && (
        <p class="lv-plan__note">原始檔不會立即刪除；沒有其他文件引用時由 doctor 回報、gc-blobs 清理。</p>
      )}
      {plan.requires_force === true && <p class="lv-plan__warn">此刪除需要強制執行（確認即等同 --force）。</p>}
    </div>
  );
}

export interface TwoPhaseProps {
  title: string;
  path: string;
  args: Record<string, unknown>;
  describe: ComponentChildren;
  onDone: (result: TwoPhaseResponse) => void;
  onCancel: () => void;
  confirmLabel?: string;
  /** 確認鈕在輸入框內容與此字串完全相同前維持停用（刪 vault 需輸入 key 全文） */
  requireText?: string;
}

/**
 * 兩段式確認：開啟即向服務規劃（不帶 token），顯示規劃內容；按確認才以相同參數＋token 執行。
 * - 過期／token 無效：要求重新規劃，不自動重送
 * - 409 plan_changed：服務附新 token 時直接顯示新規劃讓使用者再確認；沒附就只能重新規劃
 */
export function TwoPhaseConfirm({
  title,
  path,
  args,
  describe,
  onDone,
  onCancel,
  confirmLabel = '確認',
  requireText,
}: TwoPhaseProps) {
  const { api } = useApp();
  const [plan, setPlan] = useState<TwoPhasePlan | null>(null);
  const [changedPlan, setChangedPlan] = useState<Record<string, unknown> | null>(null);
  const [replanned, setReplanned] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(true);
  const [typed, setTyped] = useState('');

  const doPlan = async () => {
    setBusy(true);
    setError(null);
    setPlan(null);
    setReplanned(false);
    try {
      setPlan(await planTwoPhase(api, path, args));
      setChangedPlan(null);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    void doPlan();
    // 只在開啟時規劃一次；之後由使用者按「重新規劃」
  }, []);

  const confirm = async () => {
    if (!plan) return;
    setBusy(true);
    setError(null);
    try {
      onDone(await executeTwoPhase(api, path, args, plan.token));
    } catch (err) {
      if (err instanceof ApiError && err.code === 'plan_changed') {
        const info = planChangedInfo(err.body);
        if (info.next) {
          // 服務已為新規劃簽發 token：讓使用者檢視新內容後再確認
          setPlan(info.next);
          setChangedPlan(null);
          setReplanned(true);
        } else {
          setError(err);
          setChangedPlan(info.plan);
          setPlan(null); // 舊 token 已不能用
        }
      } else {
        setError(err);
        if (err instanceof ApiError && (err.code === 'confirm_token_expired' || err.code === 'invalid_confirm_token')) {
          setPlan(null);
        }
      }
      setBusy(false);
    }
  };

  const needsReplan = !busy && !plan;
  const textOk = requireText === undefined || typed === requireText;
  return (
    <Dialog
      title={title}
      tone="danger"
      onClose={onCancel}
      actions={
        <>
          <button type="button" class="uep-dialog__btn uep-dialog__btn--cancel" onClick={onCancel}>
            取消
          </button>
          {needsReplan ? (
            <button type="button" class="uep-dialog__btn uep-dialog__btn--confirm" onClick={() => void doPlan()}>
              重新規劃
            </button>
          ) : (
            <button
              type="button"
              class="uep-dialog__btn lv-dialog__btn--danger"
              disabled={busy || !plan || !textOk}
              onClick={() => void confirm()}
            >
              {confirmLabel}
            </button>
          )}
        </>
      }
    >
      <div class="lv-stack">
        <div>{describe}</div>
        {busy && !plan && <p class="lv-muted">正在向服務規劃影響範圍…</p>}
        {replanned && (
          <p class="lv-plan__warn" role="status" data-testid="plan-replanned">
            規劃後資料已變動，以下是服務重新規劃的內容，請確認後再送出。
          </p>
        )}
        {plan && (
          <>
            <PlanView plan={plan.plan} />
            {plan.expiresAt && <p class="lv-muted">此確認於 {formatTime(plan.expiresAt)} 前有效。</p>}
          </>
        )}
        {changedPlan && (
          <>
            <p class="lv-plan__warn">資料在規劃後已變動，目前的規劃如下，需重新規劃後再確認：</p>
            <PlanView plan={changedPlan} />
          </>
        )}
        {requireText !== undefined && plan && (
          <label class="lv-field">
            <span class="lv-field__label">
              輸入 <span class="lv-mono">{requireText}</span> 確認
            </span>
            <input
              class="lv-input lv-mono"
              value={typed}
              autoComplete="off"
              spellcheck={false}
              onInput={(e) => setTyped((e.target as HTMLInputElement).value)}
            />
          </label>
        )}
        {error !== null && (
          <p class="lv-notice lv-notice--error" role="alert">
            {describeError(error)}
          </p>
        )}
      </div>
    </Dialog>
  );
}

/** 刪除用的兩段式確認（既有呼叫點沿用）。 */
export function TwoPhaseDelete(props: TwoPhaseProps) {
  return <TwoPhaseConfirm confirmLabel="確認刪除" {...props} />;
}
