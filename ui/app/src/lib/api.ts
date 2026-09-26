// Lore Vault API client：同源、session cookie 認證，統一錯誤處理。
//
// 原則（專案頭號原則「不讓錯誤靜默發生」）：
// - 非 2xx 一律丟 ApiError（帶 status、服務端錯誤碼、原始 body），不回傳半成品
// - 401 通知 onUnauthorized（App 據此回到登入頁）；登入端點本身的 401 不算
// - 回應裡的降級／截斷／缺漏標記抽成 notices 附在結果上並廣播，原始資料不刪改；
//   畫面卡要自己呈現 notices，不能只拿 data

export const UI_HEADER = 'X-Lore-Vault-UI';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly body: unknown;
  readonly retryAfter: number | null;

  constructor(
    status: number,
    code: string,
    message: string,
    body: unknown = null,
    retryAfter: number | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.body = body;
    this.retryAfter = retryAfter;
  }
}

export type NoticeKind =
  | 'degraded'
  | 'truncated'
  | 'omitted'
  | 'unsupported_kinds'
  | 'missing'
  | 'unavailable';

export interface Notice {
  kind: NoticeKind;
  /** 在回應 JSON 中的位置，例如 `$`、`$.items[3]` */
  path: string;
  /** 附帶資訊：degraded 的 reason／detail、omitted 的筆數、清單內容等 */
  detail?: unknown;
}

export interface ApiResponse<T> {
  status: number;
  data: T;
  notices: Notice[];
}

export interface ApiClientOptions {
  fetch?: typeof fetch;
  /** 任何非登入端點回 401 時呼叫（session 過期、被登出） */
  onUnauthorized?: () => void;
  /** 每個成功回應都呼叫（notices 可能是空陣列，讓全域狀態能在恢復時清除） */
  onNotices?: (notices: Notice[], path: string) => void;
  /** 測試可注入 XHR（上傳用） */
  xhr?: ApiClientXhrFactory;
}

const LOGIN_PATH = '/ui/api/login';
// 走訪深度上限：回應是 items 陣列＋少量巢狀，6 層足夠，也避免異常深的物件拖慢
const MAX_DEPTH = 6;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** 從回應 JSON 找出所有需要呈現給使用者的狀態標記（不修改原資料）。 */
export function extractNotices(data: unknown): Notice[] {
  const notices: Notice[] = [];
  const walk = (node: unknown, path: string, depth: number): void => {
    if (depth > MAX_DEPTH) return;
    if (Array.isArray(node)) {
      node.forEach((item, i) => walk(item, `${path}[${i}]`, depth + 1));
      return;
    }
    if (!isRecord(node)) return;
    if (node.degraded === true) {
      notices.push({
        kind: 'degraded',
        path,
        detail: { reason: node.degraded_reason ?? null, detail: node.degraded_detail ?? null },
      });
    }
    if (node.truncated === true) notices.push({ kind: 'truncated', path });
    if (typeof node.omitted === 'number' && node.omitted > 0) {
      notices.push({ kind: 'omitted', path, detail: node.omitted });
    }
    for (const kind of ['unsupported_kinds', 'missing', 'unavailable'] as const) {
      const value = node[kind];
      if (Array.isArray(value) && value.length > 0) notices.push({ kind, path, detail: value });
    }
    for (const [key, value] of Object.entries(node)) {
      if (typeof value === 'object' && value !== null) walk(value, `${path}.${key}`, depth + 1);
    }
  };
  walk(data, '$', 0);
  return notices;
}

function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

async function readBody(resp: Response): Promise<unknown> {
  if (resp.status === 204) return null;
  const text = await resp.text();
  if (!text) return null;
  const type = resp.headers.get('content-type') ?? '';
  if (!type.includes('json')) {
    throw new ApiError(resp.status, 'bad_response', `服務回傳非 JSON 內容（HTTP ${resp.status}）`, text);
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ApiError(resp.status, 'bad_response', `服務回傳無法解析的 JSON（HTTP ${resp.status}）`, text);
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST';
  body?: unknown;
  signal?: AbortSignal;
}

export interface UploadOptions {
  /** 上傳進度（0–1）；瀏覽器無法計算總量時不呼叫 */
  onProgress?: (fraction: number) => void;
  signal?: AbortSignal;
}

export interface ApiClient {
  request<T>(path: string, options?: RequestOptions): Promise<ApiResponse<T>>;
  get<T>(path: string, signal?: AbortSignal): Promise<ApiResponse<T>>;
  post<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<ApiResponse<T>>;
  /** multipart 上傳（fetch 沒有上傳進度，改用 XHR）；錯誤與成功處理同 request */
  upload<T>(path: string, form: FormData, options?: UploadOptions): Promise<ApiResponse<T>>;
}

export interface ApiClientXhrFactory {
  (): XMLHttpRequest;
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const doFetch = options.fetch ?? ((input, init) => fetch(input, init));

  async function request<T>(path: string, opts: RequestOptions = {}): Promise<ApiResponse<T>> {
    const method = opts.method ?? (opts.body === undefined ? 'GET' : 'POST');
    const headers: Record<string, string> = { [UI_HEADER]: '1', Accept: 'application/json' };
    const init: RequestInit = { method, headers, credentials: 'same-origin', signal: opts.signal };
    if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }

    let resp: Response;
    try {
      resp = await doFetch(path, init);
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') throw err;
      throw new ApiError(0, 'network_error', '無法連線到 Lore Vault 服務', String(err));
    }

    if (resp.ok) {
      const data = (await readBody(resp)) as T;
      const notices = extractNotices(data);
      options.onNotices?.(notices, path);
      return { status: resp.status, data, notices };
    }

    let body: unknown;
    try {
      body = await readBody(resp);
    } catch (err) {
      // 錯誤回應本身不是 JSON（例如代理層的 HTML 錯誤頁）：保留原文供顯示
      body = err instanceof ApiError ? err.body : null;
    }
    throw failure(path, resp.status, body, resp.headers.get('retry-after'));
  }

  function failure(path: string, status: number, body: unknown, retryAfter: string | null): ApiError {
    const error = isRecord(body) && isRecord(body.error) ? body.error : null;
    const code = typeof error?.code === 'string' ? error.code : `http_${status}`;
    const message =
      typeof error?.message === 'string' ? error.message : `服務回應錯誤（HTTP ${status}）`;
    if (status === 401 && path !== LOGIN_PATH) options.onUnauthorized?.();
    return new ApiError(status, code, message, body, parseRetryAfter(retryAfter));
  }

  function upload<T>(path: string, form: FormData, opts: UploadOptions = {}): Promise<ApiResponse<T>> {
    const makeXhr = options.xhr ?? (() => new XMLHttpRequest());
    return new Promise((resolve, reject) => {
      const xhr = makeXhr();
      xhr.open('POST', path);
      xhr.withCredentials = true;
      xhr.setRequestHeader(UI_HEADER, '1');
      xhr.setRequestHeader('Accept', 'application/json');
      if (opts.onProgress) {
        const report = opts.onProgress;
        xhr.upload.onprogress = (e: ProgressEvent) => {
          if (e.lengthComputable && e.total > 0) report(e.loaded / e.total);
        };
      }
      const onAbort = () => xhr.abort();
      opts.signal?.addEventListener('abort', onAbort, { once: true });
      xhr.onerror = () =>
        reject(new ApiError(0, 'network_error', '無法連線到 Lore Vault 服務', 'upload failed'));
      xhr.onabort = () => reject(new DOMException('上傳已取消', 'AbortError'));
      xhr.onload = () => {
        opts.signal?.removeEventListener('abort', onAbort);
        const text = xhr.responseText;
        const type = xhr.getResponseHeader('content-type') ?? '';
        let body: unknown = null;
        let parsed = true;
        if (text) {
          if (type.includes('json')) {
            try {
              body = JSON.parse(text) as unknown;
            } catch {
              parsed = false;
              body = text;
            }
          } else {
            parsed = false;
            body = text;
          }
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          if (!parsed) {
            reject(new ApiError(xhr.status, 'bad_response', `服務回傳非 JSON 內容（HTTP ${xhr.status}）`, body));
            return;
          }
          const notices = extractNotices(body);
          options.onNotices?.(notices, path);
          resolve({ status: xhr.status, data: body as T, notices });
          return;
        }
        reject(failure(path, xhr.status, body, xhr.getResponseHeader('retry-after')));
      };
      xhr.send(form);
    });
  }

  return {
    request,
    get: (path, signal) => request(path, { method: 'GET', signal }),
    post: (path, body, signal) => request(path, { method: 'POST', body, signal }),
    upload,
  };
}
