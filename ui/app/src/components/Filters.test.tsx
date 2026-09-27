/** @vitest-environment happy-dom */
// 共用篩選元件：網址讀寫（只在自己的畫面寫、不合法值當預設）與文字篩選的套用／外部清除。
import { cleanup, fireEvent, render, screen } from '@testing-library/preact';
import { useState } from 'preact/hooks';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { queryChoice, queryDate, queryText, screenQuery, TextFilter, useQuerySync } from './Filters';

afterEach(() => {
  cleanup();
  window.history.replaceState(null, '', '/');
});

function Sync({ screenId, values }: { screenId: 'notes' | 'docs'; values: Record<string, string> }) {
  useQuerySync(screenId, values);
  return null;
}

describe('網址同步', () => {
  it('只在網址是該畫面列表時讀寫', () => {
    window.history.replaceState(null, '', '/ui/docs?title=x');
    expect(screenQuery('notes').toString()).toBe('');
    expect(screenQuery('docs').get('title')).toBe('x');
    render(<Sync screenId="notes" values={{ title: 'y' }} />);
    expect(window.location.pathname + window.location.search).toBe('/ui/docs?title=x');
    render(<Sync screenId="docs" values={{ title: 'y', type: '' }} />);
    expect(window.location.pathname + window.location.search).toBe('/ui/docs?title=y');
  });

  it('解析：白名單、日期格式、文字去空白與截長', () => {
    const q = new URLSearchParams({ s: 'bad', d: '2026-9-1', t: `  ${'x'.repeat(250)}  ` });
    expect(queryChoice(q, 's', ['a', 'b'] as const, 'a')).toBe('a');
    expect(queryDate(q, 'd')).toBe('');
    expect(queryText(q, 't')).toHaveLength(200);
  });
});

describe('TextFilter', () => {
  it('送出才套用；外部清除時輸入框跟著清空', () => {
    const applied = vi.fn();
    function Host() {
      const [value, setValue] = useState('');
      return (
        <>
          <TextFilter
            value={value}
            onApply={(v) => {
              applied(v);
              setValue(v);
            }}
            inputLabel="關鍵字"
            placeholder=""
            submitLabel="套用"
          />
          <button type="button" onClick={() => setValue('')}>
            清空
          </button>
        </>
      );
    }
    render(<Host />);
    const input = screen.getByRole('textbox', { name: '關鍵字' }) as HTMLInputElement;
    fireEvent.input(input, { target: { value: ' abc ' } });
    expect(applied).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '套用' }));
    expect(applied).toHaveBeenCalledWith('abc');
    fireEvent.click(screen.getByRole('button', { name: '清空' }));
    expect(input.value).toBe('');
  });
});
