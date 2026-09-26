/** @vitest-environment happy-dom */
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { MARK_START } from '../lib/diff';
import type { NoteFull } from '../lib/types';
import { apiError, json, makeApi, renderWithApp, TEST_AUTHOR, TEST_LIMITS, type Handler } from '../test/harness';
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

/** /v1/get：詳情（ids=[n1]）回 `current()`；互連／更正鏈標題（fields=meta）回 extra 裡有的，其餘列為 missing */
function getHandler(current: () => NoteFull, extra: NoteFull[] = [related]): Handler {
  return (body) => {
    const ids = body.ids as string[];
    if (body.fields === 'meta') {
      const found = extra.filter((n) => ids.includes(n.id)).map(({ body: _b, ...meta }) => meta);
      return json({ items: found, missing: ids.filter((i) => !found.some((n) => n.id === i)), unavailable: [], truncated: false, budget: 12000, used_chars: 0 });
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
    expect(retry.author).toBe(TEST_AUTHOR);
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

  it('署名一律開啟：沒有開關，每次都帶 author；被 422 拒收時明確說明、不偷偷拿掉重送', async () => {
    const { callsTo } = await openEditor({
      '/v1/get': getHandler(() => note({ links: [] })),
      '/v1/update': () => ({ status: 422, body: { detail: [{ type: 'extra_forbidden', loc: ['body', 'author'], msg: 'Extra inputs are not permitted' }] } }),
    });
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.getByTestId('author-line').textContent).toContain(TEST_AUTHOR);
    fireEvent.click(screen.getByRole('button', { name: '儲存' }));
    expect((await screen.findByRole('alert')).textContent).toContain('A22');
    fireEvent.click(screen.getByRole('button', { name: '儲存' }));
    await waitFor(() => expect(callsTo('/v1/update')).toHaveLength(2));
    expect(callsTo('/v1/update').every((c) => c.body.author === TEST_AUTHOR)).toBe(true);
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
    expect(await screen.findByTestId('note-deleted')).toBeTruthy();
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('已刪除'), 'success');
    fireEvent.click(screen.getByRole('button', { name: '回筆記列表' }));
    expect(navigate).toHaveBeenCalledWith('/ui/notes');
  });

  async function deleteThenUndelete(undelete: Handler) {
    const ctx = makeApi({
      '/v1/get': getHandler(() => note({ links: [] })),
      '/v1/note_delete': (body) =>
        body.confirm_token
          ? json({ executed: true, plan: {} })
          : json({ executed: false, plan: { target: 'note', vault: VAULT, counts: { notes: 1 } }, confirm_token: 't', expires_at: null }),
      '/v1/note_undelete': undelete,
    });
    const rendered = renderWithApp(<NoteDetail id="n1" />, ctx.api);
    fireEvent.click(await screen.findByRole('button', { name: '刪除…' }));
    await screen.findByTestId('delete-plan');
    fireEvent.click(screen.getByRole('button', { name: '確認刪除' }));
    await screen.findByTestId('note-deleted');
    fireEvent.click(screen.getByRole('button', { name: '復原這則筆記' }));
    return { ...ctx, ...rendered };
  }

  it('復原：restored=true 還原原 id 與內容，回到閱讀', async () => {
    const { callsTo, toast } = await deleteThenUndelete(() =>
      json({ undeleted: {}, restored: true, reimportable: false, note: { id: 'n1', vault: VAULT, title: '注入預算', author: null, updated: 'x' } }),
    );
    await screen.findByTestId('note-summary');
    expect(callsTo('/v1/note_undelete')[0]!.body).toEqual({ space: 'dev', id: 'n1' });
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('已還原'), 'success');
  });

  it('復原：舊墓碑 restored=false 明講內容沒回來（區分可否重跑匯入）', async () => {
    await deleteThenUndelete(() => json({ undeleted: {}, restored: false, reimportable: true, note: null }));
    const result = await screen.findByTestId('undelete-result');
    expect(result.textContent).toContain('內容未還原');
    expect(result.textContent).toContain('重跑匯入');
    expect(screen.queryByRole('button', { name: '復原這則筆記' })).toBeNull();
  });

  it('復原被拒（409 not_restorable）顯示原因', async () => {
    await deleteThenUndelete(() => apiError(409, 'not_restorable', { reason: 'vault_deleted' }));
    const result = await screen.findByTestId('undelete-result');
    expect(result.textContent).toContain('所屬 vault 已被刪除');
    expect(result.textContent).toContain('vault_deleted');
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

describe('更正鏈與未解析連結', () => {
  it('被取代（superseded_by）：顯示橫幅與更正版連結，標題以 fields=meta 取得', async () => {
    const newer = note({ id: 'n9', title: '注入預算（更正）', links: [], supersedes: 'n1' });
    const { api, callsTo } = makeApi({ '/v1/get': getHandler(() => note({ links: [], superseded_by: 'n9' }), [newer]) });
    const { navigate } = renderWithApp(<NoteDetail id="n1" />, api);
    const banner = await screen.findByTestId('note-superseded');
    await waitFor(() => expect(banner.textContent).toContain('注入預算（更正）'));
    const chain = screen.getByTestId('chain-superseded-by');
    expect(within(chain).getByRole('link', { name: '注入預算（更正）' }).getAttribute('href')).toBe('/ui/notes/n9');
    fireEvent.click(within(banner).getByRole('button', { name: '開啟更正版' }));
    expect(navigate).toHaveBeenCalledWith('/ui/notes/n9');
    const meta = callsTo('/v1/get').find((c) => c.body.fields === 'meta')!;
    expect(meta.body.ids).toEqual(['n9']);
    expect(meta.body).not.toHaveProperty('budget');
  });

  it('互連標題查詢的 id 數受 session limits.get_max_ids 限制', async () => {
    const many = Array.from({ length: 30 }, (_, i) => `x${i}`);
    const { api, callsTo } = makeApi({ '/v1/get': getHandler(() => note({ links: many })) });
    renderWithApp(<NoteDetail id="n1" />, api);
    await waitFor(() => expect(callsTo('/v1/get').some((c) => c.body.fields === 'meta')).toBe(true));
    const meta = callsTo('/v1/get').find((c) => c.body.fields === 'meta')!;
    expect((meta.body.ids as string[]).length).toBe(TEST_LIMITS.get_max_ids);
  });

  it('儲存後服務回報未解析／歧義的 [[ ]]：閱讀模式列出', async () => {
    let current = note({ links: [] });
    const ctx = makeApi({
      '/v1/get': getHandler(() => current),
      '/v1/update': (body) => {
        current = { ...current, body: body.body as string, updated: '2026-09-26T05:00:00.000Z' };
        return json({
          id: 'n1',
          vault: VAULT,
          updated: current.updated,
          summary_stale: false,
          embedding_stale: false,
          links: [],
          unresolved_links: [
            { target: '不存在的標題', status: 'unresolved', candidates: [] },
            { target: '同名', status: 'ambiguous', candidates: ['a', 'b'] },
          ],
        });
      },
    });
    renderWithApp(<NoteDetail id="n1" />, ctx.api);
    fireEvent.click(await screen.findByRole('button', { name: '編輯' }));
    fireEvent.input(document.querySelector('textarea')!, { target: { value: '[[不存在的標題]] [[同名]]' } });
    fireEvent.click(screen.getByRole('button', { name: '儲存' }));
    const banner = await screen.findByTestId('unresolved-links');
    expect(banner.textContent).toContain('2 個 [[ ]] 沒有解析成互連');
    expect(banner.textContent).toContain('[[不存在的標題]]：同一 vault 找不到這個標題');
    expect(banner.textContent).toContain('[[同名]]：同一 vault 有 2 則同名筆記');
  });
});

describe('新增筆記（寫入前查重）', () => {
  function fill(title = '注入預算 800 字') {
    fireEvent.input(screen.getByLabelText('標題'), { target: { value: title } });
    fireEvent.input(document.querySelector('textarea')!, { target: { value: '正文 [[沒有這則]]' } });
  }

  it('先 dry_run：列出疑似重複、查重降級與未解析連結，尚未寫入；改寫這則開啟既有筆記', async () => {
    const { api, callsTo } = makeApi({
      '/v1/write': () =>
        json({
          vault: VAULT,
          links: [],
          unresolved_links: [{ target: '沒有這則', status: 'unresolved', candidates: [] }],
          duplicates: [{ id: 'n1', title: '注入預算', updated: '2026-09-26T01:00:00.000Z', reasons: ['title', 'lexical'], lexical: 0.91, vector: null }],
          dedup_degraded: true,
          dedup_reason: 'embedder_unavailable',
          dry_run: true,
        }),
    });
    const { navigate } = renderWithApp(<NoteNew supersedes={null} />, api, { vault: VAULT });
    fill();
    fireEvent.click(screen.getByRole('button', { name: '寫入' }));

    const panel = await screen.findByTestId('dedup-preview');
    expect(panel.textContent).toContain('疑似重複 · 1');
    expect(panel.textContent).toContain('尚未寫入');
    const dupes = screen.getByTestId('duplicates');
    expect(dupes.textContent).toContain('標題相同、字詞相近');
    expect(dupes.textContent).toContain('0.91');
    expect(screen.getByTestId('dedup-degraded').textContent).toContain('embedder_unavailable');
    expect(screen.getByTestId('preview-unresolved').textContent).toContain('[[沒有這則]]');
    expect(callsTo('/v1/write')).toHaveLength(1);
    expect(callsTo('/v1/write')[0]!.body).toMatchObject({ space: 'dev', vault: VAULT, title: '注入預算 800 字', author: TEST_AUTHOR, dry_run: true });
    // 有結果時「寫入」停用：必須在面板明確選擇
    expect((screen.getByRole('button', { name: '寫入' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '改寫「注入預算」' }));
    expect(navigate).toHaveBeenCalledWith('/ui/notes/n1');
    expect(callsTo('/v1/write')).toHaveLength(1);
  });

  it('照樣新增：以相同內容正式寫入（不帶 dry_run），未解析連結以警示 toast 告知', async () => {
    const { api, callsTo } = makeApi({
      '/v1/write': (body) =>
        body.dry_run
          ? json({ vault: VAULT, links: [], unresolved_links: [], duplicates: [{ id: 'n1', title: '注入預算', updated: 'x', reasons: ['lexical'], lexical: 0.5, vector: 0.8 }], dedup_degraded: false, dedup_reason: null, dry_run: true })
          : json({ id: 'new1', vault: VAULT, updated: 'x', author: TEST_AUTHOR, principal: 'UEPBernie', links: [], unresolved_links: [{ target: '沒有這則', status: 'unresolved', candidates: [] }], duplicates: [], dedup_degraded: false, dedup_reason: null, dry_run: false }, 201),
    });
    const { navigate, toast } = renderWithApp(<NoteNew supersedes={null} />, api, { vault: VAULT });
    fill();
    fireEvent.click(screen.getByRole('button', { name: '寫入' }));
    await screen.findByTestId('duplicates');
    fireEvent.click(screen.getByRole('button', { name: '照樣新增' }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/ui/notes/new1'));
    const [dry, real] = callsTo('/v1/write');
    expect(dry!.body.dry_run).toBe(true);
    expect(real!.body).not.toHaveProperty('dry_run');
    const dryPayload = { ...dry!.body };
    delete dryPayload.dry_run;
    expect(real!.body).toEqual(dryPayload);
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('沒有這則'), 'warning');
  });

  it('查重後改了內容：預覽失效，照樣新增停用，寫入重新查重', async () => {
    const { api, callsTo } = makeApi({
      '/v1/write': () => json({ vault: VAULT, links: [], unresolved_links: [], duplicates: [{ id: 'n1', title: '注入預算', updated: 'x', reasons: ['title'], lexical: 1, vector: null }], dedup_degraded: false, dedup_reason: null, dry_run: true }),
    });
    renderWithApp(<NoteNew supersedes={null} />, api, { vault: VAULT });
    fill();
    fireEvent.click(screen.getByRole('button', { name: '寫入' }));
    await screen.findByTestId('duplicates');
    fireEvent.input(screen.getByLabelText('標題'), { target: { value: '改過的標題' } });
    expect(screen.getByTestId('preview-stale')).toBeTruthy();
    expect((screen.getByRole('button', { name: '照樣新增' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '寫入' }));
    await waitFor(() => expect(callsTo('/v1/write')).toHaveLength(2));
    expect(callsTo('/v1/write')[1]!.body).toMatchObject({ title: '改過的標題', dry_run: true });
  });

  it('沒有重複、查重正常、連結都解析：dry_run 後直接寫入並開啟新筆記', async () => {
    const { api, callsTo } = makeApi({
      '/v1/write': (body) =>
        body.dry_run
          ? json({ vault: VAULT, links: [], unresolved_links: [], duplicates: [], dedup_degraded: false, dedup_reason: null, dry_run: true })
          : json({ id: 'new1', vault: VAULT, updated: 'x', links: [], unresolved_links: [], duplicates: [], dedup_degraded: false, dedup_reason: null, dry_run: false }, 201),
    });
    const { navigate } = renderWithApp(<NoteNew supersedes={null} />, api, { vault: VAULT });
    fireEvent.input(screen.getByLabelText('標題'), { target: { value: 't' } });
    fireEvent.click(screen.getByRole('button', { name: '寫入' }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/ui/notes/new1'));
    expect(callsTo('/v1/write').map((c) => c.body.dry_run ?? false)).toEqual([true, false]);
  });

  it('本 space 全部時必須先選 vault', () => {
    const { api } = makeApi({});
    renderWithApp(<NoteNew supersedes={null} />, api);
    fireEvent.input(screen.getByLabelText('標題'), { target: { value: 't' } });
    expect((screen.getByRole('button', { name: '寫入' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
