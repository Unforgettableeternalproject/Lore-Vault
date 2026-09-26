/** @vitest-environment happy-dom */
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { MARK_START } from '../lib/diff';
import { UI_AUTHOR } from '../lib/prefs';
import type { NoteFull } from '../lib/types';
import { apiError, json, makeApi, renderWithApp, type Handler } from '../test/harness';
import { NoteDetail } from './NoteDetail';
import { NoteNew } from './NoteNew';

afterEach(cleanup);
beforeEach(() => window.localStorage.clear());

const VAULT = 'github.com/org/lore-vault';

function note(overrides: Partial<NoteFull> = {}): NoteFull {
  const body = overrides.body ?? '第一行\n第二行\n參考 [[相關筆記]] 與 [[不存在的標題]]';
  return {
    id: 'n1',
    kind: 'note',
    vault: VAULT,
    title: '注入預算',
    summary: '第一行',
    summary_source: 'lead',
    body,
    body_chars: body.length,
    truncated: false,
    topics: ['hook'],
    links: ['n2', 'gone'],
    supersedes: null,
    created: '2026-09-20T00:00:00.000Z',
    updated: '2026-09-26T01:00:00.000Z',
    ...overrides,
  };
}

const related = { ...note({ id: 'n2', title: '相關筆記', links: [] }) };

/** /v1/get：詳情（ids=[n1]）回 `current()`；互連標題（budget=1）回 n2、gone 缺 */
function getHandler(current: () => NoteFull): Handler {
  return (body) => {
    const ids = body.ids as string[];
    if (body.budget === 1) {
      return json({ items: ids.includes('n2') ? [related] : [], missing: ids.filter((i) => i !== 'n2'), unavailable: [], truncated: true, budget: 1, used_chars: 1 });
    }
    return json({ items: [current()], missing: [], unavailable: [], truncated: current().truncated, budget: 200000, used_chars: 10 });
  };
}

describe('閱讀', () => {
  it('摘要來源、作者未具名、互連已解析／未解析／目標不存在都看得到', async () => {
    const { api } = makeApi({ '/v1/get': getHandler(() => note()) });
    const { navigate } = renderWithApp(<NoteDetail id="n1" />, api);
    expect((await screen.findByTestId('note-summary')).textContent).toContain('首段');
    expect(screen.getByTestId('note-summary').textContent).toContain('摘要尚未產生');
    expect(screen.getByTestId('note-author').textContent).toBe('未具名');
    // 正文內的 [[相關筆記]] 與側欄互連各一
    const resolved = await screen.findAllByRole('link', { name: '相關筆記' });
    expect(resolved.map((a) => a.getAttribute('href'))).toEqual(['/ui/notes/n2', '/ui/notes/n2']);
    expect(resolved[0]!.classList.contains('lv-wikilink')).toBe(true);
    const unresolved = screen.getByRole('button', { name: /不存在的標題/ });
    expect(unresolved.textContent).toContain('未解析');
    fireEvent.click(unresolved);
    expect(navigate).toHaveBeenCalledWith('/ui/search?q=%E4%B8%8D%E5%AD%98%E5%9C%A8%E7%9A%84%E6%A8%99%E9%A1%8C');
    expect((await screen.findByTestId('link-missing')).textContent).toContain('gone');
  });

  it('有 author 時顯示作者；最後修改者不同時另外顯示', async () => {
    const { api } = makeApi({ '/v1/get': getHandler(() => note({ author: 'codex', updated_by: 'Minka', links: [] })) });
    renderWithApp(<NoteDetail id="n1" />, api);
    expect((await screen.findByTestId('note-author')).textContent).toBe('codex');
    expect(screen.getByTestId('note-updated-by').textContent).toBe('Minka');
  });

  it('正文截斷：顯示字數並可載入全文（以更大 budget 重取）；截斷時不能編輯', async () => {
    let truncated = true;
    const full = note({ links: [] });
    const { api, callsTo } = makeApi({
      '/v1/get': getHandler(() => (truncated ? { ...full, body: '第一行', truncated: true, body_chars: 5000 } : full)),
    });
    renderWithApp(<NoteDetail id="n1" />, api);
    const banner = await screen.findByTestId('note-truncated');
    expect(banner.textContent).toContain('3 / 5,000');
    expect((screen.getByRole('button', { name: '編輯' }) as HTMLButtonElement).disabled).toBe(true);
    truncated = false;
    fireEvent.click(within(banner).getByRole('button', { name: '載入全文' }));
    await waitFor(() => expect(screen.queryByTestId('note-truncated')).toBeNull());
    expect(callsTo('/v1/get').at(-1)!.body.budget).toBe(6000);
  });

  it('找不到：列為 missing 並說明', async () => {
    const { api } = makeApi({
      '/v1/get': () => json({ items: [], missing: ['n1'], unavailable: [], truncated: false, budget: 1, used_chars: 0 }),
    });
    renderWithApp(<NoteDetail id="n1" />, api);
    expect((await screen.findByTestId('note-missing')).textContent).toContain('missing');
  });
});

describe('編輯與版本衝突', () => {
  async function openEditor(handlers: Record<string, Handler>) {
    const ctx = makeApi(handlers);
    const rendered = renderWithApp(<NoteDetail id="n1" />, ctx.api);
    fireEvent.click(await screen.findByRole('button', { name: '編輯' }));
    const body = document.querySelector('textarea')!;
    fireEvent.input(body, { target: { value: '第一行\n我的第二行' } });
    return { ...ctx, ...rendered };
  }

  const theirs = note({ body: '第一行\n他們的第二行', updated: '2026-09-26T02:00:00.000Z', author: 'codex', links: [] });

  function conflictHandlers(afterOverwrite: () => void) {
    let current = note({ links: [] });
    let updates = 0;
    const handlers: Record<string, Handler> = {
      '/v1/get': getHandler(() => current),
      '/v1/update': (body) => {
        updates++;
        if (updates === 1) {
          current = theirs;
          return apiError(409, 'version_conflict', {
            expected: body.expected_updated,
            current: { id: 'n1', vault: VAULT, title: theirs.title, topics: theirs.topics, links: [], supersedes: null, created: theirs.created, updated: theirs.updated },
          });
        }
        afterOverwrite();
        current = { ...theirs, body: body.body as string, updated: '2026-09-26T03:00:00.000Z' };
        return json({ id: 'n1', vault: VAULT, updated: current.updated, summary_stale: true, embedding_stale: true });
      },
    };
    return handlers;
  }

  it('409 → 顯示兩邊差異；覆寫要二次確認，確認後以目前版本的 updated 重送', async () => {
    let overwritten = false;
    const { callsTo, toast } = await openEditor(conflictHandlers(() => (overwritten = true)));
    fireEvent.click(screen.getByRole('button', { name: '儲存' }));

    const conflict = await screen.findByTestId('version-conflict');
    expect(conflict.textContent).toContain('VERSION CONFLICT');
    expect(conflict.textContent).toContain('codex');
    expect(conflict.querySelector('.lv-diff__del')!.textContent).toContain('他們的第二行');
    expect(conflict.querySelector('.lv-diff__add')!.textContent).toContain('我的第二行');

    fireEvent.click(screen.getByRole('button', { name: '以我的版本覆寫' }));
    const dialog = await screen.findByRole('dialog');
    expect(overwritten).toBe(false); // 還沒確認，不送
    expect(callsTo('/v1/update')).toHaveLength(1);
    fireEvent.click(within(dialog).getByRole('button', { name: '確認覆寫' }));

    await waitFor(() => expect(overwritten).toBe(true));
    const retry = callsTo('/v1/update')[1]!.body;
    expect(retry.expected_updated).toBe(theirs.updated);
    expect(retry.body).toBe('第一行\n我的第二行');
    expect(retry.author).toBe(UI_AUTHOR);
    await waitFor(() => expect(screen.queryByTestId('version-conflict')).toBeNull());
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('摘要將在背景重新產生'), 'success');
  });

  it('放棄：不重送，回到閱讀並顯示目前版本', async () => {
    const { callsTo } = await openEditor(conflictHandlers(() => undefined));
    fireEvent.click(screen.getByRole('button', { name: '儲存' }));
    await screen.findByTestId('version-conflict');
    fireEvent.click(screen.getByRole('button', { name: '放棄我的修改' }));
    await screen.findByText(/他們的第二行/);
    expect(callsTo('/v1/update')).toHaveLength(1);
  });

  it('合併：以目前版本為底產生衝突標記，標記未清前不能儲存；清掉後以目前版本的 updated 送出', async () => {
    let merged = false;
    const { callsTo } = await openEditor(conflictHandlers(() => (merged = true)));
    fireEvent.click(screen.getByRole('button', { name: '儲存' }));
    await screen.findByTestId('version-conflict');
    fireEvent.click(screen.getByRole('button', { name: '在目前版本上合併我的修改' }));
    expect(await screen.findByTestId('merge-draft')).toBeTruthy();
    const textarea = document.querySelector('textarea')!;
    expect(textarea.value).toContain(MARK_START);
    expect(textarea.value).toContain('他們的第二行');
    expect(textarea.value).toContain('我的第二行');

    fireEvent.click(screen.getByRole('button', { name: '儲存' }));
    expect((await screen.findByRole('alert')).textContent).toContain('衝突標記');
    expect(callsTo('/v1/update')).toHaveLength(1);

    fireEvent.input(textarea, { target: { value: '第一行\n他們的第二行\n我的第二行' } });
    fireEvent.click(screen.getByRole('button', { name: '儲存' }));
    await waitFor(() => expect(merged).toBe(true));
    expect(callsTo('/v1/update')[1]!.body.expected_updated).toBe(theirs.updated);
  });

  it('author 被 422 拒收：明確說明並可取消署名；取消後不再帶 author', async () => {
    const { callsTo } = await openEditor({
      '/v1/get': getHandler(() => note({ links: [] })),
      '/v1/update': (body) =>
        'author' in body
          ? { status: 422, body: { detail: [{ type: 'extra_forbidden', loc: ['body', 'author'], msg: 'Extra inputs are not permitted' }] } }
          : json({ id: 'n1', vault: VAULT, updated: 'x', summary_stale: false, embedding_stale: false }),
    });
    fireEvent.click(screen.getByRole('button', { name: '儲存' }));
    expect((await screen.findByRole('alert')).textContent).toContain('A22');
    fireEvent.click(screen.getByRole('checkbox', { name: /署名寫入/ }));
    fireEvent.click(screen.getByRole('button', { name: '儲存' }));
    await waitFor(() => expect(callsTo('/v1/update')).toHaveLength(2));
    expect(callsTo('/v1/update')[1]!.body).not.toHaveProperty('author');
    expect(window.localStorage.getItem('lore-vault.author')).toBe('off');
  });

  it('沒有變更不送出', async () => {
    const ctx = makeApi({ '/v1/get': getHandler(() => note({ links: [] })), '/v1/update': () => json({}) });
    renderWithApp(<NoteDetail id="n1" />, ctx.api);
    fireEvent.click(await screen.findByRole('button', { name: '編輯' }));
    fireEvent.click(screen.getByRole('button', { name: '儲存' }));
    expect((await screen.findByRole('alert')).textContent).toContain('no_changes');
    expect(ctx.callsTo('/v1/update')).toHaveLength(0);
  });
});

describe('兩段式刪除', () => {
  it('先規劃並顯示範圍，確認後以相同參數＋token 執行', async () => {
    const { api, callsTo } = makeApi({
      '/v1/get': getHandler(() => note({ links: [] })),
      '/v1/note_delete': (body) =>
        body.confirm_token
          ? json({ executed: true, plan: {} })
          : json({
              executed: false,
              plan: { target: 'note', vault: VAULT, counts: { notes: 1, note_embeddings: 1 }, note_ids: ['n1'], requires_force: false },
              confirm_token: 'tok-1',
              expires_at: '2026-09-26T01:05:00.000Z',
            }),
    });
    const { navigate, toast } = renderWithApp(<NoteDetail id="n1" />, api);
    fireEvent.click(await screen.findByRole('button', { name: '刪除…' }));
    const plan = await screen.findByTestId('delete-plan');
    expect(plan.textContent).toContain('筆記');
    expect(plan.textContent).toContain('筆記向量');
    expect(callsTo('/v1/note_delete')).toHaveLength(1);
    expect(callsTo('/v1/note_delete')[0]!.body).toEqual({ space: 'dev', vault: VAULT, id: 'n1' });

    fireEvent.click(screen.getByRole('button', { name: '確認刪除' }));
    await waitFor(() => expect(callsTo('/v1/note_delete')).toHaveLength(2));
    expect(callsTo('/v1/note_delete')[1]!.body).toEqual({ space: 'dev', vault: VAULT, id: 'n1', confirm_token: 'tok-1' });
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/ui/notes'));
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('已刪除'), 'success');
  });

  it('規劃已變動（409 plan_changed）：顯示新規劃並要求重新規劃，不自動重送', async () => {
    const { api, callsTo } = makeApi({
      '/v1/get': getHandler(() => note({ links: [] })),
      '/v1/note_delete': (body) =>
        body.confirm_token
          ? apiError(409, 'plan_changed', { plan: { target: 'note', vault: VAULT, counts: { notes: 1, note_embeddings: 0 } } })
          : json({ executed: false, plan: { target: 'note', vault: VAULT, counts: { notes: 1 } }, confirm_token: 'tok', expires_at: null }),
    });
    renderWithApp(<NoteDetail id="n1" />, api);
    fireEvent.click(await screen.findByRole('button', { name: '刪除…' }));
    await screen.findByTestId('delete-plan');
    fireEvent.click(screen.getByRole('button', { name: '確認刪除' }));
    expect((await screen.findByRole('alert')).textContent).toContain('plan_changed');
    expect(screen.getByRole('button', { name: '重新規劃' })).toBeTruthy();
    expect(callsTo('/v1/note_delete')).toHaveLength(2);
  });
});

describe('新增筆記', () => {
  it('寫入後回報疑似重複與查重降級，並附分數與原因', async () => {
    const { api, callsTo } = makeApi({
      '/v1/write': () =>
        json(
          {
            id: 'new1',
            vault: VAULT,
            updated: 'x',
            duplicates: [{ id: 'n1', title: '注入預算', updated: '2026-09-26T01:00:00.000Z', reasons: ['title', 'lexical'], lexical: 0.91, vector: null }],
            dedup_degraded: true,
            dedup_reason: 'embedder_unavailable',
          },
          201,
        ),
    });
    const { navigate } = renderWithApp(<NoteNew supersedes={null} />, api, { vault: VAULT });
    fireEvent.input(screen.getByLabelText('標題'), { target: { value: '注入預算 800 字' } });
    fireEvent.input(document.querySelector('textarea')!, { target: { value: '正文' } });
    fireEvent.click(screen.getByRole('button', { name: '寫入' }));

    const dupes = await screen.findByTestId('duplicates');
    expect(dupes.textContent).toContain('疑似重複 · 1');
    expect(dupes.textContent).toContain('標題相同、字詞相近');
    expect(dupes.textContent).toContain('0.91');
    expect(screen.getByTestId('dedup-degraded').textContent).toContain('embedder_unavailable');
    expect(callsTo('/v1/write')[0]!.body).toMatchObject({ space: 'dev', vault: VAULT, title: '注入預算 800 字', author: UI_AUTHOR });
    fireEvent.click(screen.getByRole('button', { name: '開啟這則改寫 →' }));
    expect(navigate).toHaveBeenCalledWith('/ui/notes/n1');
  });

  it('沒有重複且查重正常：直接開啟新筆記', async () => {
    const { api } = makeApi({
      '/v1/write': () => json({ id: 'new1', vault: VAULT, updated: 'x', duplicates: [], dedup_degraded: false, dedup_reason: null }, 201),
    });
    const { navigate } = renderWithApp(<NoteNew supersedes={null} />, api, { vault: VAULT });
    fireEvent.input(screen.getByLabelText('標題'), { target: { value: 't' } });
    fireEvent.click(screen.getByRole('button', { name: '寫入' }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/ui/notes/new1'));
  });

  it('本 space 全部時必須先選 vault', () => {
    const { api } = makeApi({});
    renderWithApp(<NoteNew supersedes={null} />, api);
    fireEvent.input(screen.getByLabelText('標題'), { target: { value: 't' } });
    expect((screen.getByRole('button', { name: '寫入' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
