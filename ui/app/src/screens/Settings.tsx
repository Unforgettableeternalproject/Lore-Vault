// 連線設定（T-85）：同源部署，沒有「服務位址」與密碼欄位（A23：帳號密碼只在登入頁輸入、session 存在服務端）。
// 顯示目前連線的服務、登入帳號與顯示名稱、session 期限、測試連線、登出。
// 深淺色切換只在頂列（手機在抽屜），這頁不重複放。
import { useEffect, useState } from 'preact/hooks';

import { ErrorState, Loading } from '../components/ui';
import { useApp } from '../lib/context';
import { describeError, formatTime, isAbort } from '../lib/format';
import { checkSession, type SessionInfo } from '../lib/session';
import type { StatusResult } from '../lib/types';

interface TestRow {
  ok: boolean | null;
  label: string;
  value: string;
}

export function Settings({ onLogout }: { onLogout: () => void }) {
  const { api } = useApp();
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [sessionError, setSessionError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [rows, setRows] = useState<TestRow[] | null>(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    checkSession(api)
      .then((info) => {
        if (ctrl.signal.aborted) return;
        setSession(info);
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setSessionError(err);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api]);

  const runTest = async () => {
    setTesting(true);
    const result: TestRow[] = [];
    try {
      const info = await checkSession(api);
      setSession(info);
      result.push({ ok: info !== null, label: 'session', value: info ? `有效，到 ${formatTime(info.expires_at)}` : '已失效' });
    } catch (err) {
      result.push({ ok: false, label: 'session', value: describeError(err) });
    }
    try {
      const { data } = await api.post<StatusResult>('/v1/status');
      result.push({ ok: true, label: '服務', value: `可連線 · schema v${data.schema.version}` });
      result.push({
        ok: data.ok,
        label: '健檢',
        value: data.ok ? '通過' : `未通過（${data.doctor.summary.fail ?? 0} 項失敗）`,
      });
      const warm = data.embedding.warmup.status;
      result.push({
        ok: warm === 'ok' ? true : warm === 'failed' ? false : null,
        label: '語意模型',
        value: warm === 'ok' ? '已就緒' : warm === 'failed' ? `暖機失敗：${data.embedding.warmup.error ?? '原因不明'}` : warm,
      });
    } catch (err) {
      result.push({ ok: false, label: '服務', value: describeError(err) });
    }
    setRows(result);
    setTesting(false);
  };

  return (
    <section class="lv-screen lv-settings">
      <div class="lv-eyebrow">CONNECTION</div>
      <h1 class="lv-title">連線設定</h1>

      <section class="lv-section" aria-labelledby="settings-conn">
        <h2 class="lv-section__title" id="settings-conn">
          目前連線
        </h2>
        {loading && <Loading />}
        {sessionError !== null && <ErrorState error={sessionError} />}
        <dl class="lv-kv lv-kv--roomy" data-testid="settings-session">
          <dt>服務</dt>
          <dd class="lv-mono">{window.location.origin}</dd>
          <dt>帳號</dt>
          <dd class="lv-mono" data-testid="settings-principal">
            {session?.principal ?? (loading ? '…' : '服務未回報')}
          </dd>
          <dt>顯示名稱</dt>
          <dd data-testid="settings-display">{session?.display_name ?? (loading ? '…' : '服務未回報')}</dd>
          <dt>session 到期</dt>
          <dd>{session ? formatTime(session.expires_at) : '—'}</dd>
          <dt>閒置到期</dt>
          <dd>{session ? formatTime(session.idle_expires_at) : '—'}</dd>
        </dl>
        <p class="lv-hint lv-hint--inline">
          UI 與服務同源部署；密碼只在登入時送出一次，瀏覽器只持有 HttpOnly session cookie。服務重啟後 session 失效，需重新登入。帳號密碼在主機以 cli.admin ui-set-password 設定。
        </p>
        <div class="lv-actions">
          <button type="button" class="btn-outline btn-outline--gold" disabled={testing} onClick={() => void runTest()}>
            {testing ? '測試中…' : '測試連線'}
          </button>
          <button type="button" class="btn-outline" onClick={onLogout}>
            登出
          </button>
        </div>
        {rows && (
          <ul class="lv-test-rows" data-testid="settings-test">
            {rows.map((r) => (
              <li key={r.label} class={'lv-test-row' + (r.ok === false ? ' is-fail' : r.ok ? ' is-ok' : ' is-unknown')}>
                <span class="lv-test-row__mark" aria-hidden="true">
                  {r.ok === false ? '✕' : r.ok ? '✓' : '·'}
                </span>
                <span class="lv-test-row__label">{r.label}</span>
                <span class="lv-test-row__value">{r.value}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

    </section>
  );
}
