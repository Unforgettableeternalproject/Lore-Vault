// 筆記詳情（T-80）：讀（Markdown、摘要來源、更正鏈、互連）／編輯／版本衝突／兩段式刪除。
// 版本衝突：update 回 409 → 取目前版本全文 → 並排差異；合併＝以目前版本為底產生含衝突標記的草稿，
// 覆寫＝二次確認後以目前版本的 updated 重送，放棄＝不重送、顯示目前版本。
import { useEffect, useState } from 'preact/hooks';

import { Banner, Dialog, ErrorState, Loading, SourceTag, TwoPhaseDelete } from '../components/ui';
import { ApiError } from '../lib/api';
import { useApp } from '../lib/context';
import { diffLines, hasConflictMarkers, mergeDraft } from '../lib/diff';
import { authorLabel, describeError, formatTime, isAbort } from '../lib/format';
import { Markdown } from '../lib/markdown';
import { routePath } from '../lib/router';
import type { ConflictCurrent, GetResult, NoteFull, NoteUndeleteResult, UpdateResult } from '../lib/types';

/** 詳情頁一次取全文的字數預算；超過仍會標 truncated 並提供「載入全文」 */
export const DETAIL_BUDGET = 200_000;
const MAX_LINK_IDS = 50;

interface Draft {
  title: string;
  topics: string;
  body: string;
}

type Mode = 'read' | 'edit' | 'conflict';

interface LinkInfo {
  titles: Record<string, string>;
  missing: string[];
}

function toDraft(note: NoteFull): Draft {
  return { title: note.title, topics: note.topics.join(', '), body: note.body };
}

export function parseTopics(text: string): string[] {
  return [...new Set(text.split(/[,，\n]/).map((t) => t.trim().replace(/^#/, '')).filter(Boolean))];
}

function sameList(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

/** 草稿相對於 `against` 的變更欄位（只送有變的）。 */
function changes(draft: Draft, against: NoteFull): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (draft.title !== against.title) out.title = draft.title;
  if (draft.body !== against.body) out.body = draft.body;
  const topics = parseTopics(draft.topics);
  if (!sameList(topics, against.topics)) out.topics = topics;
  return out;
}

export function NoteDetail({ id }: { id: string }) {
  const { api, space, navigate, toast, refreshVaults, author } = useApp();
  const [note, setNote] = useState<NoteFull | null>(null);
  const [lookup, setLookup] = useState<{ missing: string[]; unavailable: string[] }>({ missing: [], unavailable: [] });
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [budget, setBudget] = useState(DETAIL_BUDGET);
  const [tick, setTick] = useState(0);
  const [links, setLinks] = useState<LinkInfo>({ titles: {}, missing: [] });

  const [mode, setMode] = useState<Mode>('read');
  const [base, setBase] = useState<NoteFull | null>(null);
  const [draft, setDraft] = useState<Draft>({ title: '', topics: '', body: '' });
  const [mergeNotice, setMergeNotice] = useState(false);
  const [conflict, setConflict] = useState<{ current: NoteFull; mine: Draft; meta: ConflictCurrent } | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [confirmOverwrite, setConfirmOverwrite] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleted, setDeleted] = useState<{ title: string } | null>(null);
  const [undeleting, setUndeleting] = useState(false);
  const [undeleteResult, setUndeleteResult] = useState<{ tone: 'ok' | 'warn' | 'error'; text: string } | null>(null);

  const fetchNote = async (signal?: AbortSignal, want = budget): Promise<GetResult<NoteFull>> => {
    const { data } = await api.post<GetResult<NoteFull>>(
      '/v1/get',
      { space: space.id, vault: '*', ids: [id], budget: want },
      signal,
    );
    return data;
  };

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    fetchNote(ctrl.signal)
      .then((data) => {
        setNote(data.items.find((n) => n.id === id && n.kind === 'note') ?? null);
        setLookup({ missing: data.missing, unavailable: data.unavailable });
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, space.id, id, budget, tick]);

  // 互連與更正對象的標題（只要標題：budget 取最小，正文不顯示，其截斷與此無關）
  useEffect(() => {
    if (!note) return;
    const ids = [...new Set([...note.links, ...(note.supersedes ? [note.supersedes] : [])])].slice(0, MAX_LINK_IDS);
    if (ids.length === 0) {
      setLinks({ titles: {}, missing: [] });
      return;
    }
    const ctrl = new AbortController();
    api
      .post<GetResult<NoteFull>>('/v1/get', { space: space.id, vault: '*', ids, budget: 1 }, ctrl.signal)
      .then(({ data }) => {
        const titles: Record<string, string> = {};
        data.items.forEach((n) => (titles[n.id] = n.title));
        setLinks({ titles, missing: [...data.missing, ...data.unavailable] });
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        // 連結標題查不到不影響閱讀，但要看得出來
        setLinks({ titles: {}, missing: ids });
        toast(`互連標題載入失敗：${describeError(err)}`, 'warning');
      });
    return () => ctrl.abort();
  }, [api, space.id, note?.id, note?.updated]);

  const startEdit = () => {
    if (!note) return;
    setBase(note);
    setDraft(toDraft(note));
    setMergeNotice(false);
    setSaveError(null);
    setMode('edit');
  };

  const refetchCurrent = async (): Promise<NoteFull> => {
    const data = await fetchNote(undefined, DETAIL_BUDGET);
    const current = data.items.find((n) => n.id === id);
    if (!current) throw new ApiError(404, 'not_found', '這則筆記已不存在（可能已被刪除）');
    return current;
  };

  const send = async (payload: Record<string, unknown>, expected: string, vault: string) => {
    const { data } = await api.post<UpdateResult>('/v1/update', {
      space: space.id,
      vault,
      id,
      expected_updated: expected,
      ...payload,
      author,
    });
    return data;
  };

  const afterSaved = async (result: UpdateResult, message: string) => {
    toast(
      result.summary_stale ? `${message}；摘要將在背景重新產生，產生前以首段頂替` : message,
      'success',
    );
    const current = await refetchCurrent();
    setNote(current);
    setConflict(null);
    setMode('read');
  };

  const enterConflict = async (err: ApiError, mine: Draft) => {
    const meta = ((err.body as { error?: { current?: ConflictCurrent } } | null)?.error?.current ?? null) as ConflictCurrent | null;
    const current = await refetchCurrent();
    setConflict({ current, mine, meta: meta ?? current });
    setMode('conflict');
  };

  const save = async () => {
    if (!base) return;
    if (hasConflictMarkers(draft.body)) {
      setSaveError('正文仍有衝突標記（<<<<<<< ／ >>>>>>>），請整理後再儲存');
      return;
    }
    const payload = changes(draft, base);
    if (Object.keys(payload).length === 0) {
      setSaveError(new ApiError(400, 'no_changes', '沒有任何變更'));
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      await afterSaved(await send(payload, base.updated, base.vault), '已儲存');
    } catch (err) {
      if (err instanceof ApiError && err.code === 'version_conflict') {
        try {
          await enterConflict(err, draft);
        } catch (inner) {
          setSaveError(inner);
        }
      } else {
        setSaveError(err);
      }
    } finally {
      setSaving(false);
    }
  };

  const overwrite = async () => {
    if (!conflict) return;
    setConfirmOverwrite(false);
    const payload = changes(conflict.mine, conflict.current);
    if (Object.keys(payload).length === 0) {
      toast('你的版本與目前版本相同，不需覆寫', 'info');
      setNote(conflict.current);
      setConflict(null);
      setMode('read');
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      await afterSaved(await send(payload, conflict.current.updated, conflict.current.vault), '已以你的版本覆寫');
    } catch (err) {
      if (err instanceof ApiError && err.code === 'version_conflict') {
        try {
          await enterConflict(err, conflict.mine);
          toast('覆寫前又被更新了一次，請重新比對', 'warning');
        } catch (inner) {
          setSaveError(inner);
        }
      } else {
        setSaveError(err);
      }
    } finally {
      setSaving(false);
    }
  };

  const merge = () => {
    if (!conflict || !base) return;
    const { current, mine } = conflict;
    const baseTopics = base.topics.join(', ');
    setBase(current);
    setDraft({
      title: mine.title !== base.title ? mine.title : current.title,
      topics: mine.topics !== baseTopics ? mine.topics : current.topics.join(', '),
      body: current.body === mine.body ? mine.body : mergeDraft(current.body, mine.body),
    });
    setMergeNotice(true);
    setSaveError(null);
    setConflict(null);
    setMode('edit');
  };

  const discard = () => {
    if (!conflict) return;
    setNote(conflict.current);
    setConflict(null);
    setMode('read');
    toast('已放棄你的修改，顯示目前版本', 'info');
  };

  const undelete = async () => {
    setUndeleting(true);
    setUndeleteResult(null);
    try {
      const { data } = await api.post<NoteUndeleteResult>('/v1/note_undelete', { space: space.id, id });
      refreshVaults();
      if (data.restored) {
        toast(`已還原「${data.note?.title ?? deleted?.title ?? id}」`, 'success');
        setDeleted(null);
        setNote(null);
        setTick((t) => t + 1);
        return;
      }
      // 舊版墓碑沒有內容快照：只移除了墓碑
      setUndeleteResult({
        tone: 'warn',
        text: data.reimportable
          ? '已移除墓碑，但這則沒有內容快照（舊版刪除），內容未還原；下次重跑匯入時會匯回。'
          : '已移除墓碑，但這則沒有內容快照（舊版刪除），內容無法還原。',
      });
    } catch (err) {
      setUndeleteResult({ tone: 'error', text: describeError(err) });
    } finally {
      setUndeleting(false);
    }
  };

  if (deleted) {
    return (
      <section class="lv-screen">
        <div class="lv-eyebrow">NOTE · {space.en} · 已刪除</div>
        <h1 class="lv-title">已刪除「{deleted.title}」</h1>
        <div class="zone-state" role="status" data-testid="note-deleted">
          已留下墓碑。v12 起的刪除保留內容快照，可以原 id 與原內容還原。
        </div>
        {undeleteResult && (
          <p
            class={'lv-notice ' + (undeleteResult.tone === 'error' ? 'lv-notice--error' : 'lv-notice--warn')}
            role={undeleteResult.tone === 'error' ? 'alert' : 'status'}
            data-testid="undelete-result"
          >
            {undeleteResult.text}
          </p>
        )}
        <div class="lv-actions lv-actions--wrap">
          {!undeleteResult || undeleteResult.tone === 'error' ? (
            <button type="button" class="btn-outline btn-outline--gold" disabled={undeleting} onClick={() => void undelete()}>
              {undeleting ? '復原中…' : '復原這則筆記'}
            </button>
          ) : null}
          <button type="button" class="btn-outline" onClick={() => navigate(routePath('notes'))}>
            回筆記列表
          </button>
        </div>
      </section>
    );
  }

  if (loading && !note) return <Loading />;
  if (error !== null && !note) return <ErrorState error={error} onRetry={() => setTick((t) => t + 1)} />;
  if (!note) {
    return (
      <section class="lv-screen">
        <div class="lv-eyebrow">NOTE · {space.en}</div>
        <h1 class="lv-title">找不到筆記</h1>
        <div class="zone-state zone-state--error" role="alert" data-testid="note-missing">
          {lookup.unavailable.includes(id)
            ? `這個模式下無法讀取 ${id}（unavailable）。`
            : `在 ${space.en} space 找不到 ${id}：可能已被刪除，或屬於其他 space（missing）。`}
          <button type="button" onClick={() => navigate(routePath('notes'))}>
            回筆記列表
          </button>
        </div>
      </section>
    );
  }

  const renderWiki = (title: string, key: string) => {
    const target = Object.entries(links.titles).find(([, t]) => t === title)?.[0];
    if (target) {
      return (
        <a
          key={key}
          href={routePath('notes', [target])}
          class="lv-wikilink"
          onClick={(e) => {
            e.preventDefault();
            navigate(routePath('notes', [target]));
          }}
        >
          {title}
        </a>
      );
    }
    return (
      <button
        key={key}
        type="button"
        class="lv-wikilink lv-wikilink--unresolved"
        title="這個 [[標題]] 沒有解析成筆記連結；點擊以標題檢索"
        onClick={() => navigate(routePath('search', [], { q: title }))}
      >
        {title}
        <span class="lv-wikilink__tag">未解析</span>
      </button>
    );
  };

  return (
    <div class="lv-detail">
      <article class="lv-detail__main">
        <nav class="lv-crumb" aria-label="位置">
          <button type="button" class="lv-crumb__link" onClick={() => navigate(routePath('notes'))}>
            {space.en} / {note.vault}
          </button>
          <span aria-hidden="true">/</span>
          <span>NOTE</span>
        </nav>
        <h1 class="lv-note-title">{mode === 'read' ? note.title : draft.title || '（無標題）'}</h1>
        <div class="lv-meta-line">
          <span>
            寫入 <span data-testid="note-author">{authorLabel(note.author)}</span>
          </span>
          {note.updated_by && note.updated_by !== note.author && (
            <span>
              最後修改 <span data-testid="note-updated-by">{note.updated_by}</span>
            </span>
          )}
          <span>最後更新 {formatTime(note.updated)}</span>
          <span>建立 {formatTime(note.created)}</span>
          {note.topics.length > 0 && <span>{note.topics.map((t) => `#${t}`).join(' ')}</span>}
        </div>

        {mode === 'read' && (
          <>
            <div class="lv-summary-box" data-testid="note-summary">
              <div class="lv-summary-box__head">
                <SourceTag source={note.summary_source} />
                <span class="lv-muted lv-mono">
                  {note.summary_source === 'summary'
                    ? 'LLM 背景產生'
                    : note.summary_source === 'lead'
                      ? '摘要尚未產生，暫以正文首段頂替'
                      : note.summary_source === 'none'
                        ? '沒有摘要'
                        : ''}
                </span>
              </div>
              {note.summary && <div class="lv-summary-box__text">{note.summary}</div>}
            </div>

            {note.truncated && (
              <Banner
                tone="warn"
                label="TRUNCATED"
                testId="note-truncated"
                title={`正文已截斷：顯示 ${note.body.length.toLocaleString()} / ${note.body_chars.toLocaleString()} 字`}
                action={
                  <button type="button" class="btn-outline btn-outline--sm" onClick={() => setBudget(note.body_chars + 1000)}>
                    載入全文
                  </button>
                }
              >
                超過字數預算的部分沒有顯示；編輯前請先載入全文，避免把截斷後的內容存回去。
              </Banner>
            )}

            <div class="lv-prose">
              <Markdown source={note.body} options={{ renderWikiLink: renderWiki }} />
            </div>

            <div class="lv-detail__actions">
              <button type="button" class="btn-zone" onClick={startEdit} disabled={note.truncated} title={note.truncated ? '正文被截斷，先載入全文再編輯' : undefined}>
                編輯
              </button>
              <button type="button" class="btn-outline" onClick={() => navigate(routePath('notes', ['new'], { supersedes: note.id }))}>
                寫一則更正
              </button>
              <div class="lv-spacer" />
              <button type="button" class="btn-outline lv-btn-danger" onClick={() => setDeleting(true)}>
                刪除…
              </button>
            </div>
          </>
        )}

        {mode === 'edit' && base && (
          <div class="lv-editor">
            {mergeNotice && (
              <Banner tone="warn" label="MERGE" testId="merge-draft">
                這是以目前版本（{formatTime(base.updated)}）為底的合併草稿。不同之處以 <code>&lt;&lt;&lt;&lt;&lt;&lt;&lt; 目前版本</code>／
                <code>&gt;&gt;&gt;&gt;&gt;&gt;&gt; 我的修改</code> 標出，整理完移除標記才能儲存。
              </Banner>
            )}
            <label class="lv-field">
              <span class="lv-field__label">標題</span>
              <input class="lv-input lv-input--title" value={draft.title} onInput={(e) => setDraft({ ...draft, title: (e.target as HTMLInputElement).value })} />
            </label>
            <label class="lv-field">
              <span class="lv-field__label">標籤（逗號分隔）</span>
              <input class="lv-input" value={draft.topics} onInput={(e) => setDraft({ ...draft, topics: (e.target as HTMLInputElement).value })} />
            </label>
            <label class="lv-field">
              <span class="lv-field__label lv-field__label--split">
                <span>MARKDOWN · 基於 {formatTime(base.updated)} 的版本編輯</span>
                <span>[[標題]] 建立互連</span>
              </span>
              <textarea
                class="lv-textarea"
                aria-label="正文（Markdown）"
                value={draft.body}
                onInput={(e) => setDraft({ ...draft, body: (e.target as HTMLTextAreaElement).value })}
              />
            </label>
            <AuthorLine />
            {saveError !== null && (
              <p class="lv-notice lv-notice--error" role="alert">
                {describeError(saveError)}
              </p>
            )}
            <div class="lv-actions">
              <button type="button" class="btn-outline btn-outline--gold" disabled={saving} onClick={() => void save()}>
                {saving ? '儲存中…' : '儲存'}
              </button>
              <button type="button" class="btn-outline" disabled={saving} onClick={() => setMode('read')}>
                取消
              </button>
            </div>
          </div>
        )}

        {mode === 'conflict' && conflict && base && (
          <ConflictView
            base={base}
            current={conflict.current}
            mine={conflict.mine}
            saving={saving}
            error={saveError}
            onMerge={merge}
            onOverwrite={() => setConfirmOverwrite(true)}
            onDiscard={discard}
          />
        )}
      </article>

      <aside class="lv-detail__side">
        <div class="lv-side-block">
          <div class="lv-side-block__label">更正鏈</div>
          <div class="lv-chain">
            <div>
              <div class="lv-chain__k">被取代</div>
              <div class="lv-chain__v lv-muted">服務尚未提供此資訊</div>
            </div>
            <div>
              <div class="lv-chain__k lv-chain__k--here">此筆記</div>
              <div class="lv-chain__v">{note.title}</div>
            </div>
            <div>
              <div class="lv-chain__k">取代了</div>
              <div class="lv-chain__v">
                {note.supersedes ? (
                  links.titles[note.supersedes] ? (
                    <a
                      href={routePath('notes', [note.supersedes])}
                      class="lv-chain__old"
                      onClick={(e) => {
                        e.preventDefault();
                        navigate(routePath('notes', [note.supersedes!]));
                      }}
                    >
                      {links.titles[note.supersedes]}
                    </a>
                  ) : (
                    <span class="lv-muted">{note.supersedes}（找不到：可能已刪除）</span>
                  )
                ) : (
                  <span class="lv-muted">無</span>
                )}
              </div>
            </div>
          </div>
        </div>
        <div class="lv-side-block">
          <div class="lv-side-block__label">互連 · {note.links.length}</div>
          {note.links.length === 0 && <p class="lv-muted lv-small">沒有已解析的互連。</p>}
          <ul class="lv-side-list">
            {note.links.map((lid) =>
              links.titles[lid] ? (
                <li key={lid}>
                  <a
                    href={routePath('notes', [lid])}
                    onClick={(e) => {
                      e.preventDefault();
                      navigate(routePath('notes', [lid]));
                    }}
                  >
                    {links.titles[lid]}
                  </a>
                </li>
              ) : links.missing.includes(lid) ? (
                <li key={lid} class="lv-muted" data-testid="link-missing">
                  連結目標不存在：{lid}
                </li>
              ) : null,
            )}
          </ul>
        </div>
      </aside>

      {confirmOverwrite && conflict && (
        <Dialog
          title="以你的版本覆寫？"
          tone="danger"
          onClose={() => setConfirmOverwrite(false)}
          actions={
            <>
              <button type="button" class="uep-dialog__btn uep-dialog__btn--cancel" onClick={() => setConfirmOverwrite(false)}>
                取消
              </button>
              <button type="button" class="uep-dialog__btn lv-dialog__btn--danger" onClick={() => void overwrite()}>
                確認覆寫
              </button>
            </>
          }
        >
          目前版本（{formatTime(conflict.current.updated)}，{authorLabel(conflict.current.updated_by ?? conflict.current.author)}）的內容會被你的修改取代。
          這個動作不會保留目前版本的副本。
        </Dialog>
      )}

      {deleting && (
        <TwoPhaseDelete
          title="刪除筆記"
          path="/v1/note_delete"
          args={{ space: space.id, vault: note.vault, id: note.id }}
          describe={
            <>
              刪除「{note.title}」。會留下墓碑與內容快照（避免重新匯入時復活），刪除後可從這裡或維護頁復原。
            </>
          }
          onCancel={() => setDeleting(false)}
          onDone={() => {
            setDeleting(false);
            setMode('read');
            setDeleted({ title: note.title });
            toast(`已刪除「${note.title}」`, 'success');
            refreshVaults();
          }}
        />
      )}
    </div>
  );
}

/** 寫入署名說明：UI 寫入一律帶 author（A22），不提供關閉。 */
export function AuthorLine() {
  const { author } = useApp();
  return (
    <p class="lv-hint lv-hint--inline" data-testid="author-line">
      以 <span class="lv-mono">{author}</span> 署名寫入（A22：分清誰做了什麼）
    </p>
  );
}

function ConflictView({
  base,
  current,
  mine,
  saving,
  error,
  onMerge,
  onOverwrite,
  onDiscard,
}: {
  base: NoteFull;
  current: NoteFull;
  mine: Draft;
  saving: boolean;
  error: unknown;
  onMerge: () => void;
  onOverwrite: () => void;
  onDiscard: () => void;
}) {
  const ops = diffLines(current.body, mine.body);
  const titleChanged = current.title !== mine.title;
  const topicsChanged = !sameList(current.topics, parseTopics(mine.topics));
  return (
    <div class="lv-conflict" data-testid="version-conflict">
      <div class="lv-conflict__banner" role="alert">
        <div class="lv-conflict__label">VERSION CONFLICT · 未儲存</div>
        <div class="lv-conflict__title">
          你基於 {formatTime(base.updated)} 的版本編輯，但這則筆記已在 {formatTime(current.updated)} 被
          {authorLabel(current.updated_by ?? current.author)}更新。
        </div>
        <div class="lv-conflict__text">你的修改仍在這裡。選擇合併、以你的版本覆寫，或放棄修改。</div>
      </div>
      {(titleChanged || topicsChanged) && (
        <ul class="lv-conflict__fields">
          {titleChanged && (
            <li>
              標題：目前「{current.title}」／你的「{mine.title}」
            </li>
          )}
          {topicsChanged && (
            <li>
              標籤：目前 {current.topics.map((t) => `#${t}`).join(' ') || '（無）'}／你的{' '}
              {parseTopics(mine.topics).map((t) => `#${t}`).join(' ') || '（無）'}
            </li>
          )}
        </ul>
      )}
      <div class="lv-conflict__cols">
        <div class="lv-conflict__col">
          <div class="lv-conflict__col-head">
            <span>目前版本</span>
            <span>
              {authorLabel(current.updated_by ?? current.author)} · {formatTime(current.updated)}
            </span>
          </div>
          <pre class="lv-diff" aria-label="目前版本">
            {ops
              .filter((o) => o.type !== 'add')
              .map((o, i) => (
                <span key={i} class={o.type === 'del' ? 'lv-diff__del' : undefined}>
                  {o.line + '\n'}
                </span>
              ))}
          </pre>
        </div>
        <div class="lv-conflict__col lv-conflict__col--mine">
          <div class="lv-conflict__col-head">
            <span>你的修改</span>
            <span>基於 {formatTime(base.updated)}</span>
          </div>
          <pre class="lv-diff" aria-label="你的修改">
            {ops
              .filter((o) => o.type !== 'del')
              .map((o, i) => (
                <span key={i} class={o.type === 'add' ? 'lv-diff__add' : undefined}>
                  {o.line + '\n'}
                </span>
              ))}
          </pre>
        </div>
      </div>
      {error !== null && (
        <p class="lv-notice lv-notice--error" role="alert">
          {describeError(error)}
        </p>
      )}
      <div class="lv-actions lv-actions--wrap">
        <button type="button" class="btn-outline btn-outline--gold" disabled={saving} onClick={onMerge}>
          在目前版本上合併我的修改
        </button>
        <button type="button" class="btn-outline" disabled={saving} onClick={onOverwrite}>
          以我的版本覆寫
        </button>
        <button type="button" class="btn-outline" disabled={saving} onClick={onDiscard}>
          放棄我的修改
        </button>
      </div>
    </div>
  );
}
