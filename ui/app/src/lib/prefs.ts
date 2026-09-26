// 每位檢視者的介面偏好（主題、目前 space）。只是便利設定：
// 瀏覽器儲存不可用（無痕、封鎖）時退回預設值，畫面照常運作。
import { SPACE_IDS, type SpaceId } from './spaces';

export type Theme = 'dark' | 'light';

const THEME_KEY = 'lore-vault.theme';
const SPACE_KEY = 'lore-vault.space';
const VAULTS_COLLAPSED_KEY = 'lore-vault.sidebar-vaults-collapsed';

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null; // 儲存被封鎖：視同沒有偏好
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // 儲存被封鎖：偏好只在這次頁面有效，不影響功能
  }
}

export function loadTheme(): Theme {
  return read(THEME_KEY) === 'light' ? 'light' : 'dark';
}

export function saveTheme(theme: Theme): void {
  write(THEME_KEY, theme);
}

export function loadSpace(): SpaceId {
  const value = read(SPACE_KEY);
  return (SPACE_IDS as readonly string[]).includes(value ?? '') ? (value as SpaceId) : 'dev';
}

export function saveSpace(space: SpaceId): void {
  write(SPACE_KEY, space);
}

/** 側欄 vault 區段是否收合（預設展開）。 */
export function loadVaultsCollapsed(): boolean {
  return read(VAULTS_COLLAPSED_KEY) === '1';
}

export function saveVaultsCollapsed(collapsed: boolean): void {
  write(VAULTS_COLLAPSED_KEY, collapsed ? '1' : '0');
}

// 署名（A22 author）不是偏好：一律用登入帳號的顯示名稱（session display_name，A23），
// 由 AppEnv.author 提供，不提供關閉開關。
