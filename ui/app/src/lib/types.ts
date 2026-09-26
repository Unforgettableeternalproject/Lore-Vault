// /v1 回應形狀（對照 docs/ARCHITECTURE.md 與服務端 to_dict）。只宣告畫面用得到的欄位；
// 未知欄位保留在物件上不刪，`author`（A22）等尚未上線的欄位宣告為可選。

export type SummarySource = 'summary' | 'lead' | 'excerpt' | 'none';

export interface Locator {
  kind: 'heading' | 'page' | 'slide' | 'offset' | 'header' | 'footer' | string;
  value: unknown;
  part?: number;
}

export interface VaultSummary {
  key: string;
  display: string;
  kind: string;
  space: string;
  origin?: string;
  aliases: string[];
  note_count: number;
  document_count?: number;
  created?: string;
  last_updated?: string | null;
}

export interface RecallItem {
  id: string;
  kind: 'note' | 'chunk' | string;
  vault: string;
  title: string;
  summary: string | null;
  summary_source: SummarySource;
  score: number;
  updated: string;
  document_id?: string;
  chunk_id?: string;
  locator?: Locator;
  author?: string | null;
}

export interface RecallResult {
  items: RecallItem[];
  mode: string;
  legs: string[];
  degraded: boolean;
  degraded_reason: string | null;
  degraded_detail: string | null;
  truncated: boolean;
  omitted: number;
  budget: number;
  used_chars: number;
  unsupported_kinds: string[];
  missing_embeddings: number | null;
  kinds: string[];
  missing_chunk_embeddings: number | null;
}

export interface NoteFull {
  id: string;
  kind: 'note';
  vault: string;
  title: string;
  summary: string | null;
  summary_source: SummarySource;
  body: string;
  body_chars: number;
  truncated: boolean;
  topics: string[];
  links: string[];
  supersedes: string | null;
  created: string;
  updated: string;
  /** A22：原作者（寫入者自報）；最後修改者另記 updated_by */
  author?: string | null;
  updated_by?: string | null;
}

export interface DocumentWarning {
  code: string;
  detail: string;
}

export type DocumentStatus = 'pending' | 'extracting' | 'ready' | 'failed';

export interface DocumentMeta {
  id: string;
  kind: 'document';
  vault: string;
  title: string;
  filename: string;
  mime: string | null;
  size_bytes: number;
  status: DocumentStatus | string;
  error_code: string | null;
  error_detail: string | null;
  version: number;
  supersedes: string | null;
  superseded_by: string | null;
  chunk_count: number | null;
  encoding: string | null;
  warnings: DocumentWarning[];
  created: string;
  updated: string;
}

export interface DocumentFull extends DocumentMeta {
  text: string;
  text_chars: number;
  truncated: boolean;
}

export interface ChunkFull {
  id: string;
  kind: 'chunk';
  document_id: string;
  vault: string;
  title: string;
  locator: Locator;
  text: string;
  text_chars: number;
  truncated: boolean;
  superseded_by: string | null;
  updated: string;
}

export interface GetResult<T = NoteFull | DocumentFull | ChunkFull> {
  items: T[];
  missing: string[];
  unavailable: string[];
  truncated: boolean;
  budget: number;
  used_chars: number;
}

export interface NoteListItem {
  id: string;
  kind: 'note';
  vault: string;
  title: string;
  topics: string[];
  updated: string;
  author?: string | null;
  summary?: string | null;
  summary_source?: SummarySource;
}

export type ListItem = NoteListItem | DocumentMeta;

export interface ListResult<T = ListItem> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
  unsupported_kinds: string[];
}

export interface DuplicateCandidate {
  id: string;
  title: string;
  updated: string;
  reasons: string[];
  lexical: number;
  vector: number | null;
}

export interface WriteResult {
  id: string;
  vault: string;
  updated: string;
  duplicates: DuplicateCandidate[];
  dedup_degraded: boolean;
  dedup_reason: string | null;
}

export interface UpdateResult {
  id: string;
  vault: string;
  updated: string;
  summary_stale: boolean;
  embedding_stale: boolean;
}

/** 409 version_conflict 附的目前版本（不含 body）。 */
export interface ConflictCurrent {
  id: string;
  vault: string;
  title: string;
  topics: string[];
  links: string[];
  supersedes: string | null;
  created: string;
  updated: string;
  author?: string | null;
  updated_by?: string | null;
}

export interface UploadResult {
  document_id: string;
  status: DocumentStatus | string;
  sha256: string;
  duplicate: boolean;
  retried: boolean;
  vault: string;
  space: string;
  filename: string;
  version: number;
  supersedes: string | null;
  size_bytes: number;
}

export interface DocumentRetryResult {
  document: Record<string, unknown>;
  space: string;
  manual_retries: number;
  max_manual_retries: number;
}

/** 兩段式確認端點的回應。 */
export interface TwoPhaseResponse {
  executed: boolean;
  plan: Record<string, unknown>;
  confirm_token?: string;
  expires_at?: string;
}
