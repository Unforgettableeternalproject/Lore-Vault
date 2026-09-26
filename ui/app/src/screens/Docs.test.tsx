/** @vitest-environment happy-dom */
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiResponse } from '../lib/api';
import { ApiError } from '../lib/api';
import type { ChunkFull, DocumentMeta, UploadResult } from '../lib/types';
import { apiError, json, makeApi, renderWithApp, type Handler } from '../test/harness';
import { DocDetail } from './DocDetail';
import { Docs, MAX_UPLOAD_BYTES } from './Docs';

afterEach(cleanup);

const VAULT = 'github.com/org/lore-vault';

function doc(overrides: Partial<DocumentMeta> = {}): DocumentMeta {
  return {
    id: 'doc:u1',
    kind: 'document',
    vault: VAULT,
    title: 'a.md',
    filename: 'a.md',
    mime: 'text/markdown',
    size_bytes: 2048,
    status: 'ready',
    error_code: null,
    error_detail: null,
    version: 1,
    supersedes: null,
    superseded_by: null,
    chunk_count: 3,
    encoding: 'utf-8',
    warnings: [],
    created: '2026-09-26T00:00:00.000Z',
    updated: '2026-09-26T00:00:00.000Z',
    ...overrides,
  };
}

function listOf(items: DocumentMeta[]): Handler {
  return () => json({ items, next_cursor: null, has_more: false, unsupported_kinds: [] });
}

describe('文件列表狀態', () => {
  it('失敗顯示錯誤碼文案與細節並可重試；警示、已被取代、處理中都看得到', async () => {
    const { api, callsTo } = makeApi({
      '/v1/list': listOf([
        doc({ id: 'doc:f', filename: 'scan.pdf', status: 'failed', error_code: 'empty_extraction', error_detail: '只有 3 個字', chunk_count: null, encoding: null }),
        doc({ id: 'doc:w', filename: 'big5.txt', encoding: 'cp950', warnings: [{ code: 'encoding_low_confidence', detail: 'cp950（Big5）判定信心低' }] }),
        doc({ id: 'doc:old', filename: 'deck.pptx', version: 1, superseded_by: 'doc:new' }),
        doc({ id: 'doc:new', filename: 'deck.pptx', version: 2, supersedes: 'doc:old', status: 'extracting', chunk_count: null }),
      ]),
      '/v1/document_retry': () => json({ document: {}, space: 'dev', manual_retries: 1, max_manual_retries: 3 }),
    });
    const { toast } = renderWithApp(<Docs />, api);
    const error = await screen.findByTestId('doc-error');
    expect(error.textContent).toContain('沒有抽出文字');
    expect(error.textContent).toContain('empty_extraction');
    expect(error.textContent).toContain('只有 3 個字');
    expect(screen.getByTestId('doc-warning').textContent).toContain('編碼判定信心低');
    expect(screen.getByTestId('doc-superseded').textContent).toContain('v2');
    expect(screen.getByText('抽取中')).toBeTruthy();
    expect(screen.getByText(/有文件處理中，自動更新/)).toBeTruthy();

    const failedRow = error.closest('[role="row"]') as HTMLElement;
    fireEvent.click(within(failedRow).getByRole('button', { name: '重試' }));
    await waitFor(() => expect(callsTo('/v1/document_retry')).toHaveLength(1));
    expect(callsTo('/v1/document_retry')[0]!.body).toEqual({ space: 'dev', vault: VAULT, id: 'doc:f' });
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringContaining('1/3'), 'success'));
  });

  it('重試達上限（409 retry_limit）明確告知', async () => {
    const { api } = makeApi({
      '/v1/list': listOf([doc({ status: 'failed', error_code: 'corrupt', chunk_count: null })]),
      '/v1/document_retry': () => apiError(409, 'retry_limit'),
    });
    const { toast } = renderWithApp(<Docs />, api);
    fireEvent.click(await screen.findByRole('button', { name: '重試' }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringContaining('retry_limit'), 'error'));
    expect(toast.mock.calls[0]![0]).toContain('人工重試上限');
  });
});

describe('上傳', () => {
  function file(name: string, size: number): File {
    const f = new File(['x'], name, { type: 'text/markdown' });
    Object.defineProperty(f, 'size', { value: size });
    return f;
  }

  it('先擋大小上限；逐檔顯示新版本／重複／重試／錯誤結果', async () => {
    const results: Record<string, UploadResult | ApiError> = {
      'new.md': { document_id: 'doc:a', status: 'pending', sha256: 's', duplicate: false, retried: false, vault: VAULT, space: 'dev', filename: 'new.md', version: 2, supersedes: 'doc:old', size_bytes: 10 },
      'same.md': { document_id: 'doc:b', status: 'ready', sha256: 's', duplicate: true, retried: false, vault: VAULT, space: 'dev', filename: 'same.md', version: 1, supersedes: null, size_bytes: 10 },
      'again.md': { document_id: 'doc:c', status: 'pending', sha256: 's', duplicate: false, retried: true, vault: VAULT, space: 'dev', filename: 'again.md', version: 1, supersedes: null, size_bytes: 10 },
      'bad.exe': new ApiError(400, 'unsupported_format', 'x'),
    };
    const uploaded: string[] = [];
    const upload = vi.fn(async (_path: string, form: FormData, options?: { onProgress?: (f: number) => void }) => {
      const f = form.get('file') as File;
      uploaded.push(f.name);
      expect(form.get('vault')).toBe(VAULT);
      expect(form.get('space')).toBe('dev');
      options?.onProgress?.(0.5);
      const r = results[f.name]!;
      if (r instanceof ApiError) throw r;
      return { status: r.duplicate ? 200 : 201, data: r, notices: [] } as ApiResponse<unknown>;
    });
    const { api } = makeApi({ '/v1/list': listOf([]) });
    const { refreshVaults } = renderWithApp(<Docs />, api, { vault: VAULT, upload });
    const zone = await screen.findByTestId('dropzone');
    fireEvent.drop(zone, {
      dataTransfer: { files: [file('huge.pdf', MAX_UPLOAD_BYTES + 1), file('new.md', 10), file('same.md', 10), file('again.md', 10), file('bad.exe', 10)] },
    });
    const results$ = await screen.findByTestId('upload-results');
    await waitFor(() => expect(uploaded).toEqual(['new.md', 'same.md', 'again.md', 'bad.exe']));
    await waitFor(() => expect(results$.textContent).toContain('不支援的檔案格式'));
    expect(results$.textContent).toContain('超過 25.0 MB 上限，未上傳');
    expect(results$.textContent).toContain('新版本 v2');
    expect(results$.textContent).toContain('內容與既有文件相同');
    expect(results$.textContent).toContain('沿用先前失敗的同內容文件');
    expect(refreshVaults).toHaveBeenCalled();
  });

  it('沒選 vault 時不上傳', async () => {
    const upload = vi.fn();
    const { api } = makeApi({ '/v1/list': listOf([]) });
    const { toast } = renderWithApp(<Docs />, api, { upload });
    fireEvent.drop(await screen.findByTestId('dropzone'), { dataTransfer: { files: [file('a.md', 1)] } });
    expect(upload).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('vault'), 'warning');
  });
});

describe('文件檢視', () => {
  function chunk(idx: number, locator: ChunkFull['locator'], text: string): ChunkFull {
    return { id: `chunk:u1:${idx}`, kind: 'chunk', document_id: 'doc:u1', vault: VAULT, title: 'a.md', locator, text, text_chars: text.length, truncated: false, superseded_by: null, updated: 'x' };
  }

  it('依 locator 列段落；從檢索跳入時標示該段並顯示 FROM RECALL', async () => {
    const { api, callsTo } = makeApi({
      '/v1/get': (body) => {
        const ids = body.ids as string[];
        if (ids[0] === 'doc:u1') {
          return json({ items: [{ ...doc(), text: 'x', text_chars: 99, truncated: true }], missing: [], unavailable: [], truncated: true, budget: 1, used_chars: 1 });
        }
        return json({
          items: [chunk(0, { kind: 'heading', value: '前言' }, '甲'), chunk(1, { kind: 'slide', value: 6 }, '乙'), chunk(2, { kind: 'page', value: 3, part: 2 }, '丙')],
          missing: [],
          unavailable: [],
          truncated: false,
          budget: 400000,
          used_chars: 3,
        });
      },
    });
    renderWithApp(<DocDetail id="doc:u1" chunk={1} fromQuery="hook 注入" />, api);
    const active = await screen.findByTestId('chunk-active');
    expect(active.textContent).toContain('投影片 6');
    expect(active.textContent).toContain('乙');
    expect(screen.getByTestId('from-recall').textContent).toContain('hook 注入');
    expect(screen.getByText('第 3 頁 · 第 2 段')).toBeTruthy();
    expect(callsTo('/v1/get')[1]!.body.ids).toEqual(['chunk:u1:0', 'chunk:u1:1', 'chunk:u1:2']);
  });

  it('失敗文件：顯示原因與重試，不去取段落', async () => {
    const { api, callsTo } = makeApi({
      '/v1/get': () =>
        json({ items: [{ ...doc({ status: 'failed', error_code: 'encrypted', error_detail: 'PDF 有密碼', chunk_count: null }), text: '', text_chars: 0, truncated: false }], missing: [], unavailable: [], truncated: false, budget: 1, used_chars: 0 }),
    });
    renderWithApp(<DocDetail id="doc:u1" chunk={null} fromQuery={null} />, api);
    const failed = await screen.findByTestId('doc-failed');
    expect(failed.textContent).toContain('已加密');
    expect(failed.textContent).toContain('PDF 有密碼');
    expect(within(failed).getByRole('button', { name: '重試抽取' })).toBeTruthy();
    expect(callsTo('/v1/get')).toHaveLength(1);
  });

  it('降級時文件讀不到（unavailable）要說明，不是「不存在」', async () => {
    const { api } = makeApi({
      '/v1/get': () => json({ items: [], missing: [], unavailable: ['doc:u1'], truncated: false, budget: 1, used_chars: 0 }),
    });
    renderWithApp(<DocDetail id="doc:u1" chunk={null} fromQuery={null} />, api);
    expect((await screen.findByTestId('doc-missing')).textContent).toContain('unavailable');
  });
});
