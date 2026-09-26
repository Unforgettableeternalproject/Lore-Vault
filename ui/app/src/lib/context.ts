// 畫面共用的環境：API client、目前 space、vault 清單與篩選、導覽、toast。
// 由 Shell 提供；元件測試以假的值直接包 Provider。
import { createContext } from 'preact';
import { useContext } from 'preact/hooks';

import type { ApiClient } from './api';
import type { Navigate } from './router';
import type { SpaceId, SpaceMeta } from './spaces';
import type { SessionLimits, VaultSummary } from './types';

export type ToastKind = 'success' | 'error' | 'warning' | 'info';

export interface VaultsState {
  items: VaultSummary[];
  loading: boolean;
  error: string | null;
}

/** 頂列與側欄的健檢徽章：取自最近一次 `/v1/status`。 */
export interface HealthBadge {
  ok: boolean;
  fail: number;
  warn: number;
  checkedAt: string | null;
  /** 取得狀態失敗時的說明（徽章改顯示「健檢無法取得」） */
  error: string | null;
}

export interface AppEnv {
  api: ApiClient;
  /** 登入帳號（session principal，A22／A23） */
  principal: string;
  /** 登入者的顯示名稱（session display_name）：UI 寫入 note 一律以它署名（A22 author） */
  author: string;
  /** 服務端限制值（session limits，缺漏時以服務預設補齊） */
  limits: SessionLimits;
  space: SpaceMeta;
  vaults: VaultsState;
  /** 目前篩選的 vault key；`*` = 本 space 全部 */
  vault: string;
  setVault: (key: string) => void;
  refreshVaults: () => void;
  navigate: Navigate;
  toast: (message: string, kind?: ToastKind) => void;
  health: HealthBadge | null;
  /** 系統健康頁重新整理後同步頂列徽章 */
  reportHealth: (badge: HealthBadge) => void;
  /** 切換目前 space（重置 vault 篩選），可同時導向指定路徑 */
  switchSpace: (space: SpaceId, path?: string) => void;
}

export const ALL = '*';

export const AppContext = createContext<AppEnv | null>(null);

export function useApp(): AppEnv {
  const env = useContext(AppContext);
  if (!env) throw new Error('useApp 必須在 AppContext.Provider 內使用');
  return env;
}

export function vaultName(env: Pick<AppEnv, 'vaults'>, key: string): string {
  if (key === ALL) return '本 space 全部';
  return env.vaults.items.find((v) => v.key === key)?.display ?? key;
}
