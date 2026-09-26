// 每位檢視者的介面偏好（主題、目前 space）。只是便利設定：
// 瀏覽器儲存不可用（無痕、封鎖）時退回預設值，畫面照常運作。
import { SPACE_IDS, type SpaceId } from './spaces';

export type Theme = 'dark' | 'light';

const THEME_KEY = 'lore-vault.theme';
const SPACE_KEY = 'lore-vault.space';

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

// ── 署名（A22 author）──
// UI 寫入 note 一律帶的作者名。A22 的用意是分清誰做了什麼，所以不提供關閉開關。
export const UI_AUTHOR = 'Xavier (Bernie)';
