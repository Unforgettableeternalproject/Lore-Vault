// /v1 回應形狀（對照 docs/ARCHITECTURE.md 與服務端 to_dict）。只宣告畫面用得到的欄位；
// 未知欄位保留在物件上不刪；A22 作者欄位（author／updated_by）宣告為可選（舊資料可能為 null）。

/** `omitted`：list 的摘要預算用完，這則的摘要被省略（summary 為 null） */
export type SummarySource = 'summary' | 'lead' | 'excerpt' | 'none' | 'omitted';

/** `/ui/api/session` 的 `limits`：與服務端實際檢查同一來源，取代前端寫死的上限。 */
export interface SessionLimits {
  max_file_bytes: number;
  max_chars: number;
  author_max_chars: number;
  get_max_ids: number;
  get_default_budget: number;
  list_max_limit: number;
  list_default_limit: number;
  list_default_budget: number;
  recall_max_limit: number;
  recall_default_limit: number;
  recall_default_budget: number;
}

/** write／update 回應裡解析不到（unresolved）或同 vault 多則同名（ambiguous，附候選 id）的 `[[ ]]`。 */
export interface UnresolvedLink {
  target: string;
  status: 'unresolved' | 'ambiguous' | string;
  candidates: string[];
}

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
  /** 同 vault 內 supersedes 指向它的 note（多則取最新）；衍生欄位 */
  superseded_by?: string | null;
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
  /** 開頭與前一段重疊的字數（段落起頭為 0）；串接顯示時略過 */
  overlap?: number;
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
  updated_by?: string | null;
  summary?: string | null;
  summary_source?: SummarySource;
  /** 摘要超過本頁公平分配的配額而被截短（結尾「…」）；完整內容用 get */
  summary_truncated?: boolean;
  supersedes?: string | null;
  superseded_by?: string | null;
}

export type ListItem = NoteListItem | DocumentMeta;

export interface ListResult<T = ListItem> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
  /** with_total 時：相同篩選下的總筆數與本頁起點 */
  total?: number;
  offset?: number;
  unsupported_kinds: string[];
  /** 本頁 note 摘要字數預算與用量；項目與分頁不受預算影響 */
  budget?: number;
  used_chars?: number;
  /** 有任何摘要被截短或省略 */
  truncated?: boolean;
  /** 預算連下限都給不起、摘要被省略（summary_source: omitted）的尾端 note 數 */
  summaries_omitted?: number;
  /** 超過配額、摘要被截短（summary_truncated）的 note 數 */
  summaries_truncated?: number;
}

/** `POST /v1/topics` */
export interface TopicsResult {
  space: string;
  vault: string;
  topics: { topic: string; count: number }[];
}

export interface DuplicateCandidate {
  id: string;
  title: string;
  updated: string;
  reasons: string[];
  lexical: number;
  vector: number | null;
}

/** `/v1/write`；`dry_run: true` 時沒有 id／updated／author／principal（沒有寫入）。 */
export interface WriteResult {
  id?: string;
  vault: string;
  updated?: string;
  author?: string | null;
  principal?: string;
  links?: string[];
  unresolved_links?: UnresolvedLink[];
  duplicates: DuplicateCandidate[];
  dedup_degraded: boolean;
  dedup_reason: string | null;
  dry_run?: boolean;
}

export interface UpdateResult {
  id: string;
  vault: string;
  updated: string;
  summary_stale: boolean;
  embedding_stale: boolean;
  links?: string[];
  unresolved_links?: UnresolvedLink[];
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

/** POST /v1/note_undelete：有內容快照時 restored=true 並附還原後的 note。 */
export interface NoteUndeleteResult {
  undeleted: Record<string, unknown>;
  restored: boolean;
  reimportable: boolean;
  note: { id: string; vault: string; title: string; author?: string | null; updated: string } | null;
}

/** 兩段式確認端點的回應。 */
export interface TwoPhaseResponse {
  executed: boolean;
  plan: Record<string, unknown>;
  confirm_token?: string;
  expires_at?: string;
  /** vault_move_space 執行後附新 space 的 vault */
  vault?: VaultSummary;
}

// ── 管理端點（T-70～T-75）──

/** `/v1/tombstones` 的一筆。note 與 document 欄位不同，共用欄位在前。 */
export interface TombstoneItem {
  kind: 'note' | 'document' | string;
  id: string;
  vault: string;
  vault_exists: boolean;
  deleted_at: string;
  reason: string | null;
  /** note：內容快照的標題（舊墓碑為 null） */
  title?: string | null;
  source?: string | null;
  restorable?: boolean;
  reimportable?: boolean;
  /** document */
  sha256?: string;
  filename?: string;
}

export interface TombstonePage {
  items: TombstoneItem[];
  next_cursor: string | null;
}

export interface DocumentUndeleteResult {
  document: DocumentMeta;
  space: string;
  tombstone: Record<string, unknown>;
}

/** `vault_move_space` 的規劃。counts 的 key 為「表.欄」。 */
export interface MovePlan {
  key: string;
  new_key: string;
  from: string;
  to: string;
  aliases: Record<string, string>;
  counts: Record<string, number>;
}

export interface ConceptItem {
  id: string;
  vault: string;
  kind: string;
  scope: string | null;
  scope_state: 'repo' | 'global' | 'missing' | string;
  statement: string;
  anchors: unknown;
  surprisal: number | null;
  usability_verdict: string | null;
  updated: string;
}

export interface ConceptPage {
  items: ConceptItem[];
  next_cursor: string | null;
  /** with_total 時的總筆數 */
  total?: number;
}

export interface EpisodeGroup {
  machine?: string | null;
  vault?: string | null;
  episodes: number;
  last_recorded: string | null;
  last_started: string | null;
}

export interface EpisodeSummary {
  space: string;
  vault: string;
  total: number;
  last_recorded: string | null;
  by_machine: EpisodeGroup[];
  by_vault: EpisodeGroup[];
}

// ── /v1/status ──

export type CheckStatus = 'pass' | 'warn' | 'fail' | 'skipped';

export interface DoctorCheck {
  name: string;
  category: string;
  description: string;
  status: CheckStatus | string;
  summary: string;
  details: string[];
  counts: Record<string, number>;
}

export interface DoctorReport {
  ok: boolean;
  exit_code: number;
  summary: { total: number } & Partial<Record<CheckStatus, number>>;
  checks: DoctorCheck[];
}

export interface WorkerStatus {
  enabled: boolean;
  running: boolean;
  stopping?: boolean;
  runs?: number;
  last_run?: string | null;
  last_error?: string | null;
  fatal_error?: string | null;
  [key: string]: unknown;
}

export interface BacklogStatus {
  status: string;
  summary: string;
  counts: Record<string, number>;
}

export interface WarmupStatus {
  status: 'disabled' | 'pending' | 'running' | 'retrying' | 'ok' | 'failed' | string;
  started_at: string | null;
  finished_at: string | null;
  elapsed_ms: number | null;
  error: string | null;
  /** 已嘗試次數（連線類失敗會退避重試）；舊版服務沒有 */
  attempts?: number;
}

export interface StatusResult {
  ok: boolean;
  checked_at: string;
  schema: { version: number; expected: number };
  space: string | null;
  /** model_loaded：Ollama 目前是否載入模型（/api/ps）；null＝無法判斷；舊版服務沒有這個欄位 */
  embedding: { warmup: WarmupStatus; model_loaded?: boolean | null };
  enrich: { worker: WorkerStatus; backlog: BacklogStatus };
  documents: { enabled: boolean; worker: WorkerStatus; backlog: BacklogStatus };
  doctor: DoctorReport;
}

// ── 問答（/v1/ask，D11）：recall 的 note 片段交模型整理成逐點回答 ──

export interface AskPoint {
  claim: string;
  /** 支持這一點的 note id（已過引用防呆，只會是 sources 內的 id） */
  note_ids: string[];
  /** 沒有任何有效引用的點：保留但標為無依據 */
  unsupported: boolean;
}

export interface AskSource {
  id: string;
  vault: string;
  title: string;
  updated: string;
  score: number;
  excerpt_truncated: boolean;
}

export interface AskResult {
  /** answered／insufficient（片段不足以回答） */
  status: 'answered' | 'insufficient' | string;
  answer: { points: AskPoint[] };
  dropped_citations: unknown[];
  /** 模型回 answered 但沒有任何有效引用，被改判為 insufficient */
  status_downgraded: boolean;
  sources: AskSource[];
  k: number;
  kinds: string[];
  unsupported_kinds: string[];
  degraded: boolean;
  degraded_reason: string | null;
  degraded_detail: string | null;
  missing_embeddings: number | null;
  model: string | null;
  usage: Record<string, number> | null;
  latency_ms: { retrieval: number; generation: number | null; total: number };
  notice: string;
}

// ── 執行期服務設定（GET /v1/settings；只允許 UI session）──

export type SettingType = 'bool' | 'int' | 'float';
export type SettingValue = boolean | number;

export interface SettingItem {
  key: string;
  type: SettingType;
  /** 分類代號（對應 categories 的 id） */
  category: string;
  label: string;
  description: string;
  min: number | null;
  max: number | null;
  unit: string | null;
  /** 目前生效的值 */
  value: SettingValue;
  /** 設定檔／環境變數的值（還原預設後會回到它） */
  default: SettingValue;
  source: 'default' | 'override';
  override: { updated: string; updated_by: string } | null;
}

export interface SettingsAuditEntry {
  seq: number;
  at: string;
  key: string;
  action: 'set' | 'reset';
  old_value: SettingValue;
  new_value: SettingValue;
  principal: string;
  display: string | null;
}

export interface SettingsResult {
  categories: { id: string; label: string }[];
  items: SettingItem[];
  /** 資料庫裡不合法、執行期被略過的覆寫（健康檢查會標紅） */
  invalid_overrides: { key: string; reason: string; updated: string }[];
  /** 最近的修改紀錄（新到舊） */
  audit: SettingsAuditEntry[];
  /** 修改／還原回應才有：這次寫入的紀錄 */
  changed?: SettingsAuditEntry[];
}

/** `invalid_setting` 錯誤的逐項明細 */
export interface SettingError {
  key: string;
  code: string;
  message: string;
}

// ── 通用側載（POST /v1/blob_get；TASK_LAYER_UI §3）──

/** `blob_get` 帶 `vault`：該 vault 存的一份內容；不存在回 404 `not_found` */
export interface BlobRecord {
  mime: string;
  content_base64: string;
  updated: string;
  /** 側載版本（每次 put 遞增；schema v18 起服務必回） */
  version?: number;
}

/** `blob_get` 省略 `vault`：本 space 內所有存過該 key 的 vault（可能是空陣列） */
export interface BlobListResult {
  items: (BlobRecord & { vault: string })[];
}

// ── 任務層快照（側載 key `tasks-snapshot` 的 JSON 內容）──
// 這是 UI 端對任務層 CLI（UI-T2）推送內容的契約；CLI 序列化時以此為準。
// 只含推導結果，不含 spec delta 全文、tasks.md 逐項文字（語料邊界，TASK_LAYER_UI §1.2）。

/** 推導狀態：沿用任務層 `workspace.py` 的五個常數字串 */
export type TaskStatus = '可開工' | '被擋住' | '待授權' | '已完成' | '無法判定';

export interface TaskBlocker {
  /** D 編號，例如 `D6` */
  id: string;
  /** true＝已裁決、false＝未裁決、null＝無法判定（DECISIONS.md 找不到或沒有此小節） */
  resolved: boolean | null;
}

export interface TaskDependency {
  /** 依賴的 change 名稱 */
  name: string;
  archived: boolean;
}

export interface TaskSpecDelta {
  capability: string;
  requirement: string;
  op: 'ADDED' | 'MODIFIED' | 'REMOVED' | string;
}

export interface TaskChange {
  name: string;
  status: TaskStatus | string;
  /** derive_status 回的原因（被擋住／無法判定／待授權的說明） */
  reasons: string[];
  blocked_by: TaskBlocker[];
  depends_on: TaskDependency[];
  requires_authorization: boolean;
  tasks: { done: number; total: number };
  /** 對應 TASKS.md 卡號，無則 null */
  source: string | null;
  /** proposal.md 的「Why」摘要段（簡單 Markdown） */
  why: string | null;
  specs: TaskSpecDelta[];
  /** 已封存且寫了總結 note 時的 note id */
  note_id: string | null;
  archived_at: string | null;
  /** 服務端計算的快照才有：UI 核准有效（內容雜湊相符）時的核准人與時間，否則 null */
  approved?: { by: string; at: string } | null;
}

export interface TaskSnapshot {
  /** 快照格式版本，目前為 1 */
  schema: number;
  /** CLI 產生快照的時間（ISO）；同步時間以側載 `updated` 為準 */
  generated_at?: string;
  changes: TaskChange[];
}
