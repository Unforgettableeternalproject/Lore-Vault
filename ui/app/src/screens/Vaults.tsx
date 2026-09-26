// Vault 列表與管理（T-82）：本 space 的 vault、建立、編輯顯示名稱，進入維護頁。
// 列表沿用 Shell 已載入的 `/v1/vault_list`（同一份資料，避免兩處不一致）；寫入後 refreshVaults。
import { useState } from 'preact/hooks';

import { ErrorState, Loading } from '../components/ui';
import { ApiError } from '../lib/api';
import { useApp } from '../lib/context';
import { describeError, formatTime } from '../lib/format';
import { routePath } from '../lib/router';
import type { VaultSummary } from '../lib/types';

export const ORIGIN_LABEL: Record<string, string> = {
  manual: '手動建立',
  episode: '收料自動建立',
  pipeline: '管線建立',
};

export function originLabel(origin: string | undefined): string {
  if (!origin) return '來源未知';
  return ORIGIN_LABEL[origin] ?? origin;
}

export function Vaults() {
  const { api, space, vaults, navigate, toast, refreshVaults } = useApp();
  const prefix = space.id === 'dev' ? '' : `${space.id}/`;
  const [creating, setCreating] = useState(false);
  const [key, setKey] = useState(prefix);
  const [display, setDisplay] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [editing, setEditing] = useState<string | null>(null);

  const create = async (e: Event) => {
    e.preventDefault();
    const k = key.trim();
    if (!k || k === prefix || !display.trim() || busy) return;
    if (prefix && !k.startsWith(prefix)) {
      setError(`${space.en} space 的 key 必須以「${prefix}」開頭`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { data } = await api.post<VaultSummary>('/v1/vaults', { space: space.id, key: k, display: display.trim() });
      toast(`已建立 ${space.en} / ${data.display ?? display.trim()}`, 'success');
      setCreating(false);
      setKey(prefix);
      setDisplay('');
      refreshVaults();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section class="lv-screen lv-screen--wide">
      <div class="lv-screen__head">
        <div>
          <div class="lv-eyebrow">VAULTS · {space.en} SPACE</div>
          <h1 class="lv-title lv-title--tight">Vault</h1>
        </div>
        <button type="button" class="btn-outline btn-outline--gold" aria-expanded={creating} onClick={() => setCreating(!creating)}>
          + 建立 vault
        </button>
      </div>

      {creating && (
        <form class="lv-card lv-create" onSubmit={(e) => void create(e)} aria-label="建立 vault">
          <label class="lv-field">
            <span class="lv-field__label">KEY</span>
            <input
              class="lv-input"
              value={key}
              placeholder={prefix ? `${prefix}名稱` : 'github.com/org/repo 或 folder/名稱'}
              onInput={(e) => setKey((e.target as HTMLInputElement).value)}
            />
          </label>
          <label class="lv-field">
            <span class="lv-field__label">顯示名稱</span>
            <input class="lv-input" value={display} onInput={(e) => setDisplay((e.target as HTMLInputElement).value)} />
          </label>
          <p class="lv-hint lv-hint--inline">
            {prefix
              ? `${space.en} 沒有 repo，key 必須以「${prefix}」開頭；key 全域唯一（別的 space 已用的 key 會被拒）。`
              : 'dev 的 key 通常是 git remote 正規化結果；agent 也會依工作目錄自動解析。key 全域唯一。'}
          </p>
          {error !== null && (
            <p class="lv-notice lv-notice--error" role="alert">
              {describeError(error)}
            </p>
          )}
          <div class="lv-actions">
            <button type="submit" class="btn-outline btn-outline--gold" disabled={busy || !key.trim() || key.trim() === prefix || !display.trim()}>
              {busy ? '建立中…' : `建立於 ${space.en}`}
            </button>
            <button type="button" class="btn-outline" onClick={() => setCreating(false)}>
              取消
            </button>
          </div>
        </form>
      )}

      {vaults.loading && vaults.items.length === 0 && <Loading />}
      {vaults.error && <ErrorState error={`vault 列表載入失敗：${vaults.error}`} onRetry={refreshVaults} />}
      {!vaults.loading && !vaults.error && vaults.items.length === 0 && (
        <div class="zone-state lv-empty">這個 space 還沒有 vault。</div>
      )}

      {vaults.items.length > 0 && (
        <div class="lv-table lv-table--vaults" role="table" aria-label={`${space.en} 的 vault`}>
          <div class="lv-table__head" role="row">
            <span role="columnheader">名稱 / KEY</span>
            <span role="columnheader" class="is-right">筆記</span>
            <span role="columnheader" class="is-right">文件</span>
            <span role="columnheader">最近更新</span>
            <span role="columnheader">來源</span>
            <span role="columnheader">
              <span class="lv-visually-hidden">操作</span>
            </span>
          </div>
          {vaults.items.map((v) => (
            <div class="lv-table__row" role="row" key={v.key} data-vault={v.key}>
              <div class="lv-table__main" role="cell">
                {editing === v.key ? (
                  <RenameForm vault={v} onDone={() => setEditing(null)} />
                ) : (
                  <>
                    <span class="lv-table__title">{v.display}</span>
                    <span class="lv-mono lv-muted">{v.key}</span>
                    {v.aliases.length > 0 && <span class="lv-table__sub">別名 {v.aliases.length}：{v.aliases.join('、')}</span>}
                  </>
                )}
              </div>
              <span role="cell" class="is-right lv-mono">{v.note_count}</span>
              <span role="cell" class="is-right lv-mono">{v.document_count ?? '—'}</span>
              <span role="cell" class="lv-small">{v.last_updated ? formatTime(v.last_updated) : '尚無內容'}</span>
              <span role="cell" class="lv-small">{originLabel(v.origin)}</span>
              <span role="cell" class="lv-row-actions">
                {editing !== v.key && (
                  <button type="button" class="btn-outline btn-outline--sm" aria-label={`編輯 ${v.display} 的顯示名稱`} onClick={() => setEditing(v.key)}>
                    改名
                  </button>
                )}
                <a
                  class="btn-outline btn-outline--sm"
                  href={routePath('maint', [v.key])}
                  aria-label={`維護 ${v.display}`}
                  onClick={(e) => {
                    e.preventDefault();
                    navigate(routePath('maint', [v.key]));
                  }}
                >
                  維護 →
                </a>
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function RenameForm({ vault, onDone }: { vault: VaultSummary; onDone: () => void }) {
  const { api, space, toast, refreshVaults } = useApp();
  const [display, setDisplay] = useState(vault.display);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const save = async (e: Event) => {
    e.preventDefault();
    const next = display.trim();
    if (!next || busy) return;
    if (next === vault.display) {
      onDone();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { data } = await api.post<VaultSummary>('/v1/vault_update', { space: space.id, vault: vault.key, display: next });
      toast(`顯示名稱已改為「${data.display}」`, 'success');
      refreshVaults();
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err : String(err));
      setBusy(false);
    }
  };

  return (
    <form class="lv-inline-form" onSubmit={(e) => void save(e)}>
      <input
        class="lv-input"
        aria-label="新的顯示名稱"
        value={display}
        onInput={(e) => setDisplay((e.target as HTMLInputElement).value)}
      />
      <button type="submit" class="btn-outline btn-outline--sm btn-outline--gold" disabled={busy || !display.trim()}>
        {busy ? '儲存中…' : '儲存'}
      </button>
      <button type="button" class="btn-outline btn-outline--sm" onClick={onDone}>
        取消
      </button>
      {error !== null && (
        <p class="lv-notice lv-notice--error" role="alert">
          {describeError(error)}
        </p>
      )}
    </form>
  );
}
