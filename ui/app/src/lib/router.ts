// 極簡前端路由：/ui/<screen>[/<param>][?query]。服務端對 /ui/* 做 SPA fallback，直接開深層網址也能載入。
// 第一段決定畫面（與側欄高亮），其餘段落交給畫面自己解讀（例如 /ui/notes/<id>、/ui/docs/<id>?chunk=3）。
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

export const BASE = '/ui/';

export interface Route {
  screen: ScreenId;
  /** 畫面名稱之後的路徑段（已 decode） */
  params: string[];
  query: URLSearchParams;
}

export function screenFromPath(pathname: string): ScreenId {
  const rest = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : '';
  const first = rest.split('/')[0] ?? '';
  return SCREENS.some((s) => s.id === first) ? (first as ScreenId) : 'search';
}

export function parseRoute(pathname: string, search: string): Route {
  const screen = screenFromPath(pathname);
  const rest = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : '';
  const segments = rest.split('/').filter(Boolean);
  const params = segments[0] === screen ? segments.slice(1) : [];
  const decoded = params.map((p) => {
    try {
      return decodeURIComponent(p);
    } catch {
      return p; // 不合法的百分比編碼：原樣保留，讓畫面顯示「找不到」而不是崩潰
    }
  });
  return { screen, params: decoded, query: new URLSearchParams(search) };
}

/** 組出 /ui/ 下的路徑；params 會逐段編碼，query 省略空值。 */
export function routePath(
  screen: ScreenId,
  params: string[] = [],
  query: Record<string, string | number | null | undefined> = {},
): string {
  const path = BASE + [screen, ...params.map((p) => encodeURIComponent(p))].join('/');
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== null && v !== undefined && v !== '') qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

export type Navigate = (path: string, options?: { replace?: boolean }) => void;

function current(): Route {
  return parseRoute(window.location.pathname, window.location.search);
}

export function useRoute(): [Route, Navigate] {
  const [route, setRoute] = useState<Route>(current);
  useEffect(() => {
    const onPop = () => setRoute(current());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  const navigate: Navigate = (path, options) => {
    const here = window.location.pathname + window.location.search;
    if (path !== here) {
      if (options?.replace) window.history.replaceState(null, '', path);
      else window.history.pushState(null, '', path);
    }
    setRoute(current());
    window.scrollTo?.(0, 0);
  };
  return [route, navigate];
}
