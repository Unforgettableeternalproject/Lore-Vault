/** @vitest-environment happy-dom */
// 登入頁（A23）：帳號＋密碼、剩餘次數、鎖定訊息、無帳號提示。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LOCKED_MESSAGE } from '../lib/session';
import { apiError, json, makeApi, type Reply } from '../test/harness';
import { Login } from './Login';

const READY = { account_configured: true, locked: false, remaining: 3, max_failures: 3, setup_command: null };

/** GET（無 body）回登入狀態，POST 依序回 `attempts`。 */
function setup(state: Record<string, unknown>, attempts: Reply[] = []) {
  const queue = [...attempts];
  const { api, callsTo } = makeApi({
    '/ui/api/login': (body) => (Object.keys(body).length === 0 ? json(state) : queue.shift()!),
  });
  const onLoggedIn = vi.fn();
  render(<Login api={api} expired={false} onLoggedIn={onLoggedIn} />);
  return { callsTo, onLoggedIn };
}

function fill(username: string, password: string) {
  fireEvent.input(screen.getByLabelText('帳號'), { target: { value: username } });
  fireEvent.input(screen.getByLabelText('密碼'), { target: { value: password } });
  fireEvent.click(screen.getByRole('button', { name: '登入' }));
}

const disabled = (el: HTMLElement) => (el as HTMLInputElement | HTMLButtonElement).disabled;

afterEach(cleanup);

describe('Login', () => {
  it('送出帳號密碼，成功後通知並清掉密碼', async () => {
    const { callsTo, onLoggedIn } = setup(READY, [json({})]);
    await screen.findByText(/目前剩餘 3 次/);
    fill('UEPBernie', 'correct horse battery');
    await waitFor(() => expect(onLoggedIn).toHaveBeenCalledTimes(1));
    const posts = callsTo('/ui/api/login').filter((c) => Object.keys(c.body).length > 0);
    expect(posts[0]!.body).toEqual({ username: 'UEPBernie', password: 'correct horse battery' });
    expect((screen.getByLabelText('密碼') as HTMLInputElement).value).toBe('');
  });

  it('失敗顯示剩餘次數', async () => {
    setup(READY, [apiError(401, 'invalid_credentials', { remaining: 2, max_failures: 3, locked: false })]);
    await screen.findByText(/目前剩餘 3 次/);
    fill('UEPBernie', 'wrong');
    expect((await screen.findByRole('alert')).textContent).toContain(
      '帳號或密碼錯誤，剩餘 2 次；失敗 3 次將鎖定，需人工解鎖',
    );
    expect(screen.getByText(/目前剩餘 2 次/)).toBeTruthy();
  });

  it('第 3 次失敗後顯示鎖定、停用登入按鈕', async () => {
    setup({ ...READY, remaining: 1 }, [apiError(423, 'locked', { remaining: 0, max_failures: 3, locked: true })]);
    await screen.findByText(/目前剩餘 1 次/);
    fill('UEPBernie', 'wrong');
    expect((await screen.findByRole('alert')).textContent).toContain(LOCKED_MESSAGE);
    expect(disabled(screen.getByRole('button', { name: '登入' }))).toBe(true);
  });

  it('已鎖定：一進頁面就顯示鎖定訊息且不能送出', async () => {
    const { callsTo } = setup({ ...READY, locked: true, remaining: 0 });
    expect((await screen.findByRole('alert')).textContent).toContain(LOCKED_MESSAGE);
    fireEvent.input(screen.getByLabelText('帳號'), { target: { value: 'UEPBernie' } });
    fireEvent.input(screen.getByLabelText('密碼'), { target: { value: 'x' } });
    expect(disabled(screen.getByRole('button', { name: '登入' }))).toBe(true);
    expect(callsTo('/ui/api/login')).toHaveLength(1);
  });

  it('尚未設定帳號：顯示主機指令、欄位停用', async () => {
    setup({ ...READY, account_configured: false, setup_command: 'docker exec -it lore-vault … ui-set-password' });
    const hint = await screen.findByTestId('login-no-account');
    expect(hint.textContent).toContain('尚未設定帳號');
    expect(hint.textContent).toContain('ui-set-password');
    expect(disabled(screen.getByLabelText('帳號'))).toBe(true);
    expect(disabled(screen.getByRole('button', { name: '登入' }))).toBe(true);
  });
});
