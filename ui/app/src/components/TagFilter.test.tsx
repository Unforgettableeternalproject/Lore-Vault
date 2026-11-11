/** @vitest-environment happy-dom */
// 標籤篩選：搜尋過濾（不分大小寫、中文、忽略 #）、無結果提示、已選固定可見、清空復原、排序與點選行為。
import { cleanup, fireEvent, render, screen } from '@testing-library/preact';
import { useState } from 'preact/hooks';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { filterTags, normalizeTagQuery, TagFilter, type TagOption } from './TagFilter';

afterEach(cleanup);

const OPTIONS: TagOption[] = [
  { topic: 'Compliance-V2', count: 16 },
  { topic: 'hr', count: 11 },
  { topic: '合規審查', count: 5 },
  { topic: 'hook', count: 3 },
];

function listLabels(): string[] {
  const list = screen.queryByTestId('tag-list');
  return list ? Array.from(list.querySelectorAll('button')).map((b) => b.getAttribute('aria-label') ?? '') : [];
}

function typeQuery(value: string) {
  fireEvent.input(screen.getByTestId('tag-search'), { target: { value } });
}

function Harness({ initial = null }: { initial?: string | null }) {
  const [tag, setTag] = useState<string | null>(initial);
  return <TagFilter options={OPTIONS} value={tag} onChange={setTag} />;
}

describe('filterTags', () => {
  it('忽略開頭 #、不分大小寫、支援中文；空字串回全部', () => {
    expect(normalizeTagQuery('  ##HR ')).toBe('hr');
    expect(filterTags(OPTIONS, '#COMPLIANCE').map((t) => t.topic)).toEqual(['Compliance-V2']);
    expect(filterTags(OPTIONS, '合規').map((t) => t.topic)).toEqual(['合規審查']);
    expect(filterTags(OPTIONS, '#').map((t) => t.topic)).toHaveLength(4);
    expect(filterTags(OPTIONS, '')).toHaveLength(4);
  });
});

describe('TagFilter', () => {
  it('預設依傳入順序（筆數）列出全部', () => {
    render(<Harness />);
    expect(listLabels()).toEqual(['#Compliance-V2（16 則）', '#hr（11 則）', '#合規審查（5 則）', '#hook（3 則）']);
  });

  it('輸入即時過濾；開頭 # 與大小寫不影響；中文可搜', () => {
    render(<Harness />);
    typeQuery('#HO');
    expect(listLabels()).toEqual(['#hook（3 則）']);
    typeQuery('合規');
    expect(listLabels()).toEqual(['#合規審查（5 則）']);
    typeQuery('compliance');
    expect(listLabels()).toEqual(['#Compliance-V2（16 則）']);
  });

  it('沒有符合時顯示「沒有符合的 tag」，「全部」仍在', () => {
    render(<Harness />);
    typeQuery('zzz');
    expect(screen.getByTestId('tag-empty').textContent).toBe('沒有符合的 tag');
    expect(screen.queryByTestId('tag-list')).toBeNull();
    expect(screen.getByRole('button', { name: '全部' })).toBeTruthy();
  });

  it('只輸入 # 視為沒有條件，不顯示無結果', () => {
    render(<Harness />);
    typeQuery('#');
    expect(screen.queryByTestId('tag-empty')).toBeNull();
    expect(listLabels()).toHaveLength(4);
  });

  it('已選的 tag 固定在前面，過濾掉它時仍可見，且不在清單重複', () => {
    render(<Harness initial="hr" />);
    expect(screen.getByTestId('tag-selected').getAttribute('aria-label')).toBe('#hr（11 則）');
    typeQuery('合規');
    expect(screen.getByTestId('tag-selected').textContent).toContain('#hr');
    expect(listLabels()).toEqual(['#合規審查（5 則）']);
    typeQuery('zzz');
    expect(screen.getByTestId('tag-selected')).toBeTruthy();
  });

  it('已選的 tag 不在選項內（例如網址帶入）也照樣顯示，點它取消', () => {
    const onChange = vi.fn();
    render(<TagFilter options={OPTIONS} value="gone" onChange={onChange} />);
    const chip = screen.getByTestId('tag-selected');
    expect(chip.textContent).toBe('#gone');
    fireEvent.click(chip);
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('清空搜尋後恢復全部', () => {
    render(<Harness />);
    typeQuery('hook');
    expect(listLabels()).toHaveLength(1);
    typeQuery('');
    expect(listLabels()).toHaveLength(4);
    expect(screen.queryByTestId('tag-empty')).toBeNull();
  });

  it('點選回報 tag；點「全部」或已選回報 null', () => {
    const onChange = vi.fn();
    const { rerender } = render(<TagFilter options={OPTIONS} value={null} onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: '#hr（11 則）' }));
    expect(onChange).toHaveBeenLastCalledWith('hr');
    rerender(<TagFilter options={OPTIONS} value="hr" onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: '全部' }));
    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it('鍵盤順序：搜尋框在 chip 之前，捲動區本身不可聚焦', () => {
    render(<Harness />);
    const focusables = Array.from(document.querySelectorAll<HTMLElement>('input, button, [tabindex]'));
    expect(focusables[0]).toBe(screen.getByTestId('tag-search'));
    expect(screen.getByTestId('tag-list').hasAttribute('tabindex')).toBe(false);
    expect(document.querySelector('[style]')).toBeNull();
  });
});
