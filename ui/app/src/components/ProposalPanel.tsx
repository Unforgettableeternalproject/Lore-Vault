// 審核頁的提案處理面板：載入提案全文，依種類顯示內容（刪除規劃、欄位差異、連結、封存摘要），
// 提供核准與駁回（原因必填）。proposal_stale 時改顯示三欄比對，並可勾選「以目前版本為基準套用」。
import { useEffect, useState } from 'preact/hooks';

import { useApp } from '../lib/context';
import { describeError, isAbort } from '../lib/format';
import { approveProposal, fetchProposalDetail, isSettledConflict, kindLabel, parseStale, rejectProposal } from '../lib/proposals';
import type { NoteFields, ProposalDetail, ProposalStaleInfo, QueueItem } from '../lib/types';
import { ProposalDiff } from './ProposalDiff';
import { Dialog, Loading } from './ui';

export type ProposalOutcome = { type: 'approved' | 'rejected' } | { type: 'conflict'; message: string };

const PLAN_LABEL: Record<string, string> = { title: '標題', link_count: '連出連結數', backlink_count: '被連結數' };

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function Facts({ rows }: { rows: [string, string][] }) {
  return (
    <dl class="lv-small">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt class="lv-muted">{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function ProposalBody({ detail, stale, itemStale }: { detail: ProposalDetail; stale: ProposalStaleInfo | null; itemStale: boolean }) {
  const { proposal, target } = detail;
  const p = proposal.payload;
  switch (proposal.kind) {
    case 'note_delete':
    case 'document_delete': {
      const plan = (p.plan_summary ?? {}) as Record<string, unknown>;
      const rows: [string, string][] = [['目標', target.title ?? target.id]];
      if (str(p.reason)) rows.push(['提出原因', str(p.reason)]);
      for (const [k, v] of Object.entries(plan)) rows.push([PLAN_LABEL[k] ?? k, String(v)]);
      return (
        <>
          <p>核准會真的刪除這個項目（留下墓碑，可復原）。</p>
          <Facts rows={rows} />
        </>
      );
    }
    case 'note_update': {
      const changes = ((p.changes ?? {}) as NoteFields) ?? {};
      const base = proposal.base_snapshot ?? {};
      if (stale) return <ProposalDiff base={stale.base} proposed={stale.proposed} current={stale.current} />;
      return <ProposalDiff base={base} proposed={changes} current={itemStale ? detail.current : null} />;
    }
    case 'link_change': {
      const remove = Array.isArray(p.remove) ? (p.remove as string[]) : [];
      const sup = p.supersedes as { from?: string | null; to?: string | null } | null | undefined;
      return (
        <>
          <p>agent 要移除下列連結（新增的連結已經生效）。</p>
          <ul class="lv-small" data-testid="proposal-link-remove">
            {remove.map((id) => (
              <li key={id}>{detail.link_titles?.[id] ?? id}</li>
            ))}
          </ul>
          {sup && (
            <p class="lv-small">
              取代關係：{sup.from ? (detail.link_titles?.[sup.from] ?? sup.from) : '（無）'} → {sup.to ? (detail.link_titles?.[sup.to] ?? sup.to) : '（無）'}
            </p>
          )}
        </>
      );
    }
    case 'task_archive': {
      const rows: [string, string][] = [['change', target.title ?? target.id]];
      if (str(p.reason)) rows.push(['封存原因', str(p.reason)]);
      rows.push(['含未完成項目', p.allow_incomplete ? '是' : '否']);
      return (
        <>
          <p>核准代表同意封存；實際封存要等 agent 下次呼叫 archive 時才會執行。</p>
          <Facts rows={rows} />
        </>
      );
    }
    default:
      return null;
  }
}

export function ProposalPanel({ item, onClose, onDone }: { item: QueueItem; onClose: () => void; onDone: (outcome: ProposalOutcome) => void }) {
  const { api, space } = useApp();
  const [detail, setDetail] = useState<ProposalDetail | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [stale, setStale] = useState<ProposalStaleInfo | null>(null);
  const [rebase, setRebase] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchProposalDetail(api, { space: space.id, vault: item.vault, id: item.item_id }, ctrl.signal)
      .then(setDetail)
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        if (isSettledConflict(err)) onDone({ type: 'conflict', message: describeError(err) });
        else setLoadError(err);
      });
    return () => ctrl.abort();
  }, [api, space.id, item.vault, item.item_id]);

  const proposal = detail?.proposal;
  const kind = proposal?.kind ?? item.kind;
  const approvedOnly = proposal?.state === 'approved';
  const needRebase = (item.stale || stale !== null) && kind === 'note_update';
  const rebaseTo = stale?.current_updated ?? detail?.current_updated ?? undefined;
  const approveLabel = kind === 'note_delete' || kind === 'document_delete' ? '核准刪除' : kind === 'task_archive' ? '同意封存' : '核准';

  const run = async (action: 'approve' | 'reject') => {
    if (!proposal) return;
    const trimmed = reason.trim();
    if (action === 'reject' && !trimmed) {
      setReasonError(true);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const args = { space: space.id, vault: item.vault, id: proposal.id, expectedRev: proposal.rev };
      if (action === 'approve') await approveProposal(api, { ...args, rebaseTo: needRebase && rebase ? rebaseTo : undefined });
      else await rejectProposal(api, { ...args, reason: trimmed });
      onDone({ type: action === 'approve' ? 'approved' : 'rejected' });
    } catch (err) {
      const info = parseStale(err);
      if (info) {
        setStale(info);
        setError(err);
      } else if (isSettledConflict(err)) onDone({ type: 'conflict', message: describeError(err) });
      else setError(err);
    } finally {
      setBusy(false);
    }
  };

  const actions = (
    <>
      {proposal && !approvedOnly && (
        <button
          type="button"
          class="btn-outline btn-outline--gold"
          disabled={busy || (needRebase && !rebase)}
          title={needRebase && !rebase ? '基準已變動：勾選「以目前版本為基準套用」後才能核准' : undefined}
          onClick={() => void run('approve')}
        >
          {approveLabel}
        </button>
      )}
      {proposal && !approvedOnly && !rejecting && (
        <button type="button" class="btn-outline" disabled={busy} onClick={() => setRejecting(true)}>
          駁回…
        </button>
      )}
      {rejecting && (
        <button type="button" class="btn-outline" disabled={busy} onClick={() => void run('reject')}>
          確認駁回
        </button>
      )}
      <button type="button" class="btn-outline" disabled={busy} onClick={onClose}>
        關閉
      </button>
    </>
  );

  return (
    <Dialog title={`${kindLabel(kind)}：${item.target.title ?? item.target.id}`} onClose={onClose} actions={actions}>
      <div data-testid="proposal-panel">
        <p class="lv-small lv-muted">
          提出者 {item.proposer.author ?? item.proposer.principal} · {item.summary}
        </p>
        {loadError !== null && (
          <p class="lv-notice lv-notice--error" role="alert" data-testid="proposal-load-error">
            {describeError(loadError)}
          </p>
        )}
        {!detail && loadError === null && <Loading />}
        {detail && <ProposalBody detail={detail} stale={stale} itemStale={item.stale} />}
        {approvedOnly && (
          <p class="lv-small" data-testid="proposal-approved-note">
            已同意，等待 agent 下次呼叫 archive 時執行。
          </p>
        )}
        {needRebase && detail && (
          <>
            <p class="lv-notice lv-notice--error" role="alert" data-testid="proposal-stale">
              提案之後目標內容已被修改過，上面是提案基準、目前版本與提案內容的比對。
            </p>
            <label class="lv-small">
              <input type="checkbox" checked={rebase} onChange={(e) => setRebase((e.target as HTMLInputElement).checked)} /> 以目前版本為基準套用（覆寫提案基準之後的修改）
            </label>
          </>
        )}
        {rejecting && (
          <label class="lv-field">
            <span class="lv-field__label">駁回原因（必填）</span>
            <textarea
              class="lv-textarea"
              aria-label="駁回原因"
              value={reason}
              onInput={(e) => {
                setReason((e.target as HTMLTextAreaElement).value);
                setReasonError(false);
              }}
            />
          </label>
        )}
        {reasonError && (
          <p class="lv-notice lv-notice--error" role="alert" data-testid="proposal-reason-required">
            請先填寫原因，才能駁回。
          </p>
        )}
        {error !== null && (
          <p class="lv-notice lv-notice--error" role="alert" data-testid="proposal-error">
            {describeError(error)}
          </p>
        )}
      </div>
    </Dialog>
  );
}
