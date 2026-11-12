/** @vitest-environment happy-dom */
import { cleanup, render, screen } from '@testing-library/preact';
import { afterEach, describe, expect, it } from 'vitest';

import { ProposalDiff } from './ProposalDiff';

afterEach(cleanup);

describe('ProposalDiff', () => {
  it('只列提案觸及的欄位，標題與標籤顯示前→後', () => {
    render(<ProposalDiff base={{ title: '舊', topics: ['a'], body: '內文' }} proposed={{ title: '新', topics: ['a', 'b'] }} />);
    expect(screen.getByTestId('proposal-field-title').textContent).toContain('舊 → 新');
    expect(screen.getByTestId('proposal-field-topics').textContent).toContain('#a → #a #b');
    expect(screen.queryByTestId('proposal-field-body')).toBeNull();
  });

  it('內文逐行標出刪除與新增', () => {
    render(<ProposalDiff base={{ body: 'x\ny' }} proposed={{ body: 'x\nz' }} />);
    const pre = screen.getByLabelText('內文差異');
    expect(pre.querySelector('.lv-diff__del')?.textContent).toBe('- y\n');
    expect(pre.querySelector('.lv-diff__add')?.textContent).toBe('+ z\n');
  });

  it('空值顯示（無）', () => {
    render(<ProposalDiff base={{ supersedes: 'n1' }} proposed={{ supersedes: null }} />);
    expect(screen.getByTestId('proposal-field-supersedes').textContent).toContain('n1 → （無）');
  });

  it('給 current 時變三欄：基準、目前、提案', () => {
    render(<ProposalDiff base={{ title: 'A' }} current={{ title: 'B' }} proposed={{ title: 'C' }} />);
    expect(screen.getByTestId('proposal-col-base').textContent).toContain('A');
    expect(screen.getByTestId('proposal-col-current').textContent).toContain('B');
    expect(screen.getByTestId('proposal-col-proposed').textContent).toContain('C');
  });

  it('沒有觸及欄位時說明沒有變更', () => {
    render(<ProposalDiff base={{}} proposed={{}} />);
    expect(screen.getByText('提案沒有任何欄位變更。')).toBeTruthy();
  });
});
