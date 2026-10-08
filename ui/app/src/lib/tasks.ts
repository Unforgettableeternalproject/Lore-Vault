// 任務層快照（TASK_LAYER_UI）：從通用側載 `/v1/blob_get` 讀回各 vault 的 `tasks-snapshot`，
// 解碼（base64 → UTF-8 JSON）、檢查形狀，並提供列表篩選與狀態呈現用的小工具。
// 格式不對的快照不丟棄：以該 vault 的錯誤呈現（對應 doctor 的 tasks.snapshot_shape），不讓錯誤靜默。
import { ApiError, type ApiClient } from './api';
import { ALL } from './context';
import type { BadgeTone } from '../components/ui';
import type { BlobListResult, BlobRecord, TaskChange, TaskSnapshot, TaskStatus } from './types';

/** 任務層固定使用的側載 key */
export const TASKS_SNAPSHOT_KEY = 'tasks-snapshot';

/** 同步時間超過這個時數標「可能已過時」（純時間判斷，不是內容判斷） */
export const TASK_STALE_HOURS = 24;

export const STATUS_READY: TaskStatus = '可開工';
export const STATUS_BLOCKED: TaskStatus = '被擋住';
export const STATUS_AUTH: TaskStatus = '待授權';
export const STATUS_DONE: TaskStatus = '已完成';
export const STATUS_UNKNOWN: TaskStatus = '無法判定';

/** 篩選分組：「無法判定」與認不得的狀態併入「被擋住」（都需要人介入），列上仍分開顯示 */
export type TaskGroup = 'ready' | 'blocked' | 'auth' | 'done';

export function statusGroup(status: string): TaskGroup {
  switch (status) {
    case STATUS_READY:
      return 'ready';
    case STATUS_AUTH:
      return 'auth';
    case STATUS_DONE:
      return 'done';
    default:
      return 'blocked';
  }
}

export interface StatusView {
  label: string;
  tone: BadgeTone;
  /** 認不得的狀態字串（快照比 UI 新，或內容有誤） */
  unrecognized: boolean;
}

export function statusView(status: string): StatusView {
  switch (status) {
    case STATUS_READY:
      return { label: status, tone: 'ready', unrecognized: false };
    case STATUS_BLOCKED:
      return { label: status, tone: 'warn', unrecognized: false };
    case STATUS_AUTH:
      return { label: status, tone: 'auth', unrecognized: false };
    case STATUS_DONE:
      return { label: status, tone: 'plain', unrecognized: false };
    case STATUS_UNKNOWN:
      return { label: status, tone: 'error', unrecognized: false };
    default:
      return { label: `未知狀態：${status}`, tone: 'error', unrecognized: true };
  }
}

/** 某個 vault 的快照（或它無法解讀的原因） */
export interface VaultSnapshot {
  vault: string;
  /** 側載的 `updated`（最後一次同步時間） */
  updated: string;
  snapshot: TaskSnapshot | null;
  /** 快照無法解讀時的說明；有值時 snapshot 為 null */
  error: string | null;
}

/** base64 → UTF-8 字串。`atob` 只給 Latin-1，中文內容要先轉位元組再解碼。 */
export function decodeBase64Utf8(b64: string): string {
  const bin = atob(b64);
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function strArray(v: unknown): v is string[] {
  return Array.isArray(v) && v.every((x) => typeof x === 'string');
}

function optString(v: unknown): v is string | null | undefined {
  return v === undefined || v === null || typeof v === 'string';
}

/** 檢查單一 change 的形狀；錯誤回說明字串，正確回正規化後的物件（缺的選填欄位補預設）。 */
function parseChange(raw: unknown, index: number): TaskChange | string {
  const at = `changes[${index}]`;
  if (!isRecord(raw)) return `${at} 不是物件`;
  const { name, status } = raw;
  if (typeof name !== 'string' || !name) return `${at} 缺少 name`;
  if (typeof status !== 'string' || !status) return `${at}（${name}）缺少 status`;
  const reasons = raw.reasons ?? [];
  if (!strArray(reasons)) return `${at}（${name}）的 reasons 格式不正確`;
  const blocked = raw.blocked_by ?? [];
  if (
    !Array.isArray(blocked) ||
    !blocked.every((b) => isRecord(b) && typeof b.id === 'string' && (b.resolved === null || typeof b.resolved === 'boolean'))
  ) {
    return `${at}（${name}）的 blocked_by 格式不正確`;
  }
  const deps = raw.depends_on ?? [];
  if (!Array.isArray(deps) || !deps.every((d) => isRecord(d) && typeof d.name === 'string' && typeof d.archived === 'boolean')) {
    return `${at}（${name}）的 depends_on 格式不正確`;
  }
  const tasks = raw.tasks ?? { done: 0, total: 0 };
  if (!isRecord(tasks) || typeof tasks.done !== 'number' || typeof tasks.total !== 'number') {
    return `${at}（${name}）的 tasks 格式不正確`;
  }
  const specs = raw.specs ?? [];
  if (
    !Array.isArray(specs) ||
    !specs.every((s) => isRecord(s) && typeof s.capability === 'string' && typeof s.requirement === 'string' && typeof s.op === 'string')
  ) {
    return `${at}（${name}）的 specs 格式不正確`;
  }
  if (!optString(raw.source) || !optString(raw.why) || !optString(raw.note_id) || !optString(raw.archived_at)) {
    return `${at}（${name}）的文字欄位格式不正確`;
  }
  if (raw.requires_authorization !== undefined && typeof raw.requires_authorization !== 'boolean') {
    return `${at}（${name}）的 requires_authorization 格式不正確`;
  }
  return {
    ...(raw as object),
    name,
    status,
    reasons,
    blocked_by: blocked as TaskChange['blocked_by'],
    depends_on: deps as TaskChange['depends_on'],
    requires_authorization: raw.requires_authorization === true,
    tasks: { done: tasks.done, total: tasks.total },
    source: (raw.source as string | null | undefined) ?? null,
    why: (raw.why as string | null | undefined) ?? null,
    specs: specs as TaskChange['specs'],
    note_id: (raw.note_id as string | null | undefined) ?? null,
    archived_at: (raw.archived_at as string | null | undefined) ?? null,
  };
}

/** 解讀一份側載內容：成功回快照，失敗回說明。 */
export function parseSnapshot(record: Pick<BlobRecord, 'mime' | 'content_base64'>): TaskSnapshot | string {
  let text: string;
  try {
    text = decodeBase64Utf8(record.content_base64);
  } catch {
    return '內容不是有效的 base64／UTF-8';
  }
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return '內容不是有效的 JSON';
  }
  if (!isRecord(data)) return '快照不是 JSON 物件';
  if (typeof data.schema !== 'number') return '快照缺少 schema 版本';
  if (data.schema !== 1) return `不支援的快照格式版本 ${data.schema}（介面只認得 1）`;
  if (!Array.isArray(data.changes)) return '快照缺少 changes 清單';
  const changes: TaskChange[] = [];
  for (const [i, raw] of data.changes.entries()) {
    const parsed = parseChange(raw, i);
    if (typeof parsed === 'string') return parsed;
    changes.push(parsed);
  }
  return {
    schema: data.schema,
    generated_at: typeof data.generated_at === 'string' ? data.generated_at : undefined,
    changes,
  };
}

function toVaultSnapshot(vault: string, record: BlobRecord): VaultSnapshot {
  const parsed = parseSnapshot(record);
  return typeof parsed === 'string'
    ? { vault, updated: record.updated, snapshot: null, error: parsed }
    : { vault, updated: record.updated, snapshot: parsed, error: null };
}

/**
 * 取回快照。`vault` 為 `*` 時一次取本 space 全部（省略 vault）；
 * 單一 vault 時 404 `not_found` 代表「尚未同步」，回空陣列。其他錯誤（含端點不存在的 404）照常丟出。
 */
export async function fetchTaskSnapshots(
  api: ApiClient,
  space: string,
  vault: string,
  signal?: AbortSignal,
): Promise<VaultSnapshot[]> {
  if (vault === ALL) {
    const { data } = await api.post<BlobListResult>('/v1/blob_get', { space, key: TASKS_SNAPSHOT_KEY }, signal);
    return data.items.map((item) => toVaultSnapshot(item.vault, item));
  }
  try {
    const { data } = await api.post<BlobRecord>('/v1/blob_get', { space, vault, key: TASKS_SNAPSHOT_KEY }, signal);
    return [toVaultSnapshot(vault, data)];
  } catch (err) {
    if (err instanceof ApiError && err.status === 404 && err.code === 'not_found') return [];
    throw err;
  }
}

export function isStale(updated: string | null | undefined, now: Date = new Date()): boolean {
  if (!updated) return true;
  const t = Date.parse(updated);
  if (Number.isNaN(t)) return true;
  return (now.getTime() - t) / 3_600_000 > TASK_STALE_HOURS;
}

export interface TaskRow {
  vault: string;
  updated: string;
  change: TaskChange;
}

export function taskRows(snapshots: VaultSnapshot[]): TaskRow[] {
  return snapshots.flatMap((s) => (s.snapshot ? s.snapshot.changes.map((change) => ({ vault: s.vault, updated: s.updated, change })) : []));
}

/** 篩選值：空字串＝未完成（預設，可開工＋被擋住＋待授權），`all`＝含已完成的全部 */
export type TaskFilter = '' | TaskGroup | 'all';

export const TASK_FILTERS: readonly { id: TaskFilter; label: string }[] = [
  { id: '', label: '未完成' },
  { id: 'ready', label: '可開工' },
  { id: 'blocked', label: '被擋住' },
  { id: 'auth', label: '待授權' },
  { id: 'done', label: '已完成' },
  { id: 'all', label: '全部' },
];

export const TASK_FILTER_IDS: readonly TaskFilter[] = TASK_FILTERS.map((f) => f.id);

export function filterRows(rows: TaskRow[], filter: TaskFilter): TaskRow[] {
  if (filter === 'all') return rows;
  if (filter === '') return rows.filter((r) => statusGroup(r.change.status) !== 'done');
  return rows.filter((r) => statusGroup(r.change.status) === filter);
}

export function blockerLabel(resolved: boolean | null): string {
  return resolved === true ? '已裁決' : resolved === false ? '未裁決' : '無法判定';
}

// ── 人類核准（TASK_LAYER_MCP §3.3、MCP-T5）──
// 服務端的 change 全文（`task-change:<name>`）與 UI 核准寫入的授權紀錄（`task-authorization:<name>`），
// 格式見 `lore_vault.tasks.remote_store`。快照只給列表與推導狀態；核准要對準「服務端目前版本」，
// 所以版本與核准狀態另讀這兩份側載。核准只能經 `/v1/tasks_authorize`（UI session 限定）。

export const TASK_CHANGE_PREFIX = 'task-change:';
export const TASK_AUTHORIZATION_PREFIX = 'task-authorization:';

/** 服務端 change 的核准相關欄位（全文其他欄位 UI 不讀） */
export interface RemoteChange {
  version: number;
  /** `active`／`pending_apply`（只有 active 可以核准） */
  state: string;
  requiresAuthorization: boolean;
}

export interface AuthorizationRecord {
  changeVersion: number;
  authorizedBy: string;
  authorizedAt: string;
}

/** 核准狀態：服務端沒有 change（無法核准）、尚未核准、已核准目前版本、核准的是舊版本（之後又改過） */
export type ApprovalState = 'no-change' | 'none' | 'approved' | 'stale';

function decodeJson(record: Pick<BlobRecord, 'content_base64'>): Record<string, unknown> | null {
  try {
    const data: unknown = JSON.parse(decodeBase64Utf8(record.content_base64));
    return isRecord(data) ? data : null;
  } catch {
    return null;
  }
}

export function parseRemoteChange(record: BlobRecord): RemoteChange | string {
  const data = decodeJson(record);
  if (!data || data.schema !== 1) return '服務端的 change 內容格式不正確';
  if (typeof record.version !== 'number') return '服務沒有回傳 change 版本';
  const meta = isRecord(data.meta) ? data.meta : {};
  return {
    version: record.version,
    state: typeof data.state === 'string' ? data.state : '',
    requiresAuthorization: meta.requires_authorization === true,
  };
}

export function parseAuthorization(record: BlobRecord): AuthorizationRecord | string {
  const data = decodeJson(record);
  if (!data || data.schema !== 1) return '授權紀錄格式不正確';
  const principal = data.principal;
  const version = data.change_version;
  if (typeof version !== 'number' || !Number.isInteger(version) || version < 1) return '授權紀錄缺少 change_version';
  if (typeof data.authorized_by !== 'string' || !data.authorized_by) return '授權紀錄缺少 authorized_by';
  if (!isRecord(principal) || principal.kind !== 'ui_session') return '授權紀錄不是 UI 核准';
  return {
    changeVersion: version,
    authorizedBy: data.authorized_by,
    authorizedAt: typeof data.authorized_at === 'string' ? data.authorized_at : '',
  };
}

async function getBlobOrNull(api: ApiClient, vault: string, key: string, signal?: AbortSignal): Promise<BlobRecord | null> {
  try {
    const { data } = await api.post<BlobRecord>('/v1/blob_get', { space: 'dev', vault, key }, signal);
    return data;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404 && err.code === 'not_found') return null;
    throw err;
  }
}

export interface ApprovalInfo {
  change: RemoteChange | null;
  record: AuthorizationRecord | null;
  /** 任一份內容格式不正確時的說明（不靜默當成「未核准」） */
  error: string | null;
}

/** 讀服務端 change 與授權紀錄（兩次 blob_get，各自 404＝不存在）。 */
export async function fetchApproval(api: ApiClient, vault: string, name: string, signal?: AbortSignal): Promise<ApprovalInfo> {
  const [changeBlob, recordBlob] = await Promise.all([
    getBlobOrNull(api, vault, TASK_CHANGE_PREFIX + name, signal),
    getBlobOrNull(api, vault, TASK_AUTHORIZATION_PREFIX + name, signal),
  ]);
  const change = changeBlob ? parseRemoteChange(changeBlob) : null;
  const record = recordBlob ? parseAuthorization(recordBlob) : null;
  const error = typeof change === 'string' ? change : typeof record === 'string' ? record : null;
  return {
    change: typeof change === 'string' ? null : change,
    record: typeof record === 'string' ? null : record,
    error,
  };
}

export function approvalState(info: Pick<ApprovalInfo, 'change' | 'record'>): ApprovalState {
  if (!info.change) return 'no-change';
  if (!info.record) return 'none';
  return info.record.changeVersion === info.change.version ? 'approved' : 'stale';
}

/** 服務端 change 可以核准：存在、進行中、標記 requires_authorization */
export function canApprove(change: RemoteChange | null): boolean {
  return change !== null && change.state === 'active' && change.requiresAuthorization;
}

export async function approveChange(api: ApiClient, vault: string, name: string): Promise<void> {
  await api.post('/v1/tasks_authorize', { space: 'dev', vault, change: name });
}
