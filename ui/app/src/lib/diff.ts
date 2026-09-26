// 行級差異（LCS）與合併草稿：版本衝突畫面用。筆記正文規模（數百行）下 O(n·m) 足夠。

export type DiffOp = { type: 'same' | 'del' | 'add'; line: string };

const MAX_CELLS = 4_000_000;

export function diffLines(a: string, b: string): DiffOp[] {
  const x = a.split('\n');
  const y = b.split('\n');
  const n = x.length;
  const m = y.length;
  if (n * m > MAX_CELLS) {
    // 太大：退化成整段刪除＋整段新增，仍然誠實呈現兩邊內容
    return [...x.map((line) => ({ type: 'del' as const, line })), ...y.map((line) => ({ type: 'add' as const, line }))];
  }
  const dp: Uint32Array[] = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i]![j] = x[i] === y[j] ? dp[i + 1]![j + 1]! + 1 : Math.max(dp[i + 1]![j]!, dp[i]![j + 1]!);
    }
  }
  const ops: DiffOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) {
      ops.push({ type: 'same', line: x[i]! });
      i++;
      j++;
    } else if (dp[i + 1]![j]! >= dp[i]![j + 1]!) {
      ops.push({ type: 'del', line: x[i]! });
      i++;
    } else {
      ops.push({ type: 'add', line: y[j]! });
      j++;
    }
  }
  while (i < n) ops.push({ type: 'del', line: x[i++]! });
  while (j < m) ops.push({ type: 'add', line: y[j++]! });
  return ops;
}

export const MARK_START = '<<<<<<< 目前版本';
export const MARK_MID = '=======';
export const MARK_END = '>>>>>>> 我的修改';

/** 以「目前版本」為底，把與「我的修改」不同的區塊包成衝突標記，交給使用者整理。 */
export function mergeDraft(current: string, mine: string): string {
  const ops = diffLines(current, mine);
  const out: string[] = [];
  let theirs: string[] = [];
  let ours: string[] = [];
  const flush = () => {
    if (theirs.length === 0 && ours.length === 0) return;
    out.push(MARK_START, ...theirs, MARK_MID, ...ours, MARK_END);
    theirs = [];
    ours = [];
  };
  for (const op of ops) {
    if (op.type === 'same') {
      flush();
      out.push(op.line);
    } else if (op.type === 'del') theirs.push(op.line);
    else ours.push(op.line);
  }
  flush();
  return out.join('\n');
}

export function hasConflictMarkers(text: string): boolean {
  return text.split('\n').some((l) => l === MARK_START || l === MARK_END);
}
