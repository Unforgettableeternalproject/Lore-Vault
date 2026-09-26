import { describe, expect, it, vi } from 'vitest';

import { ApiError, createApiClient, extractNotices, UI_HEADER } from './api';
import { checkSession, describeLoginError, LOCKED_MESSAGE, login, loginState } from './session';

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function setup(handler: Handler) {
  const calls: { url: string; init: RequestInit }[] = [];
  const onUnauthorized = vi.fn();
  const onNotices = vi.fn();
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init: init ?? {} });
    return handler(url, init ?? {});
  }) as typeof fetch;
  const api = createApiClient({ fetch: fetchImpl, onUnauthorized, onNotices });
  return { api, calls, onUnauthorized, onNotices };
}

async function caught(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (err) {
    expect(err).toBeInstanceOf(ApiError);
    return err as ApiError;
  }
  throw new Error('預期丟出 ApiError，卻成功了');
}

describe('請求格式', () => {
  it('帶 CSRF 標頭、same-origin cookie 與 JSON body', async () => {
    const { api, calls } = setup(() => json(200, { ok: true }));
    await api.post('/v1/recall', { query: 'q', vault: '*', space: 'dev' });
    const { url, init } = calls[0]!;
    expect(url).toBe('/v1/recall');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('same-origin');
    const headers = init.headers as Record<string, string>;
    expect(headers[UI_HEADER]).toBe('1');
    expect(headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(init.body as string)).toEqual({ query: 'q', vault: '*', space: 'dev' });
  });

  it('GET 不帶 body', async () => {
    const { api, calls } = setup(() => json(200, {}));
    await api.get('/ui/api/session');
    expect(calls[0]!.init.method).toBe('GET');
    expect(calls[0]!.init.body).toBeUndefined();
  });

  it('204 回 data = null', async () => {
    const { api } = setup(() => new Response(null, { status: 204 }));
    const result = await api.post('/ui/api/logout');
    expect(result.status).toBe(204);
    expect(result.data).toBeNull();
    expect(result.notices).toEqual([]);
  });
});

describe('錯誤處理', () => {
  it('服務端錯誤格式轉成 ApiError（保留 code、message、原始 body）', async () => {
    const body = { error: { code: 'unknown_vault', message: '找不到 vault', vault: 'x' } };
    const { api } = setup(() => json(404, body));
    const err = await caught(api.post('/v1/get', { vault: 'x', ids: [] }));
    expect(err.status).toBe(404);
    expect(err.code).toBe('unknown_vault');
    expect(err.message).toBe('找不到 vault');
    expect(err.body).toEqual(body);
  });

  it('409 衝突保留 body（之後的衝突畫面要用 current）', async () => {
    const body = { error: { code: 'conflict', message: '版本衝突', current: { updated: 't2' } } };
    const { api } = setup(() => json(409, body));
    const err = await caught(api.post('/v1/update', {}));
    expect(err.code).toBe('conflict');
    expect((err.body as typeof body).error.current.updated).toBe('t2');
  });

  it('非 JSON 的錯誤頁（代理層）不被吞掉', async () => {
    const { api } = setup(
      () => new Response('<html>502 Bad Gateway</html>', { status: 502, headers: { 'Content-Type': 'text/html' } }),
    );
    const err = await caught(api.post('/v1/status'));
    expect(err.status).toBe(502);
    expect(err.code).toBe('http_502');
    expect(err.body).toContain('502 Bad Gateway');
  });

  it('成功狀態卻不是 JSON 視為錯誤', async () => {
    const { api } = setup(() => new Response('<html>login</html>', { status: 200, headers: { 'Content-Type': 'text/html' } }));
    const err = await caught(api.post('/v1/status'));
    expect(err.code).toBe('bad_response');
  });

  it('網路失敗轉成 network_error', async () => {
    const { api } = setup(() => {
      throw new TypeError('Failed to fetch');
    });
    const err = await caught(api.post('/v1/status'));
    expect(err.status).toBe(0);
    expect(err.code).toBe('network_error');
  });

  it('429 帶 Retry-After 秒數', async () => {
    const { api } = setup(() =>
      json(429, { error: { code: 'too_many_attempts', message: 'x' } }, { 'Retry-After': '60' }),
    );
    const err = await caught(api.post('/v1/status'));
    expect(err.retryAfter).toBe(60);
  });

  it('403 csrf_required 照常丟出、不當成未登入', async () => {
    const { api, onUnauthorized } = setup(() => json(403, { error: { code: 'csrf_required', message: 'x' } }));
    const err = await caught(api.post('/v1/status'));
    expect(err.code).toBe('csrf_required');
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});

describe('401 流程', () => {
  it('資料端點回 401：通知 onUnauthorized 並丟出', async () => {
    const { api, onUnauthorized } = setup(() => json(401, { error: { code: 'unauthorized', message: 'x' } }));
    const err = await caught(api.post('/v1/recall', {}));
    expect(err.status).toBe(401);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('登入端點的 401（帳密錯）不觸發 onUnauthorized，並顯示剩餘次數', async () => {
    const { api, calls, onUnauthorized } = setup(() =>
      json(401, { error: { code: 'invalid_credentials', message: 'x', remaining: 2, max_failures: 3, locked: false } }),
    );
    const err = await caught(login(api, 'UEPBernie', 'wrong'));
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({ username: 'UEPBernie', password: 'wrong' });
    expect(err.code).toBe('invalid_credentials');
    expect(describeLoginError(err)).toBe('帳號或密碼錯誤，剩餘 2 次；失敗 3 次將鎖定，需人工解鎖');
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('鎖定（423）與無帳號（409）的文案', async () => {
    const locked = setup(() =>
      json(423, { error: { code: 'locked', message: 'x', remaining: 0, max_failures: 3, locked: true } }),
    );
    const err = await caught(login(locked.api, 'UEPBernie', 'right-password'));
    expect(describeLoginError(err)).toBe(LOCKED_MESSAGE);
    expect(LOCKED_MESSAGE).toContain('人工解鎖');

    const none = setup(() => json(409, { error: { code: 'no_account', message: 'x', remaining: 3 } }));
    const noAccount = await caught(login(none.api, 'a', 'b'));
    expect(describeLoginError(noAccount)).toContain('尚未設定帳號');
  });

  it('loginState 讀取登入頁的公開狀態', async () => {
    const { api, calls } = setup(() =>
      json(200, { account_configured: false, locked: false, remaining: 3, max_failures: 3, setup_command: 'cmd' }),
    );
    await expect(loginState(api)).resolves.toMatchObject({ account_configured: false, setup_command: 'cmd' });
    expect(calls[0]!.url).toBe('/ui/api/login');
    expect(calls[0]!.init.method).toBe('GET');
  });

  it('checkSession：401 回 null，其他錯誤照常丟出', async () => {
    const anon = setup(() => json(401, { error: { code: 'unauthorized', message: 'x' } }));
    await expect(checkSession(anon.api)).resolves.toBeNull();

    const down = setup(() => json(500, { error: { code: 'internal', message: 'x' } }));
    await expect(checkSession(down.api)).rejects.toBeInstanceOf(ApiError);

    const ok = setup(() => json(200, { authenticated: true, expires_at: 'a', idle_expires_at: 'b' }));
    await expect(checkSession(ok.api)).resolves.toMatchObject({ authenticated: true });
  });
});

describe('狀態標記不可被吞掉', () => {
  it('降級：notices 標出並保留原始欄位，且廣播給 onNotices', async () => {
    const body = {
      items: [{ id: 'n1', title: 't' }],
      degraded: true,
      degraded_reason: 'embedding_unavailable',
      degraded_detail: 'timeout',
      truncated: false,
    };
    const { api, onNotices } = setup(() => json(200, body));
    const result = await api.post('/v1/recall', {});
    expect(result.data).toEqual(body);
    expect(result.notices).toEqual([
      { kind: 'degraded', path: '$', detail: { reason: 'embedding_unavailable', detail: 'timeout' } },
    ]);
    expect(onNotices).toHaveBeenCalledWith(result.notices, '/v1/recall');
  });

  it('截斷與 omitted 一起標出', async () => {
    const { api } = setup(() => json(200, { items: [], truncated: true, omitted: 7 }));
    const { notices } = await api.post('/v1/recall', {});
    expect(notices.map((n) => n.kind)).toEqual(['truncated', 'omitted']);
    expect(notices[1]!.detail).toBe(7);
  });

  it('巢狀項目的截斷（get 單則 truncated）也抓得到', () => {
    const notices = extractNotices({
      items: [{ id: 'a', truncated: false }, { id: 'b', truncated: true, body_chars: 900 }],
      missing: ['c'],
      unavailable: [],
      unsupported_kinds: ['chunk'],
    });
    expect(notices).toEqual([
      { kind: 'unsupported_kinds', path: '$', detail: ['chunk'] },
      { kind: 'missing', path: '$', detail: ['c'] },
      { kind: 'truncated', path: '$.items[1]' },
    ]);
  });

  it('正常回應 notices 為空，但仍通知 onNotices（讓降級徽章能清除）', async () => {
    const { api, onNotices } = setup(() => json(200, { items: [], degraded: false }));
    const { notices } = await api.post('/v1/recall', {});
    expect(notices).toEqual([]);
    expect(onNotices).toHaveBeenCalledWith([], '/v1/recall');
  });
});

describe('上傳（XHR）', () => {
  class FakeXhr {
    static last: FakeXhr | null = null;
    headers: Record<string, string> = {};
    withCredentials = false;
    status = 0;
    responseText = '';
    upload: { onprogress: ((e: ProgressEvent) => void) | null } = { onprogress: null };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    method = '';
    url = '';
    sent: unknown = null;
    private responseHeaders: Record<string, string> = {};
    constructor() {
      FakeXhr.last = this;
    }
    open(method: string, url: string) {
      this.method = method;
      this.url = url;
    }
    setRequestHeader(k: string, v: string) {
      this.headers[k] = v;
    }
    getResponseHeader(k: string) {
      return this.responseHeaders[k.toLowerCase()] ?? null;
    }
    abort() {
      this.onabort?.();
    }
    send(body: unknown) {
      this.sent = body;
    }
    respond(status: number, body: unknown, headers: Record<string, string> = { 'content-type': 'application/json' }) {
      this.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 } as ProgressEvent);
      this.status = status;
      this.responseText = JSON.stringify(body);
      this.responseHeaders = headers;
      this.onload?.();
    }
  }

  function setupXhr() {
    const onUnauthorized = vi.fn();
    const api = createApiClient({ xhr: () => new FakeXhr() as unknown as XMLHttpRequest, onUnauthorized });
    return { api, onUnauthorized };
  }

  it('帶 CSRF 標頭與 cookie、回報進度、成功回 data', async () => {
    const { api } = setupXhr();
    const progress: number[] = [];
    const form = new FormData();
    const pending = api.upload('/v1/documents', form, { onProgress: (f) => progress.push(f) });
    const xhr = FakeXhr.last!;
    expect(xhr.headers[UI_HEADER]).toBe('1');
    expect(xhr.withCredentials).toBe(true);
    expect(xhr.sent).toBe(form);
    xhr.respond(201, { document_id: 'doc:1', duplicate: false });
    const result = await pending;
    expect(result.status).toBe(201);
    expect(result.data).toEqual({ document_id: 'doc:1', duplicate: false });
    expect(progress).toEqual([0.5]);
  });

  it('413 too_large 轉成 ApiError；401 通知 onUnauthorized', async () => {
    const { api, onUnauthorized } = setupXhr();
    const big = api.upload('/v1/documents', new FormData());
    FakeXhr.last!.respond(413, { error: { code: 'too_large', message: '太大' } });
    const err = await caught(big);
    expect(err.status).toBe(413);
    expect(err.code).toBe('too_large');

    const anon = api.upload('/v1/documents', new FormData());
    FakeXhr.last!.respond(401, { error: { code: 'unauthorized', message: 'x' } });
    await caught(anon);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
