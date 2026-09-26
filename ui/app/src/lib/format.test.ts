// 語意檢索狀態的說法：逾時不說成離線；模型未載入是「首次查詢較慢」，不是離線。
import { describe, expect, it } from 'vitest';

import { describeModelLoaded, recallDegradedBadge } from './format';

describe('recallDegradedBadge', () => {
  it('依降級原因區分逾時／離線／異常', () => {
    expect(recallDegradedBadge('embedder_timeout').label).toBe('語意檢索逾時');
    expect(recallDegradedBadge('embedder_timeout').title).toContain('再查一次');
    expect(recallDegradedBadge('embedder_unavailable').label).toBe('語意檢索離線');
    expect(recallDegradedBadge('embedder_error').label).toBe('語意檢索異常');
    expect(recallDegradedBadge(null).label).toBe('語意檢索異常');
  });
});

describe('describeModelLoaded', () => {
  it('未載入不是離線，而是下一次查詢較慢', () => {
    expect(describeModelLoaded(true)).toMatchObject({ label: '已載入', tone: 'ok' });
    const cold = describeModelLoaded(false);
    expect(cold.label).toBe('可連線，模型未載入');
    expect(cold.note).toContain('不會因此降級');
    expect(cold.label).not.toContain('離線');
    expect(describeModelLoaded(null).tone).toBe('unknown');
    expect(describeModelLoaded(undefined).tone).toBe('unknown');
  });
});
