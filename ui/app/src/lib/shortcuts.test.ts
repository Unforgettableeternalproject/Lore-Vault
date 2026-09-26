/** @vitest-environment happy-dom */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { CHORD_TIMEOUT_MS, createShortcutHandler, isTypingTarget, SHORTCUTS } from './shortcuts';

function setup() {
  let t = 1000;
  const actions = { focusSearch: vi.fn(), goto: vi.fn(), openHelp: vi.fn() };
  const handle = createShortcutHandler(actions, () => t);
  const press = (key: string, init: KeyboardEventInit = {}, target: EventTarget = document.body) => {
    const e = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init });
    Object.defineProperty(e, 'target', { value: target });
    return { handled: handle(e), prevented: e.defaultPrevented };
  };
  return { actions, press, advance: (ms: number) => (t += ms) };
}

afterEach(() => {
  document.body.innerHTML = '';
});

describe('快捷鍵', () => {
  it('/ 聚焦檢索、? 開說明；單獨的字母不觸發', () => {
    const { actions, press } = setup();
    expect(press('/').prevented).toBe(true);
    expect(actions.focusSearch).toHaveBeenCalledTimes(1);
    press('?', { shiftKey: true });
    expect(actions.openHelp).toHaveBeenCalledTimes(1);
    expect(press('n').handled).toBe(false);
  });

  it('g 再按字母跳畫面；逾時或未知字母不觸發', () => {
    const { actions, press, advance } = setup();
    press('g');
    press('n');
    expect(actions.goto).toHaveBeenLastCalledWith('notes');
    press('g');
    press('d');
    expect(actions.goto).toHaveBeenLastCalledWith('docs');
    press('g');
    advance(CHORD_TIMEOUT_MS + 1);
    press('h');
    expect(actions.goto).toHaveBeenCalledTimes(2);
    press('g');
    press('x');
    expect(actions.goto).toHaveBeenCalledTimes(2);
  });

  it('輸入框、textarea、contenteditable 中不觸發', () => {
    const { actions, press } = setup();
    const input = document.createElement('input');
    const textarea = document.createElement('textarea');
    const editable = document.createElement('div');
    editable.contentEditable = 'true';
    document.body.append(input, textarea, editable);
    for (const target of [input, textarea]) {
      expect(press('/', {}, target).handled).toBe(false);
      expect(press('?', {}, target).handled).toBe(false);
      expect(press('g', {}, target).handled).toBe(false);
    }
    expect(isTypingTarget(editable)).toBe(true);
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    expect(isTypingTarget(checkbox)).toBe(false);
    expect(actions.focusSearch).not.toHaveBeenCalled();
  });

  it('輸入法組字中、修飾鍵、modal 對話框開著時不觸發', () => {
    const { actions, press } = setup();
    expect(press('/', { isComposing: true }).handled).toBe(false);
    expect(press('/', { ctrlKey: true }).handled).toBe(false);
    expect(press('/', { metaKey: true }).handled).toBe(false);
    const dialog = document.createElement('div');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    document.body.append(dialog);
    expect(press('/').handled).toBe(false);
    expect(actions.focusSearch).not.toHaveBeenCalled();
  });

  it('說明面板列出的快捷鍵與 g 系列定義一致', () => {
    const keys = SHORTCUTS.map((s) => s.keys);
    expect(keys).toEqual(expect.arrayContaining(['/', '?', 'g n', 'g d', 'g s']));
  });
});
