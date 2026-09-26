// 文件列表與上傳（T-81）：拖放／選檔（前端先擋大小上限）→ 逐檔上傳進度與結果（新版本／重複／重試），
// 列表顯示抽取狀態（處理中自動輪詢）、失敗原因與重試、編碼警示、版本與「已被取代」、兩段式刪除。
import { useEffect, useRef, useState } from 'preact/hooks';

import { Banner, EmptyState, ErrorState, Loading, TwoPhaseDelete } from '../components/ui';
import { DateRange, DEFAULT_PAGE_SIZE, Pager, rangeParams, type DateRangeValue } from '../components/Pager';
import { VaultPicker } from '../components/VaultPicker';
import { ApiError } from '../lib/api';
import { ALL, useApp, vaultName } from '../lib/context';
import {
  describeError,
  documentErrorText,
  documentStatus,
  DOCUMENT_WARNING_TEXT,
  fileExt,
  formatBytes,
  formatTime,
  isAbort,
  isProcessing,
} from '../lib/format';
import { routePath } from '../lib/router';
import type { DocumentMeta, DocumentRetryResult, ListResult, UploadResult } from '../lib/types';

export const POLL_MS = 2000;
const ACCEPT = '.md,.markdown,.txt,.pdf,.docx,.pptx,.json,.yaml,.yml,.toml';

export interface UploadEntry {
  key: number;
  name: string;
  size: number;
  state: 'rejected' | 'queued' | 'uploading' | 'done' | 'error';
  progress: number;
  message: string;
  result?: UploadResult;
}

export function describeUpload(r: UploadResult): string {
  if (r.duplicate) return `內容與既有文件相同，沿用 v${r.version}，未重新處理`;
  if (r.retried) return `沿用先前失敗的同內容文件，重新排入抽取（v${r.version}）`;
  if (r.supersedes) return `新版本 v${r.version}：取代同檔名的前一版，排入抽取`;
  return `已上傳 v${r.version}，排入抽取`;
}

export function Docs() {
  const { api, space, vault, vaults, navigate, toast, refreshVaults, limits } = useApp();
  // 單檔上限取服務設定（session limits.max_file_bytes）；前端先擋，服務端仍以 413 為準
  const maxBytes = limits.max_file_bytes;
  const [target, setTarget] = useState(vault !== ALL ? vault : '');
  const [page, setPage] = useState<ListResult<DocumentMeta> | null>(null);
  const [range, setRange] = useState<DateRangeValue>({ from: '', to: '' });
  const [pageNo, setPageNo] = useState(1);
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [uploads, setUploads] = useState<UploadEntry[]>([]);
  const [dragging, setDragging] = useState(false);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<DocumentMeta | null>(null);
  const [lastDeleted, setLastDeleted] = useState<DocumentMeta | null>(null);
  const [undeleting, setUndeleting] = useState(false);
  const seq = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (vault !== ALL) setTarget(vault);
  }, [vault]);
  // 篩選或每頁筆數變了回第一頁
  useEffect(() => setPageNo(1), [vault, range.from, range.to, pageSize]);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    api
      .post<ListResult<DocumentMeta>>(
        '/v1/list',
        {
          space: space.id,
          vault,
          kinds: ['document'],
          limit: pageSize,
          offset: (pageNo - 1) * pageSize,
          with_total: true,
          ...rangeParams(range),
        },
        ctrl.signal,
      )
      .then(({ data }) => {
        setPage(data);
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, space.id, vault, pageNo, pageSize, range.from, range.to, tick]);

  // 有處理中的文件就輪詢列表
  const processing = page?.items.some(isProcessing) ?? false;
  useEffect(() => {
    if (!processing) return;
    const timer = window.setTimeout(() => setTick((t) => t + 1), POLL_MS);
    return () => window.clearTimeout(timer);
  }, [processing, page]);

  const patch = (key: number, next: Partial<UploadEntry>) =>
    setUploads((all) => all.map((u) => (u.key === key ? { ...u, ...next } : u)));

  const addFiles = async (files: File[]) => {
    if (files.length === 0) return;
    if (!target) {
      toast('請先選擇要上傳到哪個 vault', 'warning');
      return;
    }
    const entries: { entry: UploadEntry; file: File }[] = files.map((file) => {
      const tooBig = file.size > maxBytes;
      return {
        file,
        entry: {
          key: ++seq.current,
          name: file.name,
          size: file.size,
          state: tooBig ? 'rejected' : 'queued',
          progress: 0,
          message: tooBig ? `超過 ${formatBytes(maxBytes)} 上限，未上傳` : '等待上傳',
        },
      };
    });
    setUploads((all) => [...entries.map((e) => e.entry), ...all]);
    let changed = false;
    for (const { entry, file } of entries) {
      if (entry.state === 'rejected') continue;
      patch(entry.key, { state: 'uploading', message: '上傳中…' });
      const form = new FormData();
      form.append('file', file, file.name);
      form.append('vault', target);
      form.append('space', space.id);
      try {
        const { data } = await api.upload<UploadResult>('/v1/documents', form, {
          onProgress: (f) => patch(entry.key, { progress: f }),
        });
        patch(entry.key, { state: 'done', progress: 1, message: describeUpload(data), result: data });
        changed = true;
      } catch (err) {
        patch(entry.key, { state: 'error', message: describeError(err) });
      }
    }
    if (changed) {
      refreshVaults();
      setTick((t) => t + 1);
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const files = Array.from(e.dataTransfer?.files ?? []);
    void addFiles(files);
  };

  const retry = async (doc: DocumentMeta) => {
    setRetrying(doc.id);
    try {
      const { data } = await api.post<DocumentRetryResult>('/v1/document_retry', {
        space: space.id,
        vault: doc.vault,
        id: doc.id,
      });
      toast(`已重新排入抽取（人工重試 ${data.manual_retries}/${data.max_manual_retries}）`, 'success');
      setTick((t) => t + 1);
    } catch (err) {
      const extra =
        err instanceof ApiError && err.code === 'retry_limit' ? '：每份文件最多人工重試 3 次，請檢查原始檔後重新上傳' : '';
      toast(`重試失敗：${describeError(err)}${extra}`, 'error');
    } finally {
      setRetrying(null);
    }
  };

  const undelete = async (doc: DocumentMeta) => {
    setUndeleting(true);
    try {
      await api.post('/v1/document_undelete', { space: space.id, id: doc.id });
      toast(`已復原「${doc.filename}」，重新排入抽取`, 'success');
      setLastDeleted(null);
      refreshVaults();
      setTick((t) => t + 1);
    } catch (err) {
      toast(describeError(err), 'error');
    } finally {
      setUndeleting(false);
    }
  };

  const items = page?.items ?? [];
  const byId = new Map(items.map((d) => [d.id, d]));

  return (
    <section class="lv-screen lv-screen--wide" aria-labelledby="lv-docs-title">
      <div class="lv-eyebrow">
        DOCUMENTS · {space.en} / {vaultName({ vaults }, vault)}
      </div>
      <h1 id="lv-docs-title" class="lv-title">
        文件
      </h1>
      <section class="lv-filter-panel" aria-label="篩選條件" data-testid="filter-panel">
        <div class="lv-filter-panel__row">
          <VaultPicker />
          <DateRange value={range} onChange={setRange} />
        </div>
      </section>

      <div
        class={'lv-drop' + (dragging ? ' is-dragging' : '')}
        data-testid="dropzone"
        onDragEnter={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
        }}
        onDragLeave={(e) => {
          if (e.currentTarget === e.target) setDragging(false);
        }}
        onDrop={onDrop}
      >
        <span class="lv-drop__plus" aria-hidden="true">
          +
        </span>
        <div class="lv-drop__text">
          <div class="lv-drop__title">把檔案拖到這裡</div>
          <div class="lv-drop__hint">
            md · txt · pdf · docx · pptx · json · yaml · toml · 單檔上限 {formatBytes(maxBytes)} · 同名檔案會成為新版本
          </div>
          <label class="lv-drop__target">
            <span>上傳到</span>
            <select class="lv-input lv-select" value={target} onChange={(e) => setTarget((e.target as HTMLSelectElement).value)} aria-label="上傳目標 vault">
              <option value="">— 選擇 vault —</option>
              {vaults.items.map((v) => (
                <option key={v.key} value={v.key}>
                  {v.display}
                </option>
              ))}
            </select>
          </label>
        </div>
        <input
          ref={fileInput}
          type="file"
          multiple
          accept={ACCEPT}
          class="lv-visually-hidden"
          aria-label="選擇要上傳的檔案"
          onChange={(e) => {
            const input = e.target as HTMLInputElement;
            const files = Array.from(input.files ?? []);
            input.value = '';
            void addFiles(files);
          }}
        />
        <button type="button" class="btn-outline btn-outline--sm" disabled={!target} onClick={() => fileInput.current?.click()}>
          選擇檔案
        </button>
      </div>

      {uploads.length > 0 && (
        <div class="lv-uploads" aria-live="polite" data-testid="upload-results">
          <div class="lv-uploads__head">
            <span>本次上傳</span>
            <button type="button" class="lv-link-btn" onClick={() => setUploads((u) => u.filter((x) => x.state === 'uploading' || x.state === 'queued'))}>
              清除已完成
            </button>
          </div>
          <ul>
            {uploads.map((u) => (
              <li key={u.key} class={`lv-upload lv-upload--${u.state}`}>
                <div class="lv-upload__row">
                  <span class="lv-upload__name">{u.name}</span>
                  <span class="lv-mono lv-muted">{formatBytes(u.size)}</span>
                </div>
                {u.state === 'uploading' && (
                  <div class="lv-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(u.progress * 100)} aria-label={`${u.name} 上傳進度`}>
                    <div class="lv-progress__bar" style={{ width: `${Math.round(u.progress * 100)}%` }} />
                  </div>
                )}
                <div class="lv-upload__msg" role={u.state === 'error' || u.state === 'rejected' ? 'alert' : undefined}>
                  {u.message}
                  {u.result && (
                    <>
                      {' '}
                      <button
                        type="button"
                        class="lv-link-btn"
                        onClick={() => navigate(routePath('docs', [u.result!.document_id]))}
                      >
                        開啟
                      </button>
                    </>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {lastDeleted && (
        <Banner
          tone="info"
          label="DELETED"
          testId="doc-deleted"
          action={
            <button type="button" class="btn-outline btn-outline--sm" disabled={undeleting} onClick={() => void undelete(lastDeleted)}>
              {undeleting ? '復原中…' : '復原'}
            </button>
          }
        >
          已刪除「{lastDeleted.filename}」v{lastDeleted.version}（留有墓碑；原始檔仍在時可復原，復原後會重新抽取）。
        </Banner>
      )}

      {page && page.unsupported_kinds.length > 0 && (
        <Banner tone="warn" label="PARTIAL" testId="docs-unsupported">
          這個模式下列不出文件（{page.unsupported_kinds.join('、')}）。
        </Banner>
      )}
      {error !== null && <ErrorState error={error} onRetry={() => setTick((t) => t + 1)} />}
      {loading && !page && <Loading />}

      {page && (
        <>
          <div class="lv-table lv-table--docs" role="table" aria-label="文件列表">
            <div class="lv-table__head" role="row">
              <span role="columnheader">檔名</span>
              <span role="columnheader">類型</span>
              <span role="columnheader">大小</span>
              <span role="columnheader">抽取狀態</span>
              <span role="columnheader" class="is-right">
                版本
              </span>
              <span role="columnheader">
                <span class="lv-visually-hidden">操作</span>
              </span>
            </div>
            {items.map((d) => {
              const st = documentStatus(d);
              const replacedBy = d.superseded_by ? byId.get(d.superseded_by) : undefined;
              return (
                <div key={d.id} role="row" class={'lv-table__row lv-doc-row' + (d.superseded_by ? ' is-superseded' : '')} data-status={d.status}>
                  <span role="cell" class="lv-table__main">
                    <a
                      class="lv-table__title"
                      href={routePath('docs', [d.id])}
                      onClick={(e) => {
                        e.preventDefault();
                        navigate(routePath('docs', [d.id]));
                      }}
                    >
                      {d.filename}
                    </a>
                    {vault === ALL && <span class="lv-table__sub">{vaultName({ vaults }, d.vault)}</span>}
                  </span>
                  <span role="cell" class="lv-mono lv-muted lv-upper">
                    {fileExt(d.filename) || '—'}
                  </span>
                  <span role="cell" class="lv-mono lv-muted">
                    {formatBytes(d.size_bytes)}
                  </span>
                  <span role="cell" class="lv-status-cell">
                    <span class={`lv-status lv-status--${st.tone}`}>
                      <span class="lv-status__dot" aria-hidden="true" />
                      {st.label}
                      {d.status === 'ready' && typeof d.chunk_count === 'number' && ` · ${d.chunk_count} 段`}
                      {d.encoding && d.status === 'ready' && ` · ${d.encoding}`}
                    </span>
                    {d.status === 'failed' && (
                      <span class="lv-status__detail" data-testid="doc-error">
                        {documentErrorText(d.error_code)}
                        {d.error_code && <code class="lv-code-tag">{d.error_code}</code>}
                        {d.error_detail && <span class="lv-status__raw">{d.error_detail}</span>}
                      </span>
                    )}
                    {d.warnings.map((w, i) => (
                      <span key={i} class="lv-status__warn" data-testid="doc-warning">
                        ⚠ {DOCUMENT_WARNING_TEXT[w.code] ?? w.code}：{w.detail}
                      </span>
                    ))}
                    {d.superseded_by && (
                      <span class="lv-status__superseded" data-testid="doc-superseded">
                        已被取代{replacedBy ? `（由 v${replacedBy.version}）` : ''}，已退出檢索索引
                      </span>
                    )}
                  </span>
                  <span role="cell" class="lv-mono lv-muted is-right">
                    v{d.version}
                    {d.supersedes && <span class="lv-small"> · 新版</span>}
                    <br />
                    <span class="lv-small">{formatTime(d.updated)}</span>
                  </span>
                  <span role="cell" class="lv-row-actions">
                    {d.status === 'failed' && (
                      <button type="button" class="lv-pill-btn" disabled={retrying === d.id} onClick={() => void retry(d)}>
                        {retrying === d.id ? '重試中…' : '重試'}
                      </button>
                    )}
                    <button type="button" class="lv-pill-btn lv-pill-btn--danger" aria-label={`刪除 ${d.filename}`} onClick={() => setDeleting(d)}>
                      刪除
                    </button>
                  </span>
                </div>
              );
            })}
          </div>
          {items.length === 0 && !loading && (
            <EmptyState title="這裡還沒有文件" testId="docs-empty">
              把檔案拖到上方區塊，或按「選擇檔案」上傳。
            </EmptyState>
          )}
          {processing && (
            <p class="lv-hint lv-hint--inline" role="status">
              有文件處理中，自動更新列表。
            </p>
          )}
          {(page.total ?? items.length) > 0 && (
            <Pager
              page={pageNo}
              pageSize={pageSize}
              total={page.total ?? items.length}
              loading={loading}
              unit="份"
              label="文件分頁"
              onPage={setPageNo}
              onPageSize={setPageSize}
            />
          )}
        </>
      )}

      {deleting && (
        <TwoPhaseDelete
          title="刪除文件"
          path="/v1/document_delete"
          args={{ space: space.id, vault: deleting.vault, id: deleting.id }}
          describe={<>刪除「{deleting.filename}」v{deleting.version}。會留下墓碑，可於維護頁復原（原始檔尚在時）。</>}
          onCancel={() => setDeleting(null)}
          onDone={() => {
            toast(`已刪除「${deleting.filename}」`, 'success');
            setLastDeleted(deleting);
            setDeleting(null);
            refreshVaults();
            setTick((t) => t + 1);
          }}
        />
      )}
    </section>
  );
}
