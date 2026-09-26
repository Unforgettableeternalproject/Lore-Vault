// 系統健康頁的判讀：備份明細的時間要轉成全站一致的本地格式。
import { describe, expect, it } from 'vitest';

import { formatTime } from './format';
import { parseBackupDetail } from './health';

describe('parseBackupDetail', () => {
  it('ISO 時間改成本地格式、檔名另列', () => {
    const iso = '2026-09-26T10:05:40.773Z';
    const r = parseBackupDetail(`最近一次：${iso}（lore-20260926T100540773Z.db）`);
    expect(r).toEqual({ time: formatTime(iso), file: 'lore-20260926T100540773Z.db' });
    expect(r!.time).not.toContain('T10:05');
  });

  it('沒有檔名或格式不符時不吞掉資訊', () => {
    const iso = '2026-09-26T10:05:40Z';
    expect(parseBackupDetail(`最近一次：${iso}`)).toEqual({ time: formatTime(iso), file: null });
    expect(parseBackupDetail(null)).toBeNull();
  });
});
