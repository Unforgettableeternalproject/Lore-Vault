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

describe('stripInternalRefs', () => {
  it('去掉服務文字裡的內部編號與版本階段，保留說明', async () => {
    const { stripInternalRefs } = await import('./format');
    expect(stripInternalRefs('每則 note 都有 principal 與 updated_by_principal（A22）')).toBe(
      '每則 note 都有 principal 與 updated_by_principal',
    );
    expect(stripInternalRefs('不允許 lore → dev：dev 與 lore／personal 之間不互相轉換（A20）。')).toBe(
      '不允許 lore → dev：dev 與 lore／personal 之間不互相轉換。',
    );
    expect(stripInternalRefs('只允許互換（A20：dev 不與其他 space 轉換）')).toBe('只允許互換（dev 不與其他 space 轉換）');
    expect(stripInternalRefs('資料庫尚無 UI 登入表（schema v13 前）')).toBe('資料庫尚無 UI 登入表（舊版資料庫）');
    expect(stripInternalRefs('缺少 documents.warnings 欄（schema 未遷移到 v10）')).toBe('缺少 documents.warnings 欄（schema 未遷移到最新版）');
    expect(stripInternalRefs('墓碑缺檔名／格式（v11 前刪除）')).toBe('墓碑缺檔名／格式（舊版刪除）');
    // 一般內容不動
    expect(stripInternalRefs('pytest 2267、Flutter 1459 全綠；dev 預設')).toBe('pytest 2267、Flutter 1459 全綠；dev 預設');
  });
});
