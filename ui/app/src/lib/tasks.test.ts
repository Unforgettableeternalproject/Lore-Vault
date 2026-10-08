// 任務層快照：base64（含中文）解碼、形狀檢查、篩選分組、過時判斷，以及 blob_get 的 404 語意。
import { describe, expect, it } from 'vitest';

import { apiError, base64Utf8, json, makeApi } from '../test/harness';
import {
  decodeBase64Utf8,
  fetchTaskSnapshots,
  filterRows,
  isStale,
  parseSnapshot,
  statusGroup,
  statusView,
  TASKS_SNAPSHOT_KEY,
  type TaskRow,
} from './tasks';
import type { TaskChange } from './types';

function b64(value: unknown): string {
  return base64Utf8(typeof value === 'string' ? value : JSON.stringify(value));
}

function change(overrides: Partial<TaskChange> = {}): TaskChange {
  return {
    name: 'add-sidecar',
    status: '可開工',
    reasons: [],
    blocked_by: [],
    depends_on: [],
    requires_authorization: false,
    tasks: { done: 1, total: 3 },
    source: 'T-90',
    why: '需要側載',
    specs: [],
    note_id: null,
    archived_at: null,
    ...overrides,
  };
}

describe('decodeBase64Utf8', () => {
  it('中文內容解回原字串（不是 atob 的 Latin-1 亂碼）', () => {
    expect(decodeBase64Utf8(b64('可開工／被擋住'))).toBe('可開工／被擋住');
  });
});

describe('parseSnapshot', () => {
  it('合法快照：補齊選填欄位的預設值', () => {
    const parsed = parseSnapshot({
      mime: 'application/json',
      content_base64: b64({ schema: 1, changes: [{ name: 'x', status: '被擋住' }] }),
    });
    expect(typeof parsed).toBe('object');
    if (typeof parsed === 'string') return;
    expect(parsed.changes[0]).toMatchObject({
      name: 'x',
      status: '被擋住',
      reasons: [],
      blocked_by: [],
      depends_on: [],
      requires_authorization: false,
      tasks: { done: 0, total: 0 },
      source: null,
      note_id: null,
    });
  });

  it.each([
    ['不是 JSON', b64('{oops'), '不是有效的 JSON'],
    ['不是 base64', '%%%', 'base64'],
    ['缺 schema', b64({ changes: [] }), 'schema'],
    ['未知版本', b64({ schema: 2, changes: [] }), '版本 2'],
    ['缺 changes', b64({ schema: 1 }), 'changes'],
    ['change 缺 name', b64({ schema: 1, changes: [{ status: '可開工' }] }), '缺少 name'],
    ['blocked_by 格式錯', b64({ schema: 1, changes: [{ name: 'a', status: '可開工', blocked_by: ['D6'] }] }), 'blocked_by'],
    ['tasks 格式錯', b64({ schema: 1, changes: [{ name: 'a', status: '可開工', tasks: '1/2' }] }), 'tasks'],
  ])('%s → 回說明字串', (_label, content, fragment) => {
    const parsed = parseSnapshot({ mime: 'application/json', content_base64: content });
    expect(typeof parsed).toBe('string');
    expect(parsed).toContain(fragment);
  });
});

describe('狀態分組與呈現', () => {
  it('無法判定與認不得的狀態併入「被擋住」分組，但呈現為錯誤色', () => {
    expect(statusGroup('無法判定')).toBe('blocked');
    expect(statusGroup('weird')).toBe('blocked');
    expect(statusView('無法判定')).toEqual({ label: '無法判定', tone: 'error', unrecognized: false });
    expect(statusView('weird')).toEqual({ label: '未知狀態：weird', tone: 'error', unrecognized: true });
    expect(statusView('被擋住').tone).toBe('warn');
    expect(statusView('待授權').tone).toBe('auth');
    expect(statusView('可開工').tone).toBe('ready');
    expect(statusView('已完成').tone).toBe('plain');
  });

  const rows: TaskRow[] = ['可開工', '被擋住', '無法判定', '待授權', '已完成'].map((status, i) => ({
    vault: 'v',
    updated: '2026-10-08T00:00:00Z',
    change: change({ name: `c${i}`, status }),
  }));

  it('篩選：預設「未完成」排除已完成；各狀態單選；「全部」含已完成', () => {
    const names = (r: TaskRow[]) => r.map((x) => x.change.name);
    expect(names(filterRows(rows, ''))).toEqual(['c0', 'c1', 'c2', 'c3']);
    expect(names(filterRows(rows, 'all'))).toEqual(['c0', 'c1', 'c2', 'c3', 'c4']);
    expect(names(filterRows(rows, 'ready'))).toEqual(['c0']);
    expect(names(filterRows(rows, 'blocked'))).toEqual(['c1', 'c2']);
    expect(names(filterRows(rows, 'auth'))).toEqual(['c3']);
    expect(names(filterRows(rows, 'done'))).toEqual(['c4']);
  });
});

describe('isStale', () => {
  const now = new Date('2026-10-08T12:00:00Z');
  it('超過 24 小時才算過時；時間缺漏或無法解析視為過時', () => {
    expect(isStale('2026-10-07T13:00:00Z', now)).toBe(false);
    expect(isStale('2026-10-07T11:59:00Z', now)).toBe(true);
    expect(isStale(null, now)).toBe(true);
    expect(isStale('not-a-date', now)).toBe(true);
  });
});

describe('fetchTaskSnapshots', () => {
  const record = { mime: 'application/json', content_base64: b64({ schema: 1, changes: [change()] }), updated: '2026-10-08T00:00:00Z' };

  it('全部 vault：省略 vault 參數，回每個 vault 一筆', async () => {
    const { api, callsTo } = makeApi({ '/v1/blob_get': () => json({ items: [{ vault: 'a', ...record }, { vault: 'b', ...record }] }) });
    const result = await fetchTaskSnapshots(api, 'dev', '*');
    expect(callsTo('/v1/blob_get')[0]!.body).toEqual({ space: 'dev', key: TASKS_SNAPSHOT_KEY });
    expect(result.map((r) => r.vault)).toEqual(['a', 'b']);
    expect(result[0]!.snapshot?.changes[0]?.name).toBe('add-sidecar');
  });

  it('單一 vault：帶 vault；404 not_found 代表尚未同步（空陣列）', async () => {
    const { api, callsTo } = makeApi({ '/v1/blob_get': () => apiError(404, 'not_found') });
    expect(await fetchTaskSnapshots(api, 'dev', 'folder/x')).toEqual([]);
    expect(callsTo('/v1/blob_get')[0]!.body).toEqual({ space: 'dev', vault: 'folder/x', key: TASKS_SNAPSHOT_KEY });
  });

  it('其他 404（例如端點不存在）不當成尚未同步，照常丟錯', async () => {
    const { api } = makeApi({ '/v1/blob_get': () => ({ status: 404, body: { detail: 'Not Found' } }) });
    await expect(fetchTaskSnapshots(api, 'dev', 'folder/x')).rejects.toMatchObject({ status: 404, code: 'http_404' });
  });

  it('格式壞掉的快照保留為該 vault 的錯誤，不丟棄', async () => {
    const { api } = makeApi({
      '/v1/blob_get': () => json({ items: [{ vault: 'a', mime: 'application/json', content_base64: b64('nope'), updated: record.updated }] }),
    });
    const [only] = await fetchTaskSnapshots(api, 'dev', '*');
    expect(only).toMatchObject({ vault: 'a', snapshot: null });
    expect(only!.error).toContain('JSON');
  });
});
