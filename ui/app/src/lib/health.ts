// `/v1/status` 的整理：頂列徽章計數、doctor 分組排序、備份與收料新鮮度判讀。
import type { HealthBadge } from './context';
import { formatTime } from './format';
import type { CheckStatus, DoctorCheck, StatusResult } from './types';

export function healthBadge(status: StatusResult): HealthBadge {
  const summary = status.doctor?.summary ?? { total: 0 };
  const fail = summary.fail ?? status.doctor?.checks.filter((c) => c.status === 'fail').length ?? 0;
  const warn = summary.warn ?? status.doctor?.checks.filter((c) => c.status === 'warn').length ?? 0;
  return { ok: status.ok, fail, warn, checkedAt: status.checked_at ?? null, error: null };
}

/** 狀態嚴重度：數字越小越前面（fail → warn → 未知 → skipped → pass）。 */
const RANK: Record<string, number> = { fail: 0, warn: 1, skipped: 3, pass: 4 };

export function statusRank(status: string): number {
  return RANK[status] ?? 2; // 不認得的狀態不能當成通過
}

export const STATUS_LABEL: Record<CheckStatus, string> = {
  fail: 'FAIL',
  warn: 'WARN',
  skipped: 'SKIP',
  pass: 'PASS',
};

export interface CheckGroup {
  category: string;
  checks: DoctorCheck[];
  /** 組內最嚴重的狀態 */
  worst: string;
}

/** 依分類分組；組內與組間都把最嚴重的排前面（fail 最前），同級保持服務回傳順序。 */
export function groupChecks(checks: DoctorCheck[]): CheckGroup[] {
  const groups = new Map<string, DoctorCheck[]>();
  for (const c of checks) {
    const list = groups.get(c.category) ?? [];
    list.push(c);
    groups.set(c.category, list);
  }
  const result: CheckGroup[] = [];
  for (const [category, list] of groups) {
    const sorted = list
      .map((c, i) => ({ c, i }))
      .sort((a, b) => statusRank(a.c.status) - statusRank(b.c.status) || a.i - b.i)
      .map((x) => x.c);
    result.push({ category, checks: sorted, worst: sorted[0]?.status ?? 'pass' });
  }
  return result
    .map((g, i) => ({ g, i }))
    .sort((a, b) => statusRank(a.g.worst) - statusRank(b.g.worst) || a.i - b.i)
    .map((x) => x.g);
}

/**
 * 收料新鮮度的暫定門檻（小時）：超過即標為過久未收料。
 * 計畫未定門檻，這是 UI 暫定值（見回報的需裁決項）。
 */
export const EPISODE_STALE_HOURS = 72;

export function hoursSince(iso: string | null | undefined, now: Date = new Date()): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, (now.getTime() - t) / 3_600_000);
}

export function formatAge(hours: number | null): string {
  if (hours === null) return '—';
  if (hours < 1) return `${Math.round(hours * 60)} 分鐘前`;
  if (hours < 48) return `${hours.toFixed(1)} 小時前`;
  return `${Math.round(hours / 24)} 天前`;
}

/**
 * 服務的備份明細是「最近一次：<ISO 時間>（<檔名>）」：時間改成全站一致的本地格式，檔名另列。
 * 格式不符就原樣顯示，不吞掉資訊。
 */
export function parseBackupDetail(detail: string | null): { time: string; file: string | null } | null {
  if (!detail) return null;
  const m = /^最近一次：\s*(\S+?)\s*(?:（(.+)）)?$/.exec(detail);
  if (!m) return { time: detail.replace(/^最近一次：\s*/, ''), file: null };
  return { time: formatTime(m[1]), file: m[2] ?? null };
}
