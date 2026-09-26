// 極簡前端路由：/ui/<screen>。服務端對 /ui/* 做 SPA fallback，直接開深層網址也能載入。
import { useEffect, useState } from 'preact/hooks';

export const SCREENS = [
  { id: 'search', glyph: '◎', label: '檢索' },
  { id: 'notes', glyph: '¶', label: '筆記' },
  { id: 'docs', glyph: '▤', label: '文件' },
  { id: 'memory', glyph: '✦', label: '記憶層' },
  { id: 'vaults', glyph: '▦', label: 'Vault' },
  { id: 'maint', glyph: '↻', label: '維護' },
  { id: 'health', glyph: '✓', label: '系統健康' },
  { id: 'settings', glyph: '⇄', label: '連線設定' },
] as const;

export type ScreenId = (typeof SCREENS)[number]['id'];

const BASE = '/ui/';

export function screenFromPath(pathname: string): ScreenId {
  const rest = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : '';
  const first = rest.split('/')[0] ?? '';
  return SCREENS.some((s) => s.id === first) ? (first as ScreenId) : 'search';
}

export function useScreen(): [ScreenId, (next: ScreenId) => void] {
  const [screen, setScreen] = useState<ScreenId>(() => screenFromPath(window.location.pathname));
  useEffect(() => {
    const onPop = () => setScreen(screenFromPath(window.location.pathname));
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  const go = (next: ScreenId) => {
    if (next !== screen) window.history.pushState(null, '', BASE + next);
    setScreen(next);
  };
  return [screen, go];
}
