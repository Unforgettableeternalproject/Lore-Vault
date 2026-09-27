// 文件檢視（T-81）：依 chunk 的 locator（頁／投影片／標題段）呈現抽出的文字，左側為段落導覽；
// 從檢索跳進來（?chunk=<idx>&q=）時捲到並標示該段。非 ready 的文件只顯示狀態、失敗原因與重試。
import { useEffect, useRef, useState } from 'preact/hooks';

import { Banner, EmptyState, ErrorState, Loading } from '../components/ui';
import { useApp } from '../lib/context';
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
  locatorLabel,
} from '../lib/format';
import { routePath } from '../lib/router';
import type { ChunkFull, DocumentFull, DocumentRetryResult, GetResult } from '../lib/types';
import { POLL_MS } from './Docs';

const CHUNK_BUDGET = 400_000;

/**
 * 段落顯示文字：前一段也在畫面上時，略過開頭與前一段重疊的 `overlap` 字（同服務端 chunk_excerpt 的規則：
 * 只在 0 < overlap < 長度時切）；段落起頭（overlap 0）或前一段沒載入時原樣顯示。
 */
export function chunkDisplayText(chunk: Pick<ChunkFull, 'text' | 'overlap'>, previousShown: boolean): string {
  const overlap = chunk.overlap ?? 0;
  if (!previousShown || !(overlap > 0 && overlap < chunk.text.length)) return chunk.text;
  return chunk.text.slice(overlap);
}

export function chunkRef(documentId: string, idx: number): string {
  return `chunk:${documentId.replace(/^doc:/, '')}:${idx}`;
}

interface ChunkState {
  items: ChunkFull[];
  missing: string[];
  unavailable: string[];
  truncated: boolean;
}

export function DocDetail({ id, chunk, fromQuery }: { id: string; chunk: number | null; fromQuery: string | null }) {
  const { api, space, navigate, toast, limits } = useApp();
  const [doc, setDoc] = useState<DocumentFull | null>(null);
  const [lookup, setLookup] = useState<{ missing: string[]; unavailable: string[] }>({ missing: [], unavailable: [] });
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [chunks, setChunks] = useState<ChunkState | null>(null);
  const [chunkError, setChunkError] = useState<unknown>(null);
  const [active, setActive] = useState<number | null>(chunk);
  const [retrying, setRetrying] = useState(false);
  const scrolled = useRef(false);

  // 文件 metadata：`fields: "meta"` 只回 metadata、不組全文、不佔預算（全文改由 chunk 呈現）
  useEffect(() => {
    const ctrl = new AbortController();
    setError(null);
    api
      .post<GetResult<DocumentFull>>('/v1/get', { space: space.id, vault: '*', ids: [id], fields: 'meta' }, ctrl.signal)
      .then(({ data }) => {
        setDoc(data.items.find((d) => d.id === id && d.kind === 'document') ?? null);
        setLookup({ missing: data.missing, unavailable: data.unavailable });
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, space.id, id, tick]);

  // 處理中：輪詢狀態
  useEffect(() => {
    if (!doc || !isProcessing(doc)) return;
    const timer = window.setTimeout(() => setTick((t) => t + 1), POLL_MS);
    return () => window.clearTimeout(timer);
  }, [doc]);

  const ready = doc?.status === 'ready';
  const count = doc?.chunk_count ?? 0;
  useEffect(() => {
    if (!doc || !ready || count <= 0) {
      setChunks(null);
      return;
    }
    const ctrl = new AbortController();
    setChunkError(null);
    const refs = Array.from({ length: count }, (_, i) => chunkRef(doc.id, i));
    const batches: string[][] = [];
    // 服務端 get 一次的 id 上限取 session limits
    const perBatch = limits.get_max_ids;
    for (let i = 0; i < refs.length; i += perBatch) batches.push(refs.slice(i, i + perBatch));
    Promise.all(
      batches.map((ids) =>
        api.post<GetResult<ChunkFull>>('/v1/get', { space: space.id, vault: doc.vault, ids, budget: CHUNK_BUDGET }, ctrl.signal),
      ),
    )
      .then((results) => {
        const state: ChunkState = { items: [], missing: [], unavailable: [], truncated: false };
        for (const { data } of results) {
          state.items.push(...data.items.filter((c) => c.kind === 'chunk'));
          state.missing.push(...data.missing);
          state.unavailable.push(...data.unavailable);
          state.truncated ||= data.truncated;
        }
        setChunks(state);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setChunkError(err);
      });
    return () => ctrl.abort();
  }, [api, space.id, doc?.id, doc?.updated, ready, count, limits.get_max_ids]);

  // 從檢索跳進來：捲到該段（只做一次）
  useEffect(() => {
    if (chunk === null || !chunks || scrolled.current) return;
    const el = document.getElementById(`chunk-${chunk}`);
    if (el) {
      scrolled.current = true;
      el.scrollIntoView?.({ block: 'start' });
      el.focus({ preventScroll: true });
    }
  }, [chunks, chunk]);

  const retry = async () => {
    if (!doc) return;
    setRetrying(true);
    try {
      const { data } = await api.post<DocumentRetryResult>('/v1/document_retry', { space: space.id, vault: doc.vault, id: doc.id });
      toast(`已重新排入抽取（人工重試 ${data.manual_retries}/${data.max_manual_retries}）`, 'success');
      setTick((t) => t + 1);
    } catch (err) {
      toast(`重試失敗：${describeError(err)}`, 'error');
    } finally {
      setRetrying(false);
    }
  };

  if (loading && !doc) return <Loading />;
  if (error !== null && !doc) return <ErrorState error={error} onRetry={() => setTick((t) => t + 1)} />;
  if (!doc) {
    return (
      <section class="lv-screen">
        <div class="lv-eyebrow">DOCUMENT · {space.en}</div>
        <h1 class="lv-title">找不到文件</h1>
        <div class="zone-state zone-state--error" role="alert" data-testid="doc-missing">
          {lookup.unavailable.includes(id)
            ? `這個模式下無法讀取文件 ${id}（unavailable：服務降級時不提供文件）。`
            : `在 ${space.en} space 找不到 ${id}：可能已被刪除，或屬於其他 space（missing）。`}
          <button type="button" onClick={() => navigate(routePath('docs'))}>
            回文件列表
          </button>
        </div>
      </section>
    );
  }

  const st = documentStatus(doc);
  const sorted = chunks ? [...chunks.items].sort((a, b) => chunkIdx(a.id) - chunkIdx(b.id)) : [];
  const shown = new Set(sorted.map((c) => chunkIdx(c.id)));

  return (
    <section class="lv-doc" aria-labelledby="lv-doc-title">
      <nav class="lv-crumb" aria-label="位置">
        <button type="button" class="lv-crumb__link" onClick={() => navigate(routePath('docs'))}>
          {space.en} / {doc.vault} / 文件
        </button>
        <span aria-hidden="true">/</span>
        <span class="lv-upper">
          {fileExt(doc.filename) || 'FILE'} · v{doc.version}
          {ready && typeof doc.chunk_count === 'number' ? ` · ${doc.chunk_count} 段` : ''}
        </span>
      </nav>
      <h1 id="lv-doc-title" class="lv-title">
        {doc.filename}
      </h1>
      <div class="lv-meta-line">
        <span class={`lv-status lv-status--${st.tone}`}>
          <span class="lv-status__dot" aria-hidden="true" />
          {st.label}
        </span>
        <span>{formatBytes(doc.size_bytes)}</span>
        {doc.encoding && <span>編碼 {doc.encoding}</span>}
        <span>更新 {formatTime(doc.updated)}</span>
      </div>

      {doc.superseded_by && (
        <Banner
          tone="warn"
          label="SUPERSEDED"
          testId="doc-superseded"
          action={
            <button type="button" class="btn-outline btn-outline--sm" onClick={() => navigate(routePath('docs', [doc.superseded_by!]))}>
              開啟新版
            </button>
          }
        >
          這是舊版本，已被新版取代並退出檢索索引。
        </Banner>
      )}
      {doc.warnings.map((w, i) => (
        <Banner key={i} tone="warn" label="WARNING" testId="doc-warning" title={DOCUMENT_WARNING_TEXT[w.code] ?? w.code}>
          {w.detail}
        </Banner>
      ))}
      {doc.status === 'failed' && (
        <Banner
          tone="error"
          label="FAILED"
          testId="doc-failed"
          title={documentErrorText(doc.error_code)}
          action={
            <button type="button" class="btn-outline btn-outline--sm" disabled={retrying} onClick={() => void retry()}>
              {retrying ? '重試中…' : '重試抽取'}
            </button>
          }
        >
          {doc.error_code && <code class="lv-code-tag">{doc.error_code}</code>} {doc.error_detail ?? ''}
        </Banner>
      )}
      {isProcessing(doc) && (
        <EmptyState title={`${st.label}…`}>抽取完成後會自動顯示段落。</EmptyState>
      )}
      {ready && count === 0 && <EmptyState title="這份文件沒有段落">抽取完成但沒有取出任何文字段落。</EmptyState>}

      {chunkError !== null && <ErrorState error={chunkError} onRetry={() => setTick((t) => t + 1)} />}
      {ready && count > 0 && !chunks && chunkError === null && <Loading label="載入段落…" />}

      {chunks && (
        <>
          {(chunks.missing.length > 0 || chunks.unavailable.length > 0) && (
            <Banner tone="warn" label="MISSING" testId="chunks-missing">
              有 {chunks.missing.length + chunks.unavailable.length} 個段落讀不到（
              {chunks.missing.length > 0 && `不存在 ${chunks.missing.length}`}
              {chunks.unavailable.length > 0 && ` 暫不可用 ${chunks.unavailable.length}`}）。
            </Banner>
          )}
          {chunks.truncated && (
            <Banner tone="warn" label="TRUNCATED" testId="chunks-truncated">
              段落內容超過字數預算，標示「已截斷」的段落沒有完整顯示。
            </Banner>
          )}
          <div class="lv-doc__grid">
            <nav class="lv-doc__toc" aria-label="段落導覽">
              {sorted.map((c) => {
                const idx = chunkIdx(c.id);
                return (
                  <a
                    key={c.id}
                    href={`#chunk-${idx}`}
                    class={'lv-doc__toc-item' + (active === idx ? ' is-on' : '')}
                    aria-current={active === idx ? 'location' : undefined}
                    onClick={(e) => {
                      e.preventDefault();
                      setActive(idx);
                      const el = document.getElementById(`chunk-${idx}`);
                      el?.scrollIntoView?.({ block: 'start' });
                      el?.focus({ preventScroll: true });
                    }}
                  >
                    <span class="lv-doc__toc-n">{idx + 1}</span>
                    <span>{locatorLabel(c.locator)}</span>
                  </a>
                );
              })}
            </nav>
            <div class="lv-doc__body">
              {chunk !== null && fromQuery && (
                <div class="lv-from-recall" data-testid="from-recall">
                  <span class="lv-from-recall__label">FROM RECALL</span>
                  <span>
                    從檢索「{fromQuery}」跳到第 {chunk + 1} 段
                    {sorted.some((c) => chunkIdx(c.id) === chunk) ? '' : '（這個段落已不存在，文件可能已重新抽取）'}
                  </span>
                </div>
              )}
              {sorted.map((c) => {
                const idx = chunkIdx(c.id);
                return (
                  <section
                    key={c.id}
                    id={`chunk-${idx}`}
                    tabIndex={-1}
                    class={'lv-chunk' + (active === idx ? ' is-on' : '')}
                    data-testid={active === idx ? 'chunk-active' : undefined}
                    aria-label={`第 ${idx + 1} 段 ${locatorLabel(c.locator)}`}
                  >
                    <div class="lv-chunk__loc">
                      {locatorLabel(c.locator)} · 第 {idx + 1} 段
                    </div>
                    <p class="lv-chunk__text">{chunkDisplayText(c, shown.has(idx - 1))}</p>
                    {c.truncated && (
                      <div class="lv-chunk__trunc">
                        已截斷：顯示 {c.text.length.toLocaleString()} / {c.text_chars.toLocaleString()} 字
                      </div>
                    )}
                  </section>
                );
              })}
            </div>
          </div>
        </>
      )}
    </section>
  );
}

function chunkIdx(chunkId: string): number {
  const n = Number(chunkId.split(':').pop());
  return Number.isFinite(n) ? n : 0;
}
