// 畫面共用的小元件：摘要來源標籤、狀態框、對話框、兩段式刪除確認。
import type { ComponentChildren } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';

import { ApiError } from '../lib/api';
import { executeTwoPhase, planTwoPhase, type TwoPhasePlan } from '../lib/confirm';
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
      previous?.focus?.();
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

// ── 兩段式刪除 ──

const COUNT_LABEL: Record<string, string> = {
  notes: '筆記',
  note_embeddings: '筆記向量',
  note_fts: '筆記全文索引',
  documents: '文件',
  document_chunks: '文件段落',
  chunk_fts: '段落全文索引',
  document_chunk_embeddings: '段落向量',
};

function PlanView({ plan }: { plan: Record<string, unknown> }) {
  const counts = (plan.counts ?? {}) as Record<string, number>;
  const entries = Object.entries(counts).filter(([, n]) => typeof n === 'number');
  return (
    <div class="lv-plan" data-testid="delete-plan">
      <dl class="lv-plan__meta">
        {'vault' in plan && (
          <>
            <dt>vault</dt>
            <dd>{String(plan.vault)}</dd>
          </>
        )}
        {'filename' in plan && (
          <>
            <dt>檔名</dt>
            <dd>{String(plan.filename)}</dd>
          </>
        )}
      </dl>
      {entries.length > 0 && (
        <ul class="lv-plan__counts">
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

/**
 * 兩段式刪除：開啟即向服務規劃（不帶 token），顯示規劃內容；按確認才以相同參數＋token 執行。
 * 過期、規劃變動都要求重新規劃，不自動重送。
 */
export function TwoPhaseDelete({
  title,
  path,
  args,
  describe,
  onDone,
  onCancel,
}: {
  title: string;
  path: string;
  args: Record<string, unknown>;
  describe: ComponentChildren;
  onDone: (result: TwoPhaseResponse) => void;
  onCancel: () => void;
}) {
  const { api } = useApp();
  const [plan, setPlan] = useState<TwoPhasePlan | null>(null);
  const [changedPlan, setChangedPlan] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(true);

  const doPlan = async () => {
    setBusy(true);
    setError(null);
    setPlan(null);
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
      setError(err);
      if (err instanceof ApiError && err.code === 'plan_changed') {
        const body = err.body as { error?: { plan?: Record<string, unknown> } } | null;
        setChangedPlan(body?.error?.plan ?? null);
        setPlan(null); // 舊 token 已不能用
      } else if (err instanceof ApiError && (err.code === 'confirm_token_expired' || err.code === 'invalid_confirm_token')) {
        setPlan(null);
      }
      setBusy(false);
    }
  };

  const needsReplan = !busy && !plan;
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
              disabled={busy || !plan}
              onClick={() => void confirm()}
            >
              確認刪除
            </button>
          )}
        </>
      }
    >
      <div class="lv-stack">
        <div>{describe}</div>
        {busy && !plan && <p class="lv-muted">正在向服務規劃刪除範圍…</p>}
        {plan && (
          <>
            <PlanView plan={plan.plan} />
            {plan.expiresAt && <p class="lv-muted">此確認於 {formatTime(plan.expiresAt)} 前有效。</p>}
          </>
        )}
        {changedPlan && (
          <>
            <p class="lv-plan__warn">資料在規劃後已變動，目前的規劃如下，需重新確認：</p>
            <PlanView plan={changedPlan} />
          </>
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
