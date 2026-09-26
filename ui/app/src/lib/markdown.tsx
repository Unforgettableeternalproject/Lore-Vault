// 安全的 Markdown 呈現：marked 只負責切 token，由這裡把 token 轉成 Preact VNode。
// 全程不產生 HTML 字串、不用 dangerouslySetInnerHTML：
// - 原始 HTML（區塊與行內）一律當純文字顯示，不執行、不解讀
// - 連結只放行 http/https/mailto 與站內相對路徑，其他（javascript:、data: 等）顯示為純文字
// - 圖片不載入（CSP img-src 只允許同源），改成可點的文字連結
// - `[[標題]]` 為互連：交給呼叫端決定已解析（開啟筆記）或未解析（帶去搜尋）
import { Marked, type Token, type Tokens } from 'marked';
import type { ComponentChildren, VNode } from 'preact';

interface WikiToken {
  type: 'wikilink';
  raw: string;
  title: string;
}

const marked = new Marked({
  gfm: true,
  extensions: [
    {
      name: 'wikilink',
      level: 'inline',
      start(src: string) {
        const i = src.indexOf('[[');
        return i < 0 ? undefined : i;
      },
      tokenizer(src: string) {
        const m = /^\[\[([^[\]\n]+?)\]\]/.exec(src);
        if (!m) return undefined;
        return { type: 'wikilink', raw: m[0], title: m[1]!.trim() };
      },
    },
  ],
});

export interface MarkdownOptions {
  /** 渲染 `[[標題]]`；未提供時顯示原文 */
  renderWikiLink?: (title: string, key: string) => VNode;
}

const SAFE_PROTOCOL = /^(https?:|mailto:)/i;

export function safeHref(href: string | null | undefined): string | null {
  if (!href) return null;
  const trimmed = href.trim();
  // 去掉控制字元與空白後再判斷協定（擋 `java\nscript:` 這類繞法）
  // eslint-disable-next-line no-control-regex
  const compact = trimmed.replace(/[\u0000- ]/g, '');
  if (SAFE_PROTOCOL.test(compact)) return trimmed;
  if (/^[a-z][a-z0-9+.-]*:/i.test(compact)) return null; // 其他協定一律不放行
  if (compact.startsWith('//')) return null; // protocol-relative 會跳到外站
  return trimmed; // 相對路徑、#錨點
}

// marked 在 lexer 階段已解開字元參照（&amp; → &）；Preact 輸出文字節點時會自行跳脫，不需再處理
function inline(tokens: Token[] | undefined, opts: MarkdownOptions, prefix: string): ComponentChildren[] {
  if (!tokens) return [];
  return tokens.map((token, i) => inlineToken(token, opts, `${prefix}.${i}`));
}

function inlineToken(token: Token, opts: MarkdownOptions, key: string): ComponentChildren {
  switch (token.type) {
    case 'text': {
      const t = token as Tokens.Text;
      return t.tokens ? <>{inline(t.tokens, opts, key)}</> : t.text;
    }
    case 'escape':
      return (token as Tokens.Escape).text;
    case 'strong':
      return <strong key={key}>{inline((token as Tokens.Strong).tokens, opts, key)}</strong>;
    case 'em':
      return <em key={key}>{inline((token as Tokens.Em).tokens, opts, key)}</em>;
    case 'del':
      return <del key={key}>{inline((token as Tokens.Del).tokens, opts, key)}</del>;
    case 'codespan':
      return (
        <code key={key} class="lv-md__code">
          {(token as Tokens.Codespan).text}
        </code>
      );
    case 'br':
      return <br key={key} />;
    case 'link': {
      const t = token as Tokens.Link;
      const href = safeHref(t.href);
      const children = inline(t.tokens, opts, key);
      if (!href) {
        return (
          <span key={key} class="lv-md__unsafe" title={`已停用的連結：${t.href}`}>
            {children}
          </span>
        );
      }
      const external = /^(https?:|mailto:)/i.test(href);
      return (
        <a
          key={key}
          href={href}
          title={t.title ?? undefined}
          rel={external ? 'noopener noreferrer' : undefined}
          target={external ? '_blank' : undefined}
        >
          {children}
        </a>
      );
    }
    case 'image': {
      const t = token as Tokens.Image;
      const href = safeHref(t.href);
      const label = `圖片：${t.text || t.href}`;
      return href ? (
        <a key={key} href={href} rel="noopener noreferrer" target="_blank" class="lv-md__image">
          {label}
        </a>
      ) : (
        <span key={key} class="lv-md__unsafe">
          {label}
        </span>
      );
    }
    case 'html':
      // 原始 HTML：照原文顯示，不解讀
      return (
        <code key={key} class="lv-md__raw-html">
          {(token as Tokens.HTML).text}
        </code>
      );
    case 'wikilink': {
      const t = token as unknown as WikiToken;
      return opts.renderWikiLink ? opts.renderWikiLink(t.title, key) : t.raw;
    }
    default:
      return 'raw' in token && typeof token.raw === 'string' ? token.raw : '';
  }
}

function block(tokens: Token[], opts: MarkdownOptions, prefix: string): ComponentChildren[] {
  return tokens.map((token, i) => blockToken(token, opts, `${prefix}.${i}`));
}

function blockToken(token: Token, opts: MarkdownOptions, key: string): ComponentChildren {
  switch (token.type) {
    case 'space':
    case 'def':
      return null;
    case 'heading': {
      const t = token as Tokens.Heading;
      const Tag = (`h${Math.min(6, Math.max(1, t.depth + 1))}`) as 'h2';
      return (
        <Tag key={key} class="lv-md__heading">
          {inline(t.tokens, opts, key)}
        </Tag>
      );
    }
    case 'paragraph':
      return <p key={key}>{inline((token as Tokens.Paragraph).tokens, opts, key)}</p>;
    case 'text': {
      const t = token as Tokens.Text;
      return <p key={key}>{t.tokens ? inline(t.tokens, opts, key) : t.text}</p>;
    }
    case 'code': {
      const t = token as Tokens.Code;
      return (
        <pre key={key} class="lv-md__pre" data-lang={t.lang || undefined}>
          <code>{t.text}</code>
        </pre>
      );
    }
    case 'blockquote':
      return <blockquote key={key}>{block((token as Tokens.Blockquote).tokens, opts, key)}</blockquote>;
    case 'hr':
      return <hr key={key} />;
    case 'list': {
      const t = token as Tokens.List;
      const items = t.items.map((item, i) => (
        <li key={`${key}.${i}`}>
          {item.task && (
            <input type="checkbox" checked={!!item.checked} disabled aria-label={item.checked ? '已完成' : '未完成'} />
          )}
          {item.loose ? block(item.tokens, opts, `${key}.${i}`) : listInline(item.tokens, opts, `${key}.${i}`)}
        </li>
      ));
      return t.ordered ? (
        <ol key={key} start={typeof t.start === 'number' ? t.start : undefined}>
          {items}
        </ol>
      ) : (
        <ul key={key}>{items}</ul>
      );
    }
    case 'table': {
      const t = token as Tokens.Table;
      return (
        <div key={key} class="lv-md__table-wrap">
          <table class="lv-md__table">
            <thead>
              <tr>
                {t.header.map((cell, i) => (
                  <th key={i} class={cell.align ? `is-${cell.align}` : undefined}>
                    {inline(cell.tokens, opts, `${key}.h${i}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {t.rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, i) => (
                    <td key={i} class={cell.align ? `is-${cell.align}` : undefined}>
                      {inline(cell.tokens, opts, `${key}.${r}.${i}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    case 'html':
      return (
        <pre key={key} class="lv-md__pre lv-md__raw-html">
          <code>{(token as Tokens.HTML).text}</code>
        </pre>
      );
    default:
      return 'raw' in token && typeof token.raw === 'string' ? <p key={key}>{token.raw}</p> : null;
  }
}

// 緊湊清單的項目是 text 區塊（內含 inline tokens），不包 <p>
function listInline(tokens: Token[], opts: MarkdownOptions, prefix: string): ComponentChildren[] {
  return tokens.map((token, i) => {
    const key = `${prefix}.${i}`;
    if (token.type === 'text') {
      const t = token as Tokens.Text;
      return t.tokens ? <>{inline(t.tokens, opts, key)}</> : t.text;
    }
    return blockToken(token, opts, key);
  });
}

export function Markdown({ source, options = {} }: { source: string; options?: MarkdownOptions }) {
  const tokens = marked.lexer(source);
  return <div class="lv-md">{block(tokens, options, 'md')}</div>;
}

/** 取出正文中所有 `[[標題]]`（去重、保留順序）。 */
export function wikiTitles(source: string): string[] {
  const out: string[] = [];
  const re = /\[\[([^[\]\n]+?)\]\]/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(source))) {
    const title = m[1]!.trim();
    if (title && !out.includes(title)) out.push(title);
  }
  return out;
}
