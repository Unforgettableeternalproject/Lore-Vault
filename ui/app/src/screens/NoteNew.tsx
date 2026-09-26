// 新增筆記（T-80）：選 vault → 標題／標籤／正文 → write。
// 服務端 write 一律先寫入、再回傳疑似重複清單（無法在寫入前預覽），所以寫入後誠實顯示：
// 「已寫入，發現 N 則疑似重複」，讓使用者開啟既有筆記改寫，或刪除剛寫入的這則（兩段式）。
import { useEffect, useState } from 'preact/hooks';

import { Banner, TwoPhaseDelete } from '../components/ui';
import { ALL, useApp } from '../lib/context';
import { describeDegradedReason, describeError, formatTime } from '../lib/format';
import { loadSendAuthor, saveSendAuthor, UI_AUTHOR } from '../lib/prefs';
import { routePath } from '../lib/router';
import type { GetResult, NoteFull, WriteResult } from '../lib/types';
import { AuthorToggle, parseTopics } from './NoteDetail';

const REASON_LABEL: Record<string, string> = { title: '標題相同', lexical: '字詞相近', vector: '語意相近' };

export function NoteNew({ supersedes }: { supersedes: string | null }) {
  const { api, space, vault: filterVault, vaults, navigate, toast, refreshVaults } = useApp();
  const [vault, setVault] = useState(filterVault !== ALL ? filterVault : '');
  const [title, setTitle] = useState('');
  const [topics, setTopics] = useState('');
  const [body, setBody] = useState('');
  const [sendAuthor, setSendAuthor] = useState(loadSendAuthor);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [written, setWritten] = useState<{ result: WriteResult; title: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [oldNote, setOldNote] = useState<NoteFull | null>(null);
  const [oldMissing, setOldMissing] = useState(false);

  // 寫更正：預先帶出被取代的筆記（vault 必須相同）
  useEffect(() => {
    if (!supersedes) return;
    const ctrl = new AbortController();
    api
      .post<GetResult<NoteFull>>('/v1/get', { space: space.id, vault: '*', ids: [supersedes], budget: 1 }, ctrl.signal)
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

  const submit = async (e: Event) => {
    e.preventDefault();
    if (!vault || !title.trim() || saving) return;
    setSaving(true);
    setError(null);
    try {
      const { data } = await api.post<WriteResult>('/v1/write', {
        space: space.id,
        vault,
        title: title.trim(),
        body,
        topics: parseTopics(topics),
        ...(supersedes && oldNote ? { supersedes } : {}),
        ...(sendAuthor ? { author: UI_AUTHOR } : {}),
      });
      refreshVaults();
      if (data.duplicates.length === 0 && !data.dedup_degraded) {
        toast(`已新增至 ${space.en} / ${data.vault}`, 'success');
        navigate(routePath('notes', [data.id]));
        return;
      }
      setWritten({ result: data, title: title.trim() });
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  };

  if (written) {
    const { result } = written;
    return (
      <section class="lv-screen" aria-labelledby="lv-new-title">
        <div class="lv-eyebrow">NEW NOTE · 已寫入</div>
        <h1 id="lv-new-title" class="lv-title">
          已寫入「{written.title}」
        </h1>
        {result.dedup_degraded && (
          <Banner tone="warn" label="DEDUP DEGRADED" testId="dedup-degraded">
            查重只做了關鍵字比對（{describeDegradedReason(result.dedup_reason)}）：語意相近但用詞不同的筆記可能沒被找出來。
          </Banner>
        )}
        {result.duplicates.length > 0 ? (
          <div class="lv-dupes" data-testid="duplicates">
            <div class="lv-dupes__head">
              <div class="lv-dupes__label">疑似重複 · {result.duplicates.length}</div>
              <div class="lv-dupes__text">
                服務會先寫入再查重，這則已經存在。若它和既有筆記重複，可以改寫既有筆記後刪除這則。
              </div>
            </div>
            <ul>
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
                  <button type="button" class="lv-link-btn" onClick={() => navigate(routePath('notes', [d.id]))}>
                    開啟這則改寫 →
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p class="lv-muted">沒有找到疑似重複的筆記。</p>
        )}
        <div class="lv-actions lv-actions--wrap">
          <button type="button" class="btn-outline btn-outline--gold" onClick={() => navigate(routePath('notes', [result.id]))}>
            保留並開啟新筆記
          </button>
          <button type="button" class="btn-outline lv-btn-danger" onClick={() => setDeleting(true)}>
            刪除剛寫入的這則…
          </button>
        </div>
        {deleting && (
          <TwoPhaseDelete
            title="刪除剛寫入的筆記"
            path="/v1/note_delete"
            args={{ space: space.id, vault: result.vault, id: result.id }}
            describe={<>刪除「{written.title}」（剛寫入）。會留下墓碑，內容不會保留。</>}
            onCancel={() => setDeleting(false)}
            onDone={() => {
              setDeleting(false);
              toast(`已刪除「${written.title}」`, 'success');
              refreshVaults();
              navigate(routePath('notes'));
            }}
          />
        )}
      </section>
    );
  }

  return (
    <section class="lv-screen" aria-labelledby="lv-new-title">
      <div class="lv-eyebrow">NEW NOTE{supersedes ? ' · 更正' : ''}</div>
      <h1 id="lv-new-title" class="lv-title">
        {supersedes ? '寫一則更正' : '新增筆記'}
      </h1>
      <form class="lv-editor" onSubmit={submit}>
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
            這個 space 沒有 vault，無法寫入。請先建立 vault（Vault 畫面，T-82）。
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
        <p class="lv-hint lv-hint--inline">摘要會在儲存後於背景產生；產生前以首段頂替。寫入後會回報疑似重複的既有筆記。</p>
        <AuthorToggle
          on={sendAuthor}
          onChange={(on) => {
            setSendAuthor(on);
            saveSendAuthor(on);
          }}
        />
        {error !== null && (
          <p class="lv-notice lv-notice--error" role="alert">
            {describeError(error)}
          </p>
        )}
        <div class="lv-actions">
          <button type="submit" class="btn-outline btn-outline--gold" disabled={saving || !vault || !title.trim()}>
            {saving ? '寫入中…' : '寫入'}
          </button>
          <button type="button" class="btn-outline" onClick={() => navigate(routePath('notes'))}>
            取消
          </button>
        </div>
      </form>
    </section>
  );
}
