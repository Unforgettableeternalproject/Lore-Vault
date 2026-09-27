// 給畫面用的文案與格式轉換：錯誤碼、摘要來源、文件狀態、locator、時間與大小。
import { ApiError } from './api';
import type { DocumentMeta, Locator, SummarySource } from './types';

// ── 錯誤 ──

const ERROR_TEXT: Record<string, string> = {
  network_error: '無法連線到 Lore Vault 服務',
  space_required: '請求缺少 space',
  invalid_space: 'space 不合法',
  vault_required: '此操作必須指定單一 vault',
  unknown_vault: '找不到這個 vault（或它屬於其他 space）',
  not_found: '找不到目標（可能已被刪除，或屬於其他 space）',
  no_changes: '沒有任何變更，未送出',
  invalid_characters: '內容含有不允許的控制字元',
  too_large: '檔案超過服務的大小上限',
  unsupported_format: '不支援的檔案格式',
  documents_not_configured: '服務未設定文件儲存目錄（documents.blob_dir），不能收文件',
  invalid_confirm_token: '確認憑證無效（參數與規劃不符），請重新規劃',
  confirm_token_expired: '確認已過期（5 分鐘），請重新規劃',
  plan_changed: '規劃後資料已變動，請確認新的規劃後重新送出',
  retry_limit: '已達人工重試上限，不再重試',
  not_failed: '這份文件不是失敗狀態，不需重試',
  csrf_required: '請求缺少 UI 標頭（CSRF 防護）',
  invalid_setting: '設定值不合法，未儲存任何變更',
  ui_session_required: '服務設定只能由登入 UI 的管理者查看與修改',
  episode_ingest_disabled: '服務未開啟 episode 收料',
  ask_disabled: '問答已由管理者在設定頁關閉',
  storage_error: '服務端儲存層錯誤',
  invalid_request: '請求參數不合法',
  invalid_cursor: '分頁游標無效，請從第一頁重新載入',
  vault_exists: 'key 或別名已被使用',
  space_key_prefix_required: '非 dev space 的 key 與別名必須以「<space>/」開頭',
  cannot_remove_key: '這是 vault 的正式 key，不能當別名移除',
  space_change_refused: '只允許 lore 與 personal 互換；dev 不與其他 space 轉換',
  vault_conflict: '目標 space 已有相同 key 的 vault',
  not_restorable: '無法復原',
};

const RESTORE_REASON: Record<string, string> = {
  vault_deleted: '所屬 vault 已被刪除',
  exists: '同一 id 已經存在',
  incomplete: '這是舊版墓碑，缺少復原所需的資料',
  blob_missing: '原始檔已不在（可能已被清理）',
  duplicate: '同 vault 已有相同內容的文件',
};

/** FastAPI 預設 422：`{detail: [{loc: ["body", "<欄位>"], type}]}`；回傳被拒的 body 欄位名。 */
export function rejectedFields(err: ApiError): string[] {
  if (err.status !== 422) return [];
  const body = err.body as { detail?: unknown } | null;
  if (!body || !Array.isArray(body.detail)) return [];
  const fields: string[] = [];
  for (const item of body.detail) {
    const loc = (item as { loc?: unknown })?.loc;
    if (Array.isArray(loc) && loc[0] === 'body' && typeof loc[1] === 'string') fields.push(loc[1]);
  }
  return fields;
}

/** A22 未上線的服務會以 422 extra_forbidden 拒絕 author 欄位。 */
export function isAuthorRejected(err: unknown): boolean {
  return err instanceof ApiError && rejectedFields(err).includes('author');
}

/**
 * 服務回給 UI 顯示的文字（doctor 摘要與說明、錯誤訊息）可能夾帶內部決策／工作編號（A22、T-69）
 * 或 schema 版本階段（v11 前）。使用者看不懂這些代號：顯示前去掉編號、把版本階段改成白話。
 * 只用在服務產生的說明文字，不用在使用者資料（標題、正文、vault 名）。
 */
const REF = String.raw`(?:[ATD]\d{1,3}|T-\d+)`;
export function stripInternalRefs(text: string): string {
  return text
    .replace(new RegExp(String.raw`\s*[（(]\s*${REF}(?:\s*[、,，]\s*${REF})*\s*[）)]`, 'g'), '')
    .replace(new RegExp(String.raw`([（(])\s*${REF}\s*[：:]\s*`, 'g'), '$1')
    .replace(/schema\s*v\d+\s*前/g, '舊版資料庫')
    .replace(/未遷移到\s*v\d+/g, '未遷移到最新版')
    .replace(/(?<![A-Za-z])v\d+\s*前/g, '舊版')
    .replace(/(?<![A-Za-z])v\d+\s*起/g, '新版起');
}

export function describeError(err: unknown): string {
  // 畫面自己產生的說明（例如前端檢查）直接顯示
  if (typeof err === 'string') return err;
  if (err instanceof ApiError) {
    if (isAuthorRejected(err)) {
      return '服務拒收 author 欄位（服務版本太舊，不支援作者署名）。UI 寫入一律署名，請更新服務後再試。';
    }
    if (err.status === 422) {
      const fields = rejectedFields(err);
      return `服務拒絕請求欄位${fields.length ? `：${fields.join('、')}` : ''}（HTTP 422）`;
    }
    if (err.code === 'not_restorable') {
      const reason = (err.body as { error?: { reason?: unknown } } | null)?.error?.reason;
      const text = typeof reason === 'string' ? (RESTORE_REASON[reason] ?? reason) : '原因不明';
      return `無法復原：${text}（not_restorable${typeof reason === 'string' ? ` · ${reason}` : ''}）`;
    }
    if (err.code === 'vault_exists') {
      const existing = (err.body as { error?: { existing?: { key?: unknown } | null } } | null)?.error?.existing;
      const owner =
        typeof existing?.key === 'string' ? `，已屬於 vault ${existing.key}` : existing === null ? '（佔用者在其他 space）' : '';
      return `${ERROR_TEXT.vault_exists}${owner}（vault_exists）`;
    }
    const known = ERROR_TEXT[err.code];
    const base = known ?? stripInternalRefs(err.message);
    return `${base}（${err.code}）`;
  }
  if (err instanceof Error) return `未預期的錯誤：${err.message}`;
  return '未預期的錯誤';
}

export function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

// ── 摘要來源 ──

export interface SourceTag {
  label: string;
  tone: 'zone' | 'gold' | 'neutral' | 'mute';
  note: string;
}

export function summarySource(source: SummarySource | string | undefined): SourceTag {
  switch (source) {
    case 'summary':
      return { label: '摘要', tone: 'zone', note: 'LLM 背景產生的摘要' };
    case 'lead':
      return { label: '首段', tone: 'gold', note: '摘要尚未產生，暫以正文首段頂替' };
    case 'excerpt':
      return { label: '摘錄', tone: 'neutral', note: '文件段落的原文摘錄' };
    case 'none':
      return { label: '無摘要', tone: 'mute', note: '沒有摘要，正文也沒有可頂替的首段' };
    case 'omitted':
      return { label: '摘要省略', tone: 'mute', note: '本頁摘要字數預算已用完，這則的摘要沒有列出' };
    default:
      return { label: `來源 ${String(source)}`, tone: 'mute', note: `未知的摘要來源：${String(source)}` };
  }
}

// ── 降級 ──

const DEGRADED_REASON: Record<string, string> = {
  embedder_unavailable: '語意模型無法連線或未設定',
  embedder_timeout: '語意模型逾時',
  embedder_error: '語意模型回傳錯誤',
  embedder_invalid_vector: '語意模型回傳的向量不合法',
};

/**
 * 最近一次檢索降級時頂列徽章的說法：依原因分開，逾時（多半是模型冷啟動）不說成離線。
 */
export function recallDegradedBadge(reason: string | null | undefined): { label: string; title: string } {
  switch (reason) {
    case 'embedder_timeout':
      return {
        label: '語意檢索逾時',
        title: '最近一次檢索時語意模型逾時（可能正在載入），那次只用了關鍵字比對；再查一次通常就會恢復。點此看系統健康。',
      };
    case 'embedder_unavailable':
      return { label: '語意檢索離線', title: '最近一次檢索連不上語意模型，只用了關鍵字比對。點此看系統健康。' };
    default:
      return {
        label: '語意檢索異常',
        title: `最近一次檢索的語意模型回應異常（${describeDegradedReason(reason)}），只用了關鍵字比對。點此看系統健康。`,
      };
  }
}

/** 系統健康的語意模型狀態：依 /api/ps 探測結果區分已載入／未載入（首次查詢較慢）／無法判斷。 */
export function describeModelLoaded(loaded: boolean | null | undefined): { label: string; note: string; tone: 'ok' | 'warn' | 'unknown' } {
  if (loaded === true) return { label: '已載入', note: '可立即查詢', tone: 'ok' };
  if (loaded === false) {
    return {
      label: '可連線，模型未載入',
      note: '下一次查詢會先載入模型，較慢（最長等待冷啟動逾時），不會因此降級',
      tone: 'warn',
    };
  }
  return { label: '無法確認', note: '服務無法查詢模型是否載入（可能連不上 Ollama）', tone: 'unknown' };
}

export function describeDegradedReason(reason: string | null | undefined): string {
  if (!reason) return '語意檢索不可用';
  return DEGRADED_REASON[reason] ? `${DEGRADED_REASON[reason]}（${reason}）` : reason;
}

// ── 文件 ──

export const DOCUMENT_ERROR_TEXT: Record<string, string> = {
  encrypted: '檔案已加密，無法抽取文字',
  corrupt: '檔案損毀或無法解析',
  empty_extraction: '沒有抽出文字（可能是掃描件、沒有文字層）',
  too_large: '超過大小或解壓上限',
  unsupported_format: '不支援的檔案格式',
  unsupported_encoding: '無法判定文字編碼',
};

export function documentErrorText(code: string | null): string {
  if (!code) return '抽取失敗（服務未附錯誤碼）';
  return DOCUMENT_ERROR_TEXT[code] ?? `抽取失敗（${code}）`;
}

export const DOCUMENT_WARNING_TEXT: Record<string, string> = {
  encoding_low_confidence: '編碼判定信心低',
};

export interface DocumentStatusView {
  label: string;
  tone: 'ok' | 'run' | 'fail' | 'unknown';
}

export function documentStatus(doc: Pick<DocumentMeta, 'status'>): DocumentStatusView {
  switch (doc.status) {
    case 'ready':
      return { label: '完成', tone: 'ok' };
    case 'pending':
      return { label: '排隊中', tone: 'run' };
    case 'extracting':
      return { label: '抽取中', tone: 'run' };
    case 'failed':
      return { label: '失敗', tone: 'fail' };
    default:
      return { label: `未知狀態 ${doc.status}`, tone: 'unknown' };
  }
}

export function isProcessing(doc: Pick<DocumentMeta, 'status'>): boolean {
  return doc.status === 'pending' || doc.status === 'extracting';
}

export function locatorLabel(locator: Locator | null | undefined): string {
  if (!locator) return '';
  const value = locator.value;
  let base: string;
  switch (locator.kind) {
    case 'page':
      base = `第 ${String(value)} 頁`;
      break;
    case 'slide':
      base = `投影片 ${String(value)}`;
      break;
    case 'heading':
      base = typeof value === 'string' && value ? value : '（無標題段）';
      break;
    case 'offset':
      base = `位置 ${String(value)}`;
      break;
    case 'header':
      base = '頁首';
      break;
    case 'footer':
      base = '頁尾';
      break;
    default:
      base = `${locator.kind} ${String(value ?? '')}`.trim();
  }
  return locator.part ? `${base} · 第 ${locator.part} 段` : base;
}

export function fileExt(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n)) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// ── 時間 ──

/** 服務端時間為 UTC ISO 字串；顯示為本地時間 `MM/DD HH:mm`（跨年加年份）。無法解析時原樣顯示。 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (x: number) => String(x).padStart(2, '0');
  const now = new Date();
  const md = `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()}/${md}`;
}

/** n 天前的 UTC ISO 字串（筆記列表 since 篩選）。 */
export function daysAgoIso(days: number, now: Date = new Date()): string {
  return new Date(now.getTime() - days * 86400_000).toISOString();
}

/** write／update 回應的未解析連結轉成一行說明（歧義附候選數）。 */
export function describeUnresolvedLink(link: { target: string; status: string; candidates?: string[] }): string {
  if (link.status === 'ambiguous') {
    return `[[${link.target}]]：同一 vault 有 ${link.candidates?.length ?? 0} 則同名筆記，無法判定要連哪一則`;
  }
  if (link.status === 'unresolved') return `[[${link.target}]]：同一 vault 找不到這個標題`;
  return `[[${link.target}]]：${link.status}`;
}

export function authorLabel(author: string | null | undefined): string {
  return typeof author === 'string' && author.trim() ? author : '未具名';
}
