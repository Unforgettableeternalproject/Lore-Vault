// 任務層啟用狀態（Vault 維護頁、任務頁）：讀 UI session 限定的 `/v1/tasks_status`，
// 以 `/v1/tasks_enable`／`/v1/tasks_disable` 啟用、停用。啟用＝服務端有該 vault 的任務索引；
// 停用只在索引加停用標記，內容全部保留，重新啟用即復原（服務端停用中拒收任務層寫入）。
import { useEffect, useState } from 'preact/hooks';

import type { ApiClient } from './api';
import { isAbort } from './format';

export interface TaskLayerDisabled {
  at: string | null;
  by: string | null;
}

export interface TaskLayerStates {
  active: number;
  pending_apply: number;
  archived: number;
}

export interface TaskLayerVault {
  vault: string;
  /** 服務端有任務索引（含停用中） */
  initialized: boolean;
  /** 有索引且未停用 */
  enabled: boolean;
  disabled: TaskLayerDisabled | null;
  changes: number | null;
  states: TaskLayerStates | null;
  error: string | null;
}

export interface TaskLayerStatus {
  remoteSync: boolean;
  vaults: Map<string, TaskLayerVault>;
}

export type TaskLayerState = 'enabled' | 'disabled' | 'off' | 'invalid';

export function layerState(v: TaskLayerVault | undefined): TaskLayerState {
  if (!v || !v.initialized) return 'off';
  if (v.error) return 'invalid';
  return v.disabled ? 'disabled' : 'enabled';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

function parseVault(raw: unknown): TaskLayerVault | null {
  if (!isRecord(raw) || typeof raw.vault !== 'string') return null;
  const disabled = isRecord(raw.disabled) ? { at: str(raw.disabled.at), by: str(raw.disabled.by) } : null;
  const states = isRecord(raw.states)
    ? {
        active: Number(raw.states.active) || 0,
        pending_apply: Number(raw.states.pending_apply) || 0,
        archived: Number(raw.states.archived) || 0,
      }
    : null;
  return {
    vault: raw.vault,
    initialized: raw.initialized === true,
    enabled: raw.enabled === true && disabled === null,
    disabled,
    changes: typeof raw.changes === 'number' ? raw.changes : null,
    states,
    error: str(raw.error),
  };
}

export function parseTaskLayerStatus(data: unknown): TaskLayerStatus {
  const vaults = new Map<string, TaskLayerVault>();
  const items = isRecord(data) && Array.isArray(data.vaults) ? data.vaults : [];
  for (const item of items) {
    const v = parseVault(item);
    if (v) vaults.set(v.vault, v);
  }
  return { remoteSync: !isRecord(data) || data.remote_sync !== false, vaults };
}

/** vault 省略或 `*`：dev space 全部 vault */
export async function fetchTaskLayerStatus(api: ApiClient, vault: string, signal?: AbortSignal): Promise<TaskLayerStatus> {
  const body = vault === '*' ? { space: 'dev' } : { space: 'dev', vault };
  const { data } = await api.post<unknown>('/v1/tasks_status', body, signal);
  return parseTaskLayerStatus(data);
}

export interface EnableResult {
  created: boolean;
  reenabled: boolean;
}

export async function enableTaskLayer(api: ApiClient, vault: string): Promise<EnableResult> {
  const { data } = await api.post<Partial<EnableResult>>('/v1/tasks_enable', { space: 'dev', vault });
  return { created: data?.created === true, reenabled: data?.reenabled === true };
}

export async function disableTaskLayer(api: ApiClient, vault: string): Promise<boolean> {
  const { data } = await api.post<{ changed?: boolean }>('/v1/tasks_disable', { space: 'dev', vault });
  return data?.changed === true;
}

/** 讀任務層狀態（`enabled` 為 false 時不發請求）；錯誤怎麼呈現由呼叫端決定。 */
export function useTaskLayer(api: ApiClient, vault: string, enabled = true) {
  const [status, setStatus] = useState<TaskLayerStatus | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    const ctrl = new AbortController();
    setError(null);
    fetchTaskLayerStatus(api, vault, ctrl.signal)
      .then((data) => {
        if (!ctrl.signal.aborted) setStatus(data);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
      });
    return () => ctrl.abort();
  }, [api, vault, tick, enabled]);

  return {
    status: enabled ? status : null,
    error: enabled ? error : null,
    reload: () => setTick((t) => t + 1),
  };
}

/** 「3 個 change（進行中 2 · 待落地 1）」；沒有計數時回 null */
export function describeCounts(v: TaskLayerVault): string | null {
  if (v.changes === null) return null;
  const parts: string[] = [];
  if (v.states) {
    if (v.states.active) parts.push(`進行中 ${v.states.active}`);
    if (v.states.pending_apply) parts.push(`待落地 ${v.states.pending_apply}`);
    if (v.states.archived) parts.push(`已封存 ${v.states.archived}`);
  }
  return `${v.changes} 個 change` + (parts.length ? `（${parts.join(' · ')}）` : '');
}

export function describeDisabled(d: TaskLayerDisabled, formatTime: (iso: string) => string): string {
  return `${d.by ?? '不明'} 於 ${d.at ? formatTime(d.at) : '時間不明'} 停用`;
}
