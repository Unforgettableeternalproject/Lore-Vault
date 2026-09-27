/** @vitest-environment happy-dom */
import { cleanup, render } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import { Markdown, safeHref, wikiTitles } from './markdown';

afterEach(cleanup);

describe('安全渲染', () => {
  it('原始 HTML 與 <script> 只當文字，不產生元素', () => {
    const { container } = render(
      <Markdown source={'<script>window.__pwned = 1</script>\n\n段落 <img src=x onerror="alert(1)"> <b>粗</b>'} />,
    );
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).toContain('<script>window.__pwned = 1</script>');
    expect(container.textContent).toContain('onerror="alert(1)"');
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it('javascript:／data: 連結被停用，http 連結保留並加 noopener', () => {
    const { container } = render(
      <Markdown source={'[壞](javascript:alert(1)) [也壞](JAVA\nSCRIPT:alert(1)) [資料](data:text/html,x) [好](https://example.com) [站內](/ui/notes)'} />,
    );
    const hrefs = Array.from(container.querySelectorAll('a')).map((a) => a.getAttribute('href'));
    expect(hrefs).toEqual(['https://example.com', '/ui/notes']);
    expect(container.querySelector('a[href="https://example.com"]')!.getAttribute('rel')).toContain('noopener');
    expect(container.textContent).toContain('壞');
  });

  it('圖片不載入，改成連結文字', () => {
    const { container } = render(<Markdown source={'![架構圖](https://example.com/a.png)'} />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('圖片：架構圖');
  });

  it('safeHref 擋 protocol-relative 與未知協定', () => {
    expect(safeHref('//evil.example')).toBeNull();
    expect(safeHref('vbscript:x')).toBeNull();
    expect(safeHref('#anchor')).toBe('#anchor');
    expect(safeHref('mailto:a@b.c')).toBe('mailto:a@b.c');
  });
});

describe('技術文本', () => {
  it('程式碼區塊、表格、清單照結構呈現', () => {
    const src = '## 預算\n\n| HOOK | 上限 |\n|---|---:|\n| PreToolUse | 600 字 |\n\n```py\nx = "<b>"\n```\n\n- 一\n- `二`';
    const { container } = render(<Markdown source={src} />);
    expect(container.querySelector('h3')!.textContent).toBe('預算');
    expect(container.querySelector('table td.is-right')!.textContent).toBe('600 字');
    expect(container.querySelector('pre code')!.textContent).toBe('x = "<b>"');
    expect(container.querySelectorAll('li')).toHaveLength(2);
    expect(container.querySelector('li code')!.textContent).toBe('二');
  });
});

describe('[[標題]] 互連', () => {
  it('交給 renderWikiLink 決定已解析／未解析', () => {
    const { container } = render(
      <Markdown
        source={'參考 [[已存在]] 與 [[不存在]]'}
        options={{
          renderWikiLink: (title, key) =>
            title === '已存在' ? (
              <a key={key} href="/ui/notes/n1">
                {title}
              </a>
            ) : (
              <button key={key} type="button">
                {title}（未解析）
              </button>
            ),
        }}
      />,
    );
    expect(container.querySelector('a[href="/ui/notes/n1"]')!.textContent).toBe('已存在');
    expect(container.querySelector('button')!.textContent).toBe('不存在（未解析）');
  });

  it('沒有 renderWikiLink 時保留原文', () => {
    const { container } = render(<Markdown source={'看 [[X]]'} />);
    expect(container.textContent).toBe('看 [[X]]');
  });

  it('wikiTitles 去重保序', () => {
    expect(wikiTitles('[[a]] [[b]] [[a]]')).toEqual(['a', 'b']);
  });
});
