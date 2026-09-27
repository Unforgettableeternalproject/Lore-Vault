// 分頁元件的頁碼視窗與日期轉換。
import { describe, expect, it } from 'vitest';

import { dayEndIso, dayStartIso, pageWindow, rangeParams } from './Pager';

describe('pageWindow', () => {
  it('頁數少時全列，多時保留首尾與目前頁前後並以省略號隔開', () => {
    expect(pageWindow(1, 5)).toEqual([1, 2, 3, 4, 5]);
    expect(pageWindow(1, 20)).toEqual([1, 2, 3, 4, null, 20]);
    expect(pageWindow(10, 20)).toEqual([1, null, 9, 10, 11, null, 20]);
    expect(pageWindow(20, 20)).toEqual([1, null, 17, 18, 19, 20]);
  });
});

describe('日期區間', () => {
  it('起日取本地當天 00:00、訖日取本地 23:59:59.999（含端點），空欄不送', () => {
    expect(dayStartIso('2026-09-01')).toBe(new Date(2026, 8, 1).toISOString());
    expect(dayEndIso('2026-09-01')).toBe(new Date(2026, 8, 1, 23, 59, 59, 999).toISOString());
    expect(dayStartIso('bad')).toBeNull();
    expect(rangeParams({ from: '', to: '' })).toEqual({});
    expect(rangeParams({ from: '2026-09-01', to: '' })).toEqual({ since: new Date(2026, 8, 1).toISOString() });
  });
});
