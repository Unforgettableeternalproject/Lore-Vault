/** @vitest-environment happy-dom */
// 頁面內 vault 篩選器：combobox 語意、搜尋、鍵盤操作；狀態寫回 AppEnv.setVault（與側欄共用）。
import { cleanup, fireEvent, screen } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import { makeApi, renderWithApp } from '../test/harness';
import type { VaultSummary } from '../lib/types';
import { VaultPicker } from './VaultPicker';

const VAULTS: VaultSummary[] = [
  { key: 'github.com/org/chatroom', display: 'Chatroom', kind: 'repo', space: 'dev', aliases: [], note_count: 208, document_count: 0 },
  { key: 'github.com/org/eternity', display: 'Eternity', kind: 'repo', space: 'dev', aliases: [], note_count: 340 },
  { key: 'folder/mcsf', display: 'MCSF', kind: 'folder', space: 'dev', aliases: [], note_count: 15 },
];

function setup(vault = '*') {
  const { api } = makeApi({});
  return renderWithApp(<VaultPicker />, api, { vaults: VAULTS, vault });
}

describe('VaultPicker', () => {
  afterEach(cleanup);

  it('收合時顯示目前 vault 名稱，展開列出「本 space 全部」與各 vault', () => {
    setup('github.com/org/eternity');
    const box = screen.getByRole('combobox', { name: 'vault 篩選' });
    expect(box).toHaveProperty('value', 'Eternity');
    expect(box.getAttribute('aria-expanded')).toBe('false');
    fireEvent.focus(box);
    expect(box.getAttribute('aria-expanded')).toBe('true');
    const options = screen.getAllByRole('option');
    expect(options.map((o) => o.textContent)).toEqual([
      expect.stringContaining('本 space 全部'),
      expect.stringContaining('Chatroom'),
      expect.stringContaining('Eternity'),
      expect.stringContaining('MCSF'),
    ]);
    // 目前選的 vault 標為 selected，且是鍵盤游標所在
    const current = screen.getByRole('option', { selected: true });
    expect(current.textContent).toContain('Eternity');
    expect(box.getAttribute('aria-activedescendant')).toBe(current.id);
  });

  it('輸入可依名稱或 key 篩選，點選即呼叫 setVault', () => {
    const { setVault } = setup();
    const box = screen.getByRole('combobox', { name: 'vault 篩選' });
    fireEvent.focus(box);
    fireEvent.input(box, { target: { value: 'mcsf' } });
    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(1);
    fireEvent.click(options[0]!);
    expect(setVault).toHaveBeenCalledWith('folder/mcsf');
    expect(box.getAttribute('aria-expanded')).toBe('false');
  });

  it('鍵盤：方向鍵移動、Enter 選取、Esc 關閉不選', () => {
    const { setVault } = setup();
    const box = screen.getByRole('combobox', { name: 'vault 篩選' });
    fireEvent.keyDown(box, { key: 'ArrowDown' }); // 展開，游標在目前選項（全部）
    fireEvent.keyDown(box, { key: 'ArrowDown' });
    fireEvent.keyDown(box, { key: 'ArrowDown' });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(setVault).toHaveBeenCalledWith('github.com/org/eternity');

    fireEvent.keyDown(box, { key: 'ArrowDown' });
    expect(box.getAttribute('aria-expanded')).toBe('true');
    fireEvent.keyDown(box, { key: 'Escape' });
    expect(box.getAttribute('aria-expanded')).toBe('false');
    expect(setVault).toHaveBeenCalledTimes(1);
  });

  it('選「本 space 全部」回到全部範圍；搜尋不到時顯示說明', () => {
    const { setVault } = setup('folder/mcsf');
    const box = screen.getByRole('combobox', { name: 'vault 篩選' });
    fireEvent.focus(box);
    fireEvent.click(screen.getByRole('option', { name: /本 space 全部/ }));
    expect(setVault).toHaveBeenCalledWith('*');
    fireEvent.focus(box);
    fireEvent.input(box, { target: { value: 'zzz-none' } });
    expect(screen.queryAllByRole('option')).toHaveLength(0);
    expect(screen.getByText(/沒有符合「zzz-none」的 vault/)).toBeTruthy();
  });
});
