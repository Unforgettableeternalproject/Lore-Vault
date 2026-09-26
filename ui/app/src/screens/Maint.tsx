// Vault 維護（T-82）：別名與重新導向、換 space（A20 只允許 lore↔personal）、墓碑與還原、刪除整個 vault。
// `/ui/maint/<key>` 針對單一 vault；`/ui/maint` 列出本 space 的 vault 與全 space 墓碑（刪除 vault 後回到這裡）。
import { useEffect, useState } from 'preact/hooks';

import { Badge, ErrorState, Loading, TwoPhaseConfirm, TwoPhaseDelete } from '../components/ui';
import { useApp } from '../lib/context';
import { describeError, formatTime, isAbort } from '../lib/format';
import { routePath } from '../lib/router';
import { SPACES, SPACE_IDS, type SpaceId } from '../lib/spaces';
import type {
  DocumentUndeleteResult,
  NoteUndeleteResult,
  TombstoneItem,
  TombstonePage,
  TwoPhaseResponse,
  VaultSummary,
} from '../lib/types';
import { originLabel } from './Vaults';

export function Maint({ vaultKey }: { vaultKey: string | null }) {
  const { space, vaults, navigate } = useApp();
  const [local, setLocal] = useState<VaultSummary | null>(null);

  if (!vaultKey) {
    return (
      <section class="lv-screen lv-screen--wide">
        <div class="lv-eyebrow">MAINTENANCE · {space.en}</div>
        <h1 class="lv-title">維護</h1>
        <section class="lv-section" aria-labelledby="maint-pick">
          <div class="lv-section__head">
            <h2 class="lv-section__title" id="maint-pick">
              選擇 vault
            </h2>
            {vaults.items.length > 0 && (
              <span class="lv-section__stat" data-testid="maint-totals">
                {vaults.items.length} 個 vault · {vaults.items.reduce((n, v) => n + v.note_count, 0)} 筆記 ·{' '}
                {vaults.items.reduce((n, v) => n + (v.document_count ?? 0), 0)} 文件
              </span>
            )}
          </div>
          <p class="lv-section__desc">別名與重新導向、換 space、刪除都針對單一 vault；點卡片進入。</p>
          {vaults.loading && vaults.items.length === 0 && <Loading />}
          {vaults.error && <ErrorState error={`vault 列表載入失敗：${vaults.error}`} />}
          {!vaults.loading && !vaults.error && vaults.items.length === 0 && (
            <div class="zone-state lv-empty">這個 space 還沒有 vault。</div>
          )}
          {vaults.items.length > 0 && (
            <ul class="lv-vcards" aria-label={`${space.en} 的 vault`}>
              {vaults.items.map((v) => (
                <li key={v.key}>
                  <VaultCard vault={v} onOpen={() => navigate(routePath('maint', [v.key]))} />
                </li>
              ))}
            </ul>
          )}
        </section>
        <TombstoneSection vault="*" />
      </section>
    );
  }

  const listed = vaults.items.find((v) => v.key === vaultKey || v.aliases.includes(vaultKey)) ?? null;
  const vault = local && (local.key === listed?.key || !listed) ? local : listed;

  if (!vault) {
    if (vaults.loading) return <Loading />;
    return (
      <section class="lv-screen lv-screen--wide">
        <div class="lv-eyebrow">MAINTENANCE · {space.en}</div>
        <h1 class="lv-title">{vaultKey}</h1>
        {vaults.error ? (
          <ErrorState error={`vault 列表載入失敗：${vaults.error}`} />
        ) : (
          <div class="zone-state" role="status" data-testid="maint-missing">
            {space.en} space 沒有這個 vault（可能已刪除，或屬於其他 space）。已刪除 vault 的墓碑仍可在下方以原 key 查到。
          </div>
        )}
        <TombstoneSection vault={vaultKey} />
      </section>
    );
  }

  return (
    <section class="lv-screen lv-screen--wide">
      <div class="lv-eyebrow">
        MAINTENANCE · {space.en} / <span class="lv-mono">{vault.key}</span>
      </div>
      <h1 class="lv-title lv-title--tight">{vault.display}</h1>
      <p class="lv-meta-line">
        {vault.note_count} 筆記 · {vault.document_count ?? '—'} 文件 · {originLabel(vault.origin)}
        {vault.last_updated ? ` · 最近更新 ${formatTime(vault.last_updated)}` : ''}
      </p>
      <AliasSection vault={vault} onChanged={setLocal} />
      <MoveSection vault={vault} />
      <TombstoneSection vault={vault.key} />
      <DeleteSection vault={vault} />
    </section>
  );
}

// ── vault 卡片（維護入口）──

function VaultCard({ vault, onOpen }: { vault: VaultSummary; onOpen: () => void }) {
  return (
    <a
      class="lv-vcard"
      href={routePath('maint', [vault.key])}
      data-maint-vault={vault.key}
      onClick={(e) => {
        e.preventDefault();
        onOpen();
      }}
    >
      <span class="lv-vcard__head">
        <span class="lv-vcard__name">{vault.display}</span>
        <Badge tone="tag" label="來源">
          {originLabel(vault.origin)}
        </Badge>
      </span>
      <span class="lv-vcard__key lv-mono">{vault.key}</span>
      <span class="lv-vcard__stats">
        <span class="lv-vcard__stat">
          <span class="lv-vcard__n">{vault.note_count}</span>
          <span class="lv-vcard__k">筆記</span>
        </span>
        <span class="lv-vcard__stat">
          <span class="lv-vcard__n">{vault.document_count ?? '—'}</span>
          <span class="lv-vcard__k">文件</span>
        </span>
        <span class="lv-vcard__stat">
          <span class="lv-vcard__n">{vault.aliases.length}</span>
          <span class="lv-vcard__k">別名</span>
        </span>
      </span>
      <span class="lv-vcard__foot">
        <span>{vault.last_updated ? `最近更新 ${formatTime(vault.last_updated)}` : '尚無內容'}</span>
        <span class="lv-vcard__go" aria-hidden="true">
          維護 →
        </span>
      </span>
    </a>
  );
}

// ── 別名 ──

function AliasSection({ vault, onChanged }: { vault: VaultSummary; onChanged: (v: VaultSummary) => void }) {
  const { api, space, toast, refreshVaults } = useApp();
  const prefix = space.id === 'dev' ? '' : `${space.id}/`;
  const [alias, setAlias] = useState(prefix);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const call = async (path: string, name: string, label: string) => {
    setBusy(name);
    setError(null);
    try {
      const { data } = await api.post<VaultSummary>(path, { space: space.id, vault: vault.key, alias: name });
      onChanged(data);
      refreshVaults();
      toast(label, 'success');
      return true;
    } catch (err) {
      setError(err);
      return false;
    } finally {
      setBusy(null);
    }
  };

  const add = async (e: Event) => {
    e.preventDefault();
    const name = alias.trim();
    if (!name || name === prefix || busy) return;
    if (prefix && !name.startsWith(prefix)) {
      setError(`${space.en} space 的別名必須以「${prefix}」開頭`);
      return;
    }
    if (await call('/v1/vault_alias_add', name, `已將 ${name} 導向 ${vault.display}`)) setAlias(prefix);
  };

  return (
    <section class="lv-section" aria-labelledby="maint-alias">
      <h2 class="lv-section__title" id="maint-alias">
        別名與重新導向
      </h2>
      <p class="lv-section__desc">
        repo 改名後，agent 依新的 git remote 會算出新的 key。把新 key 加成這個 vault 的別名，用新 key 讀寫就會導向這裡；舊 key
        也仍能解析。若收料已經先用新 key 自動建了另一個 vault，新增會被拒（vault_exists）——目前沒有合併功能，需先處理那個 vault。
      </p>
      <ul class="lv-alias-list" aria-label="key 與別名">
        <li class="lv-alias">
          <span class="lv-alias__tag">主 KEY</span>
          <span class="lv-mono">{vault.key}</span>
        </li>
        {vault.aliases.map((a) => (
          <li class="lv-alias" key={a}>
            <span class="lv-alias__tag">別名</span>
            <span class="lv-mono">
              {a} <span class="lv-muted">→ 本 vault</span>
            </span>
            <button
              type="button"
              class="btn-outline btn-outline--sm lv-btn-danger"
              aria-label={`移除別名 ${a}`}
              disabled={busy !== null}
              onClick={() => void call('/v1/vault_alias_remove', a, `已移除別名 ${a}`)}
            >
              {busy === a ? '移除中…' : '移除'}
            </button>
          </li>
        ))}
        {vault.aliases.length === 0 && <li class="lv-muted lv-small">尚無別名。</li>}
      </ul>
      <form class="lv-inline-form" onSubmit={(e) => void add(e)}>
        <input
          class="lv-input"
          aria-label="新別名"
          placeholder={prefix ? `${prefix}新名稱` : '新 key，例如 github.com/org/new-name'}
          value={alias}
          onInput={(e) => setAlias((e.target as HTMLInputElement).value)}
        />
        <button type="submit" class="btn-outline btn-outline--sm btn-outline--gold" disabled={busy !== null || !alias.trim() || alias.trim() === prefix}>
          導向此 vault
        </button>
      </form>
      {error !== null && (
        <p class="lv-notice lv-notice--error" role="alert">
          {describeError(error)}
        </p>
      )}
    </section>
  );
}

// ── 換 space ──

/** A20：只允許 lore↔personal；dev 不與其他 space 轉換。 */
export function moveAllowed(from: SpaceId, to: SpaceId): boolean {
  return from !== to && from !== 'dev' && to !== 'dev';
}

function MoveSection({ vault }: { vault: VaultSummary }) {
  const { space, toast, refreshVaults, switchSpace, navigate } = useApp();
  const [target, setTarget] = useState<SpaceId | null>(null);
  const [moved, setMoved] = useState<{ to: SpaceId; key: string } | null>(null);
  const targets = SPACE_IDS.filter((id) => id !== space.id);

  const done = (result: TwoPhaseResponse) => {
    const to = target!;
    const newKey = result.vault?.key ?? String(result.plan.new_key ?? '');
    setTarget(null);
    setMoved({ to, key: newKey });
    refreshVaults();
    toast(`已移到 ${SPACES[to].en}：${newKey}`, 'success');
  };

  if (moved) {
    return (
      <section class="lv-section" aria-labelledby="maint-move">
        <h2 class="lv-section__title" id="maint-move">
          移到其他 space
        </h2>
        <div class="zone-state" role="status" data-testid="move-done">
          已移到 {SPACES[moved.to].en} · {SPACES[moved.to].name}，新 key <span class="lv-mono">{moved.key}</span>。在 {space.en} 已檢索不到。
        </div>
        <div class="lv-actions">
          <button
            type="button"
            class="btn-outline btn-outline--gold"
            onClick={() => switchSpace(moved.to, routePath('maint', [moved.key]))}
          >
            切換到 {SPACES[moved.to].en} 並開啟
          </button>
          <button type="button" class="btn-outline" onClick={() => navigate(routePath('vaults'))}>
            回 vault 列表
          </button>
        </div>
      </section>
    );
  }

  return (
    <section class="lv-section" aria-labelledby="maint-move">
      <h2 class="lv-section__title" id="maint-move">
        移到其他 space
      </h2>
      <p class="lv-section__desc">
        筆記、文件與別名一起搬移，key 與別名換成新 space 的前綴（舊 key 不保留為別名）。之後在原 space 檢索不到。
      </p>
      <div class="lv-move-targets">
        {targets.map((id) => {
          const allowed = moveAllowed(space.id, id);
          const meta = SPACES[id];
          return (
            <button
              key={id}
              type="button"
              class="lv-move-target"
              data-zone={meta.zone}
              disabled={!allowed}
              aria-describedby={allowed ? undefined : 'maint-move-a20'}
              onClick={() => setTarget(id)}
            >
              <span class="lv-space__glyph" aria-hidden="true">
                {meta.glyph}
              </span>
              移到 {meta.en} · {meta.name}
            </button>
          );
        })}
      </div>
      {targets.some((id) => !moveAllowed(space.id, id)) && (
        <p class="lv-hint lv-hint--inline" id="maint-move-a20" data-testid="move-a20">
          {space.id === 'dev'
            ? 'dev 的 vault 不能移到其他 space（A20）：dev 綁 repo 與收料、記憶層只屬於 dev，與 lore／personal 不互相轉換。'
            : '不能移到 dev（A20）：dev 綁 repo 與收料，只允許 lore 與 personal 互換。'}
        </p>
      )}
      {target && (
        <TwoPhaseConfirm
          title={`移到 ${SPACES[target].en} · ${SPACES[target].name}`}
          path="/v1/vault_move_space"
          args={{ space: space.id, key: vault.key, to_space: target }}
          confirmLabel="確認搬移"
          describe={
            <>
              把「{vault.display}」從 {space.en} 移到 {SPACES[target].en}。key 與別名改成新前綴，舊 key 之後無法解析。
            </>
          }
          onCancel={() => setTarget(null)}
          onDone={done}
        />
      )}
    </section>
  );
}

// ── 刪除 vault ──

function DeleteSection({ vault }: { vault: VaultSummary }) {
  const { space, toast, refreshVaults, navigate } = useApp();
  const [open, setOpen] = useState(false);
  return (
    <section class="lv-section lv-section--danger" aria-labelledby="maint-delete">
      <h2 class="lv-section__title" id="maint-delete">
        刪除整個 vault
      </h2>
      <p class="lv-section__desc">
        {vault.note_count} 則筆記會轉為墓碑（保留內容快照，可在墓碑清單還原——但要先重建同 key 的 vault），
        {vault.document_count ?? '—'} 份文件與其抽取段落會移除。確認前會列出服務規劃的實際筆數。
      </p>
      <button type="button" class="btn-outline lv-btn-danger" onClick={() => setOpen(true)}>
        刪除這個 vault…
      </button>
      {open && (
        <TwoPhaseDelete
          title={`刪除 vault「${vault.display}」`}
          path="/v1/vault_delete"
          args={{ space: space.id, key: vault.key }}
          requireText={vault.key}
          describe={<>這會刪除 {space.en} / {vault.key} 的全部內容。</>}
          onCancel={() => setOpen(false)}
          onDone={() => {
            setOpen(false);
            toast(`已刪除 vault ${vault.key}`, 'success');
            refreshVaults();
            navigate(routePath('maint'));
          }}
        />
      )}
    </section>
  );
}

// ── 墓碑 ──

type RestoreOutcome = { tone: 'ok' | 'warn' | 'error'; text: string; link?: string };

export function TombstoneSection({ vault }: { vault: string }) {
  const { api, space, navigate, refreshVaults } = useApp();
  const [items, setItems] = useState<TombstoneItem[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [outcomes, setOutcomes] = useState<Record<string, RestoreOutcome>>({});

  const load = async (cursor: string | null, signal?: AbortSignal) => {
    const { data } = await api.post<TombstonePage>(
      '/v1/tombstones',
      { space: space.id, vault, limit: 50, ...(cursor ? { cursor } : {}) },
      signal,
    );
    return data;
  };

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    load(null, ctrl.signal)
      .then((data) => {
        setItems(data.items);
        setNext(data.next_cursor);
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, space.id, vault, tick]);

  const more = async () => {
    if (!next) return;
    setLoading(true);
    try {
      const data = await load(next);
      setItems((prev) => [...prev, ...data.items]);
      setNext(data.next_cursor);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  };

  const restore = async (t: TombstoneItem) => {
    const key = `${t.kind}:${t.id}`;
    setBusy(key);
    try {
      let outcome: RestoreOutcome;
      if (t.kind === 'document') {
        const { data } = await api.post<DocumentUndeleteResult>('/v1/document_undelete', { space: space.id, id: t.id });
        outcome = {
          tone: 'ok',
          text: `已重建文件「${data.document.filename}」並重新排入抽取（${data.document.status}）。`,
          link: routePath('docs', [data.document.id]),
        };
      } else {
        const { data } = await api.post<NoteUndeleteResult>('/v1/note_undelete', { space: space.id, id: t.id });
        outcome = data.restored
          ? { tone: 'ok', text: `已完整還原「${data.note?.title ?? t.title ?? t.id}」（原 id 與內容）。`, link: routePath('notes', [t.id]) }
          : {
              tone: 'warn',
              text: data.reimportable
                ? '已移除墓碑，但這是舊版墓碑（沒有內容快照），內容未還原；下次重跑匯入時才會匯回。'
                : '已移除墓碑，但這是舊版墓碑（沒有內容快照），內容無法還原。',
            };
      }
      setOutcomes((o) => ({ ...o, [key]: outcome }));
      // 墓碑已不在：從清單移除，但保留結果訊息
      setItems((prev) => prev.filter((x) => `${x.kind}:${x.id}` !== key));
      refreshVaults();
    } catch (err) {
      setOutcomes((o) => ({ ...o, [key]: { tone: 'error', text: describeError(err) } }));
    } finally {
      setBusy(null);
    }
  };

  const resultEntries = Object.entries(outcomes).filter(([key]) => !items.some((x) => `${x.kind}:${x.id}` === key));
  const noteCount = items.filter((t) => t.kind !== 'document').length;
  const docCount = items.length - noteCount;

  return (
    <section class="lv-section" aria-labelledby="maint-graves">
      <div class="lv-section__head">
        <h2 class="lv-section__title" id="maint-graves">
          墓碑{vault === '*' ? `（${space.en} 全部）` : ''}
        </h2>
        {items.length > 0 && (
          <span class="lv-section__stat">
            {noteCount} 筆記 · {docCount} 文件{next ? '（還有更多）' : ''}
          </span>
        )}
        <span class="lv-spacer" />
        <button type="button" class="btn-outline btn-outline--sm" onClick={() => setTick((t) => t + 1)}>
          重新整理
        </button>
      </div>
      <p class="lv-section__desc">
        已刪除的筆記與文件留下紀錄，重新匯入時不會復活。v12 起刪除的筆記保留內容快照可完整還原；舊墓碑只能移除墓碑、內容靠重新匯入。
      </p>
      {resultEntries.length > 0 && (
        <ul class="lv-stack lv-restore-results" data-testid="restore-results">
          {resultEntries.map(([key, r]) => (
            <li
              key={key}
              class={'lv-notice ' + (r.tone === 'error' ? 'lv-notice--error' : r.tone === 'warn' ? 'lv-notice--warn' : 'lv-notice--ok')}
              role={r.tone === 'error' ? 'alert' : 'status'}
              data-tone={r.tone}
            >
              {r.text}
              {r.link && (
                <button type="button" class="lv-link-btn" onClick={() => navigate(r.link!)}>
                  開啟
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {error !== null && <ErrorState error={error} onRetry={() => setTick((t) => t + 1)} />}
      {loading && items.length === 0 && <Loading />}
      {!loading && error === null && items.length === 0 && (
        <div class="zone-state lv-empty lv-graves__empty">沒有墓碑：{vault === '*' ? `${space.en} space` : '這個 vault'} 目前沒有已刪除的筆記或文件。</div>
      )}
      {items.length > 0 && (
        <ul class="lv-graves" data-testid="tombstones">
          {items.map((t) => {
            const key = `${t.kind}:${t.id}`;
            const outcome = outcomes[key];
            const name = t.kind === 'document' ? t.filename : t.title;
            const blocked = !t.vault_exists;
            const oldNote = t.kind === 'note' && t.restorable === false && t.vault_exists;
            const state = blocked ? 'blocked' : t.restorable ? 'ok' : 'old';
            return (
              <li class="lv-grave" key={key} data-kind={t.kind} data-restorable={String(t.restorable ?? false)} data-state={state}>
                <span class="lv-grave__kind">
                  <Badge tone={t.kind === 'document' ? 'kind' : 'tag'} label="種類">
                    {t.kind === 'document' ? '文件' : '筆記'}
                  </Badge>
                </span>
                <div class="lv-grave__main">
                  <div class="lv-grave__title">{name ?? <span class="lv-muted">（舊墓碑，沒有標題）</span>}</div>
                  <div class="lv-grave__meta">
                    <Badge tone={blocked ? 'warn' : 'vault'} label="vault">
                      {t.vault}
                      {blocked ? ' · vault 已刪除' : ''}
                    </Badge>
                    <Badge tone="time" label="刪除於">
                      刪除於 {formatTime(t.deleted_at)}
                    </Badge>
                    {t.reason && (
                      <Badge tone="plain" label="原因">
                        {t.reason}
                      </Badge>
                    )}
                    <Badge tone="anchor" label="id">
                      {t.id}
                    </Badge>
                  </div>
                  <div class="lv-grave__state lv-small">
                    {blocked
                      ? '所屬 vault 已刪除：重建同 key 的 vault 後才能還原。'
                      : t.restorable
                        ? t.kind === 'note'
                          ? '有內容快照，可完整還原。'
                          : '原始檔仍在，可重建並重新抽取。'
                        : oldNote
                          ? t.reimportable
                            ? '舊墓碑（無內容快照）：只能移除墓碑，內容靠重新匯入。'
                            : '舊墓碑（無內容快照、無匯入來源）：移除墓碑後內容也不會回來。'
                          : '服務判定目前無法還原（舊墓碑、原始檔不在或已有同內容文件），可嘗試以查看原因。'}
                  </div>
                  {outcome && outcome.tone === 'error' && (
                    <p class="lv-notice lv-notice--error" role="alert">
                      {outcome.text}
                    </p>
                  )}
                </div>
                <button
                  type="button"
                  class="btn-outline btn-outline--sm"
                  disabled={blocked || busy !== null}
                  aria-label={`${oldNote ? '移除墓碑' : '還原'} ${name ?? t.id}`}
                  onClick={() => void restore(t)}
                >
                  {busy === key ? '處理中…' : oldNote ? '移除墓碑' : t.restorable ? '還原' : '嘗試還原'}
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {next && (
        <button type="button" class="btn-outline btn-outline--sm" disabled={loading} onClick={() => void more()}>
          載入更多墓碑
        </button>
      )}
    </section>
  );
}
