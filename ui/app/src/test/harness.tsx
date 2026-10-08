// 元件測試用：以假的 fetch 建真正的 API client（錯誤解析、notices 與正式相同），
// 並提供 AppContext。handler 依路徑回應；呼叫紀錄可用來斷言請求內容。
import { render } from '@testing-library/preact';
import type { ComponentChildren } from 'preact';
import { vi } from 'vitest';

import { createApiClient, type ApiClient, type ApiResponse, type UploadOptions } from '../lib/api';
import { AppContext, type AppEnv } from '../lib/context';
import { FALLBACK_LIMITS } from '../lib/limits';
import { SPACES, type SpaceId } from '../lib/spaces';
import type { VaultSummary } from '../lib/types';

export interface Call {
  path: string;
  body: Record<string, unknown>;
}

export type Reply = { status?: number; body: unknown };
export type Handler = (body: Record<string, unknown>, call: number) => Reply | Promise<Reply>;

export function json(body: unknown, status = 200): Reply {
  return { status, body };
}

export function apiError(status: number, code: string, extra: Record<string, unknown> = {}): Reply {
  return { status, body: { error: { code, message: `${code} message`, ...extra } } };
}

/** 元件測試的登入者（對應 session 的 principal／display_name） */
export const TEST_PRINCIPAL = 'UEPBernie';
export const TEST_AUTHOR = 'Xavier (Bernie)';

export const VAULTS: VaultSummary[] = [
  { key: 'github.com/org/lore-vault', display: 'Lore Vault', kind: 'repo', space: 'dev', aliases: [], note_count: 3, document_count: 1 },
];

export function makeApi(handlers: Record<string, Handler>) {
  const calls: Call[] = [];
  const counts: Record<string, number> = {};
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const body = init?.body ? (JSON.parse(init.body as string) as Record<string, unknown>) : {};
    calls.push({ path, body });
    const handler = handlers[path];
    if (!handler) throw new Error(`測試沒有處理 ${path}`);
    counts[path] = (counts[path] ?? 0) + 1;
    const reply = await handler(body, counts[path]!);
    return new Response(JSON.stringify(reply.body), {
      status: reply.status ?? 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof fetch;
  const api: ApiClient = createApiClient({ fetch: fetchImpl });
  return { api, calls, callsTo: (path: string) => calls.filter((c) => c.path === path) };
}

/** 元件測試的 session limits（刻意與服務預設不同，確認畫面讀的是 limits 而不是寫死的常數） */
export const TEST_LIMITS = { ...FALLBACK_LIMITS, max_file_bytes: 1024 * 1024, get_max_ids: 20 };

export interface EnvOverrides {
  vault?: string;
  vaults?: VaultSummary[];
  space?: SpaceId;
  upload?: (path: string, form: FormData, options?: UploadOptions) => Promise<ApiResponse<unknown>>;
}

export function renderWithApp(ui: ComponentChildren, api: ApiClient, overrides: EnvOverrides = {}) {
  const navigate = vi.fn();
  const toast = vi.fn();
  const refreshVaults = vi.fn();
  const setVault = vi.fn();
  const reportHealth = vi.fn();
  const switchSpace = vi.fn();
  const client: ApiClient = overrides.upload
    ? { ...api, upload: overrides.upload as ApiClient['upload'] }
    : api;
  const env: AppEnv = {
    api: client,
    principal: TEST_PRINCIPAL,
    author: TEST_AUTHOR,
    limits: TEST_LIMITS,
    space: SPACES[overrides.space ?? 'dev'],
    vaults: { items: overrides.vaults ?? VAULTS, loading: false, error: null },
    vault: overrides.vault ?? '*',
    setVault,
    refreshVaults,
    navigate,
    toast,
    health: null,
    reportHealth,
    recallDegraded: null,
    switchSpace,
  };
  const result = render(<AppContext.Provider value={env}>{ui}</AppContext.Provider>);
  /** 以新的環境值（例如換 vault）重新渲染同一棵元件樹，模擬 Shell 的 context 更新 */
  const rerenderWith = (next: Partial<AppEnv>) =>
    result.rerender(<AppContext.Provider value={{ ...env, ...next }}>{ui}</AppContext.Provider>);
  return { ...result, navigate, toast, refreshVaults, setVault, reportHealth, switchSpace, rerenderWith };
}

/** 字串 → base64（UTF-8 位元組；btoa 只吃 Latin-1，中文要先編碼）。側載 content_base64 的測試資料用。 */
export function base64Utf8(text: string): string {
  let bin = '';
  new TextEncoder().encode(text).forEach((b) => (bin += String.fromCharCode(b)));
  return btoa(bin);
}
