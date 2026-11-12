// note_update 提案的欄位級差異：兩欄（前→後）；基準已變動時三欄（提案基準／目前版本／提案內容）。
// 只列提案實際觸及的欄位（proposed 內有的鍵）。
import { diffLines } from '../lib/diff';
import type { NoteFields } from '../lib/types';

const FIELD_ORDER = ['title', 'topics', 'links', 'supersedes', 'body'] as const;
type FieldKey = (typeof FIELD_ORDER)[number];

const FIELD_LABEL: Record<FieldKey, string> = {
  title: '標題',
  topics: '標籤',
  links: '連結',
  supersedes: '取代',
  body: '內文',
};

/** 非內文欄位轉成一行文字；空值顯示（無） */
function inline(key: FieldKey, value: unknown): string {
  if (Array.isArray(value)) {
    const items = value.map((v) => (key === 'topics' ? `#${String(v)}` : String(v)));
    return items.length ? items.join(' ') : '（無）';
  }
  return typeof value === 'string' && value ? value : '（無）';
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

const PREFIX = { del: '- ', add: '+ ', same: '  ' } as const;

/** 內文逐行比對；side 指定只顯示哪一側（兩欄模式 both 同時顯示刪除與新增） */
function BodyLines({ from, to, side, label }: { from: string; to: string; side: 'del' | 'add' | 'both'; label: string }) {
  const ops = diffLines(from, to).filter((o) => side === 'both' || o.type === 'same' || o.type === side);
  return (
    <pre class="lv-diff" aria-label={label}>
      {ops.map((o, i) => (
        <span key={i} class={o.type === 'del' ? 'lv-diff__del' : o.type === 'add' ? 'lv-diff__add' : undefined}>
          {PREFIX[o.type] + o.line + '\n'}
        </span>
      ))}
    </pre>
  );
}

export function ProposalDiff({ base, proposed, current }: { base: NoteFields; proposed: NoteFields; current?: NoteFields | null }) {
  const keys = FIELD_ORDER.filter((k) => k in proposed);
  if (keys.length === 0) return <p class="lv-muted lv-small">提案沒有任何欄位變更。</p>;

  if (current) {
    const columns: { id: string; title: string; fields: NoteFields; mine?: boolean }[] = [
      { id: 'base', title: '提案基準', fields: base },
      { id: 'current', title: '目前版本', fields: current },
      { id: 'proposed', title: '提案內容', fields: proposed, mine: true },
    ];
    return (
      <div class="lv-conflict__cols" data-testid="proposal-diff-3col">
        {columns.map((col) => (
          <div key={col.id} class={'lv-conflict__col' + (col.mine ? ' lv-conflict__col--mine' : '')} data-testid={`proposal-col-${col.id}`}>
            <div class="lv-conflict__col-head">
              <span>{col.title}</span>
            </div>
            {keys.map((k) => (
              <div key={k} class="lv-small">
                <div class="lv-muted">{FIELD_LABEL[k]}</div>
                {k === 'body' ? (
                  col.id === 'base' ? (
                    <pre class="lv-diff" aria-label="提案基準內文">
                      {text(col.fields.body)}
                    </pre>
                  ) : (
                    // 目前版本與提案內容都和基準比，標出各自改了哪些行
                    <BodyLines from={text(base.body)} to={text(col.fields.body)} side="add" label={`${col.title}內文`} />
                  )
                ) : (
                  <div>{inline(k, col.fields[k])}</div>
                )}
              </div>
            ))}
          </div>
        ))}
      </div>
    );
  }

  return (
    <div data-testid="proposal-diff">
      {keys.map((k) => (
        <div key={k} class="lv-small" data-testid={`proposal-field-${k}`}>
          <div class="lv-muted">{FIELD_LABEL[k]}</div>
          {k === 'body' ? (
            <BodyLines from={text(base.body)} to={text(proposed.body)} side="both" label="內文差異" />
          ) : (
            <div>
              <span class="lv-diff__del">{inline(k, base[k])}</span> → <span class="lv-diff__add">{inline(k, proposed[k])}</span>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
