// 新增筆記（T-80）：選 vault → 標題／標籤／正文 → 寫入前查重（`write` 的 `dry_run: true`）→ 寫入。
// 查重預覽與正式寫入是同一個函式（驗證、範圍、supersedes 檢查、連結解析都相同），只在寫入前停下。
// 預覽發現疑似重複、查重降級（只做了關鍵字比對）或 [[ ]] 解析不到時，旁邊的面板照實列出，讓使用者
// 開啟既有筆記改寫，或「照樣新增」；草稿在預覽後有任何變動，預覽即失效、要重新查重。
import { useEffect, useRef, useState } from 'preact/hooks';

import { Banner } from '../components/ui';
import { ALL, useApp } from '../lib/context';
import { describeDegradedReason, describeError, describeUnresolvedLink, formatTime } from '../lib/format';
import { routePath } from '../lib/router';
import type { GetResult, NoteFull, WriteResult } from '../lib/types';
import { AuthorLine, parseTopics } from './NoteDetail';

const REASON_LABEL: Record<string, string> = { title: '標題相同', lexical: '字詞相近', vector: '語意相近' };

interface Payload {
  space: string;
  vault: string;
  title: string;
  body: string;
  topics: string[];
  supersedes?: string;
  author: string;
}

/** 查重預覽綁定的草稿：比對用的指紋（草稿變了預覽就失效） */
function fingerprint(p: Payload): string {
  return JSON.stringify([p.vault, p.title, p.body, p.topics, p.supersedes ?? null]);
}

function needsDecision(r: WriteResult): boolean {
  return r.duplicates.length > 0 || r.dedup_degraded || (r.unresolved_links?.length ?? 0) > 0;
}

export function NoteNew({ supersedes }: { supersedes: string | null }) {
  const { api, space, vault: filterVault, vaults, navigate, toast, refreshVaults, author } = useApp();
  const [vault, setVault] = useState(filterVault !== ALL ? filterVault : '');
  const [title, setTitle] = useState('');
  const [topics, setTopics] = useState('');
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState<'check' | 'write' | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [preview, setPreview] = useState<{ result: WriteResult; key: string } | null>(null);
  const [oldNote, setOldNote] = useState<NoteFull | null>(null);
  const [oldMissing, setOldMissing] = useState(false);
  // 查重結果出現時「寫入」會停用，焦點移到面板，鍵盤使用者不會掉到頁首
  const panel = useRef<HTMLElement>(null);
  const previewKey = preview?.key ?? null;
  useEffect(() => {
    if (previewKey) panel.current?.focus();
  }, [previewKey]);

  // 寫更正：預先帶出被取代的筆記（vault 必須相同）；只要 metadata
  useEffect(() => {
    if (!supersedes) return;
    const ctrl = new AbortController();
    api
      .post<GetResult<NoteFull>>('/v1/get', { space: space.id, vault: '*', ids: [supersedes], fields: 'meta' }, ctrl.signal)
      .then(({ data }) => {
        const found = data.items.find((n) => n.id === supersedes) ?? null;
        setOldNote(found);
        setOldMissing(!found);
        if (found) {
          setVault(found.vault);
          setTitle((t) => t || found.title);
          setTopics((t) => t || found.topics.join(', '));
        }
      })
      .catch((err) => {
        if (ctrl.signal.aborted) return;
        setError(err);
      });
    return () => ctrl.abort();
  }, [api, space.id, supersedes]);

  const payload = (): Payload => ({
    space: space.id,
    vault,
    title: title.trim(),
    body,
    topics: parseTopics(topics),
    ...(supersedes && oldNote ? { supersedes } : {}),
    author,
  });

  const current = payload();
  const fresh = preview !== null && preview.key === fingerprint(current);

  const write = async (p: Payload) => {
    setBusy('write');
    setError(null);
    try {
      const { data } = await api.post<WriteResult>('/v1/write', p);
      refreshVaults();
      const unresolved = data.unresolved_links ?? [];
      if (unresolved.length > 0) {
        toast(`已新增，但有 ${unresolved.length} 個 [[ ]] 沒有解析成互連：${unresolved.map((u) => u.target).join('、')}`, 'warning');
      } else {
        toast(`已新增至 ${space.en} / ${data.vault}`, 'success');
      }
      navigate(routePath('notes', [data.id ?? '']));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  };

  const submit = async (e: Event) => {
    e.preventDefault();
    const p = payload();
    // 預覽仍對應目前草稿：要在查重結果面板明確選「照樣新增」或改寫既有，Enter 不會直接寫入
    if (!p.vault || !p.title || busy || fresh) return;
    setBusy('check');
    setError(null);
    setPreview(null);
    try {
      const { data } = await api.post<WriteResult>('/v1/write', { ...p, dry_run: true });
      if (!needsDecision(data)) {
        await write(p);
        return;
      }
      setPreview({ result: data, key: fingerprint(p) });
    } catch (err) {
      setError(err);
    } finally {
      setBusy((b) => (b === 'check' ? null : b));
    }
  };

  const result = preview?.result ?? null;
  const unresolved = result?.unresolved_links ?? [];

  return (
    <section class="lv-screen lv-screen--wide" aria-labelledby="lv-new-title">
      <div class="lv-new">
        <div class="lv-new__main">
          <div class="lv-eyebrow">NEW NOTE{supersedes ? ' · 更正' : ''}</div>
          <h1 id="lv-new-title" class="lv-title">
            {supersedes ? '寫一則更正' : '新增筆記'}
          </h1>
          <form class="lv-editor" onSubmit={submit} aria-label="新增筆記">
            <div class="lv-write-to">
              <span class="lv-space__glyph lv-space__glyph--lg" aria-hidden="true">
                {space.glyph}
              </span>
              <label class="lv-write-to__field">
                <span class="lv-field__label">將寫入 {space.en} · {space.name} /</span>
                <select
                  class="lv-input lv-select"
                  value={vault}
                  required
                  disabled={Boolean(supersedes && oldNote)}
                  onChange={(e) => setVault((e.target as HTMLSelectElement).value)}
                >
                  <option value="">— 選擇 vault —</option>
                  {vaults.items.map((v) => (
                    <option key={v.key} value={v.key}>
                      {v.display}（{v.key}）
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {vaults.items.length === 0 && !vaults.loading && (
              <p class="lv-notice lv-notice--warn" role="status">
                這個 space 沒有 vault，無法寫入。請先到 Vault 畫面建立 vault。
              </p>
            )}
            {supersedes && oldNote && (
              <p class="lv-notice" role="status">
                這則會取代「{oldNote.title}」（{formatTime(oldNote.updated)}）。
              </p>
            )}
            {supersedes && oldMissing && (
              <p class="lv-notice lv-notice--error" role="alert" data-testid="supersedes-missing">
                找不到要被取代的筆記 {supersedes}（可能已刪除或屬於其他 space），這則會以一般新筆記寫入。
              </p>
            )}
            <label class="lv-field">
              <span class="lv-field__label">標題</span>
              <input class="lv-input lv-input--title" required value={title} onInput={(e) => setTitle((e.target as HTMLInputElement).value)} />
            </label>
            <label class="lv-field">
              <span class="lv-field__label">標籤（逗號分隔）</span>
              <input class="lv-input" value={topics} onInput={(e) => setTopics((e.target as HTMLInputElement).value)} />
            </label>
            <label class="lv-field">
              <span class="lv-field__label lv-field__label--split">
                <span>MARKDOWN</span>
                <span>[[標題]] 建立互連</span>
              </span>
              <textarea class="lv-textarea" aria-label="正文（Markdown）" value={body} onInput={(e) => setBody((e.target as HTMLTextAreaElement).value)} />
            </label>
            <p class="lv-hint lv-hint--inline">摘要會在儲存後於背景產生；產生前以首段頂替。寫入前會先查重，發現疑似重複時讓你決定。</p>
            <AuthorLine />
            {error !== null && (
              <p class="lv-notice lv-notice--error" role="alert">
                {describeError(error)}
              </p>
            )}
            {fresh && (
              <p class="lv-hint lv-hint--inline" role="status">
                查重有結果：請在查重面板選擇改寫既有筆記，或「照樣新增」。
              </p>
            )}
            {preview && !fresh && (
              <p class="lv-notice lv-notice--warn" role="status" data-testid="preview-stale">
                內容在查重後有變動，按「寫入」會重新查重。
              </p>
            )}
            <div class="lv-actions">
              <button type="submit" class="btn-outline btn-outline--gold" disabled={busy !== null || fresh || !vault || !title.trim()}>
                {busy === 'check' ? '查重中…' : busy === 'write' ? '寫入中…' : '寫入'}
              </button>
              <button type="button" class="btn-outline" onClick={() => navigate(routePath('notes'))}>
                取消
              </button>
            </div>
          </form>
        </div>

        {result && (
          <aside class="lv-new__side" aria-label="寫入前查重結果" data-testid="dedup-preview" ref={panel} tabIndex={-1}>
            <div class="lv-dupes" role="status">
              <div class="lv-dupes__head">
                <div class="lv-dupes__label">
                  {result.duplicates.length > 0 ? `疑似重複 · ${result.duplicates.length}` : '寫入前檢查'}
                  {!fresh && ' · 已過期'}
                </div>
                <div class="lv-dupes__text">
                  {result.duplicates.length > 0 ? '改寫既有筆記，或照樣新增。' : '沒有找到疑似重複的筆記。'}（尚未寫入）
                </div>
              </div>
              {result.dedup_degraded && (
                <div class="lv-dupes__section">
                  <Banner tone="warn" label="DEDUP DEGRADED" testId="dedup-degraded">
                    查重只做了關鍵字比對（{describeDegradedReason(result.dedup_reason)}）：語意相近但用詞不同的筆記可能沒被找出來。
                  </Banner>
                </div>
              )}
              {unresolved.length > 0 && (
                <div class="lv-dupes__section" data-testid="preview-unresolved">
                  <div class="lv-dupes__label">未解析的互連 · {unresolved.length}</div>
                  <ul class="lv-plain-list">
                    {unresolved.map((u) => (
                      <li key={u.target}>{describeUnresolvedLink(u)}</li>
                    ))}
                  </ul>
                  <div class="lv-dupes__meta">原文會保留在正文，但不會建立互連。</div>
                </div>
              )}
              {result.duplicates.length > 0 && (
                <ul data-testid="duplicates">
                  {result.duplicates.map((d) => (
                    <li key={d.id} class="lv-dupes__item">
                      <div class="lv-dupes__row">
                        <span class="lv-dupes__title">{d.title}</span>
                        <span class="lv-dupes__score">
                          字詞 {d.lexical.toFixed(2)}
                          {d.vector !== null ? ` · 語意 ${d.vector.toFixed(2)}` : ' · 語意 —'}
                        </span>
                      </div>
                      <div class="lv-dupes__meta">
                        {d.reasons.map((r) => REASON_LABEL[r] ?? r).join('、')} · 更新 {formatTime(d.updated)}
                      </div>
                      <button
                        type="button"
                        class="lv-link-btn"
                        aria-label={`改寫「${d.title}」`}
                        onClick={() => navigate(routePath('notes', [d.id]))}
                      >
                        改寫這則 →
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <div class="lv-dupes__foot">
                <button
                  type="button"
                  class="btn-outline"
                  disabled={busy !== null || !fresh}
                  onClick={() => void write(current)}
                >
                  {busy === 'write' ? '寫入中…' : '照樣新增'}
                </button>
              </div>
            </div>
          </aside>
        )}
      </div>
    </section>
  );
}
