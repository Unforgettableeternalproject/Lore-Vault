/** @vitest-environment happy-dom */
// 服務設定區塊：分類分組、來源與預設值、只送出有變動的項目（型別正確）、前端與服務端逐欄驗證、還原預設、不合法覆寫提示。
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import type { SettingItem, SettingsResult } from '../lib/types';
import { apiError, json, makeApi, renderWithApp, type Handler } from '../test/harness';
import { Settings } from './Settings';

afterEach(cleanup);

const SESSION = json({
  authenticated: true,
  principal: 'UEPBernie',
  display_name: 'Xavier (Bernie)',
  expires_at: '2026-09-27T12:00:00.000Z',
  idle_expires_at: '2026-09-27T01:00:00.000Z',
});

function item(overrides: Partial<SettingItem> & Pick<SettingItem, 'key'>): SettingItem {
  return {
    type: 'bool',
    category: 'privacy',
    label: overrides.key,
    description: '說明',
    min: null,
    max: null,
    unit: null,
    value: false,
    default: false,
    source: 'default',
    override: null,
    ...overrides,
  };
}

function result(overrides: Partial<SettingsResult> = {}): SettingsResult {
  return {
    categories: [
      { id: 'privacy', label: '收料與隱私' },
      { id: 'ask', label: '問答' },
    ],
    items: [
      item({ key: 'episodes.ingest', label: '接收 episode（對話紀錄）' }),
      item({
        key: 'ask.snippet_max_chars',
        category: 'ask',
        type: 'int',
        label: '每則筆記送進模型的字數上限',
        min: 500,
        max: 50000,
        unit: '字元',
        value: 3000,
        default: 6000,
        source: 'override',
        override: { updated: '2026-09-27T00:00:00.000Z', updated_by: 'UEPBernie' },
      }),
    ],
    invalid_overrides: [],
    audit: [
      {
        seq: 1,
        at: '2026-09-27T00:00:00.000Z',
        key: 'ask.snippet_max_chars',
        action: 'set',
        old_value: 6000,
        new_value: 3000,
        principal: 'UEPBernie',
        display: 'Xavier (Bernie)',
      },
    ],
    ...overrides,
  };
}

function setup(handlers: Record<string, Handler> = {}) {
  const made = makeApi({ '/ui/api/session': () => SESSION, '/v1/settings': () => json(result()), ...handlers });
  const rendered = renderWithApp(<Settings onLogout={() => undefined} />, made.api);
  return { ...made, ...rendered };
}

describe('服務設定', () => {
  it('依分類分組，顯示來源、預設值與最近的修改', async () => {
    setup();
    const privacy = await screen.findByTestId('settings-group-privacy');
    expect(screen.getByRole('group', { name: '收料與隱私' })).toBe(privacy);
    expect(screen.getByTestId('setting-source-episodes.ingest').textContent).toBe('預設');
    expect(screen.getByTestId('setting-source-ask.snippet_max_chars').textContent).toBe('已覆寫');
    const row = screen.getByTestId('setting-ask.snippet_max_chars');
    expect(row.textContent).toContain('預設：6,000 字元');
    expect(row.textContent).toContain('範圍 500 字元～50,000 字元');
    expect(screen.getByTestId('settings-audit').textContent).toContain('Xavier (Bernie) 修改「每則筆記送進模型的字數上限」：6,000 字元 → 3,000 字元');
    // 沒有變更時不能儲存
    expect((screen.getByRole('button', { name: '沒有變更' }) as HTMLButtonElement).disabled).toBe(true);
    // 使用者看得到的文字不出現內部編號
    expect(document.body.textContent).not.toMatch(/\b[AD]\d{1,2}\b|T-\d+/);
  });

  it('只送出有變動的項目，開關為布林、數字為數值', async () => {
    const { callsTo, toast } = setup({
      '/v1/settings_update': (body) => {
        const values = body.values as Record<string, unknown>;
        return json({
          ...result(),
          items: result().items.map((i) => (i.key in values ? { ...i, value: values[i.key] as boolean | number, source: 'override' } : i)),
          changed: Object.keys(values).map((key, n) => ({ seq: 10 + n, key })),
        });
      },
    });
    const toggle = (await screen.findByLabelText('接收 episode（對話紀錄）')) as HTMLInputElement;
    fireEvent.click(toggle);
    const number = screen.getByLabelText('每則筆記送進模型的字數上限') as HTMLInputElement;
    fireEvent.input(number, { target: { value: '4000' } });
    fireEvent.click(screen.getByRole('button', { name: '儲存 2 項變更' }));
    await waitFor(() => expect(callsTo('/v1/settings_update')).toHaveLength(1));
    expect(callsTo('/v1/settings_update')[0]!.body).toEqual({ values: { 'episodes.ingest': true, 'ask.snippet_max_chars': 4000 } });
    await waitFor(() => expect(toast).toHaveBeenCalledWith('已儲存 2 項設定，立即生效'));
    expect(screen.getByTestId('setting-source-episodes.ingest').textContent).toBe('已覆寫');
  });

  it('前端先擋範圍外與小數，不送出', async () => {
    const { callsTo } = setup();
    const number = (await screen.findByLabelText('每則筆記送進模型的字數上限')) as HTMLInputElement;
    fireEvent.input(number, { target: { value: '100' } });
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '不可小於 500 字元');
    expect(number.getAttribute('aria-invalid')).toBe('true');
    fireEvent.input(number, { target: { value: '1000.5' } });
    expect(screen.getByRole('alert').textContent).toBe('必須是整數');
    const save = screen.getByRole('button', { name: '儲存 1 項變更' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(callsTo('/v1/settings_update')).toHaveLength(0);
  });

  it('服務端逐項錯誤顯示在對應欄位下', async () => {
    setup({
      '/v1/settings_update': () =>
        apiError(400, 'invalid_setting', {
          errors: [{ key: 'episodes.ingest', code: 'invalid_value', message: '必須是 true 或 false' }],
        }),
    });
    fireEvent.click(await screen.findByLabelText('接收 episode（對話紀錄）'));
    fireEvent.click(screen.getByRole('button', { name: '儲存 1 項變更' }));
    const row = screen.getByTestId('setting-episodes.ingest');
    await waitFor(() => expect(within(row).getByRole('alert').textContent).toBe('必須是 true 或 false'));
  });

  it('已覆寫的項目可單項還原預設', async () => {
    const { callsTo, toast } = setup({
      '/v1/settings_reset': () =>
        json({
          ...result(),
          items: result().items.map((i) => (i.key === 'ask.snippet_max_chars' ? { ...i, value: 6000, source: 'default', override: null } : i)),
          changed: [{ seq: 2 }],
        }),
    });
    const row = await screen.findByTestId('setting-ask.snippet_max_chars');
    fireEvent.click(within(row).getByRole('button', { name: '還原預設' }));
    await waitFor(() => expect(callsTo('/v1/settings_reset')).toHaveLength(1));
    expect(callsTo('/v1/settings_reset')[0]!.body).toEqual({ keys: ['ask.snippet_max_chars'] });
    await waitFor(() => expect(screen.getByTestId('setting-source-ask.snippet_max_chars').textContent).toBe('預設'));
    expect(toast).toHaveBeenCalledWith('「每則筆記送進模型的字數上限」已還原為預設值');
  });

  it('資料庫裡不合法的覆寫以錯誤橫幅提示', async () => {
    setup({
      '/v1/settings': () => json(result({ invalid_overrides: [{ key: 'episodes.ingest', reason: '必須是 true 或 false', updated: '2026-09-27T00:00:00.000Z' }] })),
    });
    const banner = await screen.findByTestId('settings-invalid');
    expect(banner.textContent).toContain('接收 episode（對話紀錄）：必須是 true 或 false');
  });

  it('讀取失敗時顯示錯誤並可重試', async () => {
    let n = 0;
    setup({ '/v1/settings': () => (++n === 1 ? apiError(403, 'ui_session_required') : json(result())) });
    expect(await screen.findByText(/服務設定只能由登入 UI 的管理者查看與修改/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '重試' }));
    expect(await screen.findByTestId('settings-group-privacy')).toBeTruthy();
  });
});
