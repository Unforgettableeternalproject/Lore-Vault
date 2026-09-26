import { describe, expect, it, vi } from 'vitest';

import { ApiError, createApiClient, extractNotices, UI_HEADER } from './api';
import { checkSession, describeLoginError, login } from './session';

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
    const err = await caught(api.post('/ui/api/login', { key: 'k' }));
    expect(err.retryAfter).toBe(60);
    expect(describeLoginError(err)).toBe('嘗試次數過多，請於 60 秒後再試');
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

  it('登入端點的 401（金鑰錯）不觸發 onUnauthorized', async () => {
    const { api, onUnauthorized } = setup(() =>
      json(401, { error: { code: 'invalid_credentials', message: '存取金鑰不正確' } }),
    );
    const err = await caught(login(api, 'wrong'));
    expect(err.code).toBe('invalid_credentials');
    expect(describeLoginError(err)).toBe('存取金鑰不正確');
    expect(onUnauthorized).not.toHaveBeenCalled();
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
