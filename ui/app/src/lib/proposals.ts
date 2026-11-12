// 審核頁統一佇列的共用判斷與文案：種類標籤、深層連結、核准／駁回請求、錯誤分類。
import { ApiError, type ApiClient } from './api';
import { routePath } from './router';
import type { NoteFields, ProposalDetail, ProposalStaleInfo, QueueCounts, QueueItem, QueueKind } from './types';

export const QUEUE_KINDS: readonly QueueKind[] = ['note_update', 'note_delete', 'document_delete', 'link_change', 'task_archive', 'task_auth', 'uep_review'];

export const KIND_LABEL: Record<QueueKind, string> = {
  note_update: '修改筆記',
  note_delete: '刪除筆記',
  document_delete: '刪除文件',
  link_change: '移除連結',
  task_archive: '封存任務',
  task_auth: '任務授權',
  uep_review: 'UEP 待審',
};

export const QUEUE_PAGE_SIZE = 30;

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind as QueueKind] ?? kind;
}

export function countTotal(counts: QueueCounts | null): number {
  return counts ? Object.values(counts.counts).reduce<number>((sum, n) => sum + (n ?? 0), 0) : 0;
}

/** 非提案項目要導去的頁面：服務給的 deep_link（限 /ui/ 站內）優先，否則依種類推導 */
export function itemHref(item: QueueItem): string {
  if (item.deep_link && item.deep_link.startsWith('/ui/')) return item.deep_link;
  if (item.kind === 'task_auth') return routePath('tasks', [item.vault, item.target.id]);
  return routePath('notes', [item.target.id]);
}

export async function fetchProposalDetail(
  api: ApiClient,
  args: { space: string; vault: string; id: string },
  signal?: AbortSignal,
): Promise<ProposalDetail> {
  const { data } = await api.post<ProposalDetail>('/v1/review/proposal_get', args, signal);
  return data;
}

export async function approveProposal(
  api: ApiClient,
  args: { space: string; vault: string; id: string; expectedRev: number; rebaseTo?: string },
): Promise<void> {
  const body: Record<string, unknown> = { space: args.space, vault: args.vault, id: args.id, expected_rev: args.expectedRev };
  if (args.rebaseTo) body.rebase_to = args.rebaseTo;
  await api.post('/v1/review/proposal_approve', body);
}

export async function rejectProposal(
  api: ApiClient,
  args: { space: string; vault: string; id: string; expectedRev: number; reason: string },
): Promise<void> {
  await api.post('/v1/review/proposal_reject', {
    space: args.space,
    vault: args.vault,
    id: args.id,
    expected_rev: args.expectedRev,
    reason: args.reason,
  });
}

function errorOf(err: unknown): Record<string, unknown> | null {
  if (!(err instanceof ApiError)) return null;
  const e = (err.body as { error?: unknown } | null)?.error;
  return typeof e === 'object' && e !== null ? (e as Record<string, unknown>) : null;
}

function isFields(v: unknown): v is NoteFields {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** 409 proposal_stale：取出 base／current／proposed 三份與目前版本；內容不完整回 null（畫面改成只顯示訊息並重新載入） */
export function parseStale(err: unknown): ProposalStaleInfo | null {
  if (!(err instanceof ApiError) || err.code !== 'proposal_stale') return null;
  const e = errorOf(err);
  if (!e || !isFields(e.base) || !isFields(e.current) || !isFields(e.proposed) || typeof e.current_updated !== 'string') return null;
  return { base: e.base, current: e.current, proposed: e.proposed, current_updated: e.current_updated };
}

/** 代表「這則已被處理或狀態變了」的錯誤碼：畫面應關閉處理面板並重新載入佇列 */
const SETTLED_CODES = new Set(['proposal_state_conflict', 'proposal_resolved', 'proposal_executing', 'proposal_not_found']);

export function isSettledConflict(err: unknown): boolean {
  return err instanceof ApiError && SETTLED_CODES.has(err.code);
}
