// 全站快捷鍵（T-87）：`/` 聚焦檢索、`g` 再按一個字母跳到畫面、`?` 開說明。
// 守衛：焦點在輸入元件（input／textarea／select／contenteditable）、按著 Ctrl／Meta／Alt、
// 輸入法組字中（isComposing，中文輸入必須排除）、或有 modal 對話框開著時一律不觸發。
import type { ScreenId } from './router';

export interface ShortcutDef {
  keys: string;
  label: string;
}

/** `g` 之後的第二鍵 → 畫面 */
export const GOTO: Record<string, ScreenId> = {
  s: 'search',
  n: 'notes',
  d: 'docs',
  m: 'memory',
  v: 'vaults',
  r: 'maint',
  h: 'health',
  c: 'settings',
};

const GOTO_LABEL: Record<ScreenId, string> = {
  search: '檢索',
  notes: '筆記',
  docs: '文件',
  memory: '記憶層',
  vaults: 'Vault',
  maint: '維護',
  health: '系統健康',
  settings: '連線設定',
};

/** 說明面板列出的快捷鍵（與實際行為同一份定義） */
export const SHORTCUTS: ShortcutDef[] = [
  { keys: '/', label: '聚焦檢索框（不在檢索頁時先切過去）' },
  ...Object.entries(GOTO).map(([k, screen]) => ({ keys: `g ${k}`, label: `前往${GOTO_LABEL[screen]}` })),
  { keys: '?', label: '開啟這個快捷鍵說明' },
  { keys: 'Esc', label: '關閉對話框、選單或側欄' },
];

/** 第二鍵等待時間（毫秒） */
export const CHORD_TIMEOUT_MS = 1200;

export interface ShortcutActions {
  focusSearch: () => void;
  goto: (screen: ScreenId) => void;
  openHelp: () => void;
}

/** 是否為正在打字的位置（不能搶走使用者的按鍵） */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!target || typeof (target as Element).closest !== 'function') return false;
  const el = target as HTMLElement;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = ((el as HTMLInputElement).type || 'text').toLowerCase();
    // 核取、按鈕類的 input 不算打字
    return !['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file'].includes(type);
  }
  return Boolean(el.closest('[contenteditable="true"]'));
}

function modalOpen(doc: Document): boolean {
  return doc.querySelector('[role="dialog"][aria-modal="true"]') !== null;
}

/**
 * 建立 keydown 處理器。回傳的函式處理了按鍵就呼叫 preventDefault 並回傳 true。
 * `now` 可注入（測試用）。
 */
export function createShortcutHandler(actions: ShortcutActions, now: () => number = () => Date.now()) {
  let pendingG: number | null = null;
  return (e: KeyboardEvent): boolean => {
    if (e.defaultPrevented || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return false;
    if (isTypingTarget(e.target)) return false;
    const doc = (e.target as Node | null)?.ownerDocument ?? document;
    if (modalOpen(doc)) return false;

    const key = e.key;
    if (pendingG !== null) {
      const fresh = now() - pendingG <= CHORD_TIMEOUT_MS;
      pendingG = null;
      const screen = GOTO[key.toLowerCase()];
      if (fresh && screen) {
        e.preventDefault();
        actions.goto(screen);
        return true;
      }
    }
    switch (key) {
      case '/':
        e.preventDefault();
        actions.focusSearch();
        return true;
      case '?':
        e.preventDefault();
        actions.openHelp();
        return true;
      case 'g':
        if (e.shiftKey) return false;
        pendingG = now();
        return true;
      default:
        return false;
    }
  };
}
