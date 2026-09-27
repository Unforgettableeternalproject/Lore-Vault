// 設定頁（T-85、D13）：
// - 目前連線：同源部署，沒有「服務位址」與密碼欄位（A23：帳號密碼只在登入頁輸入、session 存在服務端）。
//   顯示目前連線的服務、登入帳號與顯示名稱、session 期限、測試連線、登出。
// - 服務設定：可在執行期安全調整的服務設定（GET /v1/settings），依分類呈現目前值、來源（預設／已覆寫）與預設值；
//   逐欄驗證、只送出有變動的項目、單項還原預設；最近的修改紀錄。只有登入 UI 的管理者能看與改。
// 深淺色切換只在頂列（手機在抽屜），這頁不重複放。
import { useEffect, useMemo, useState } from 'preact/hooks';

import { Badge, Banner, ErrorState, Loading } from '../components/ui';
import { ApiError } from '../lib/api';
import { useApp } from '../lib/context';
import { describeError, formatTime, isAbort } from '../lib/format';
import { checkSession, type SessionInfo } from '../lib/session';
import type {
  SettingError,
  SettingItem,
  SettingsAuditEntry,
  SettingsResult,
  SettingValue,
  StatusResult,
} from '../lib/types';

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
      <div class="lv-eyebrow">SETTINGS</div>
      <h1 class="lv-title">連線與服務設定</h1>

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

      <ServiceSettings />
    </section>
  );
}

// ── 服務設定 ──

/** 編輯中的值：開關為 boolean，數字保留輸入字串（驗證與送出時才轉換） */
type Draft = Record<string, boolean | string>;

function formatValue(item: Pick<SettingItem, 'type' | 'unit'>, value: SettingValue): string {
  if (typeof value === 'boolean') return value ? '開啟' : '關閉';
  const text = Number.isInteger(value) ? value.toLocaleString('en-US') : String(value);
  return item.unit ? `${text} ${item.unit}` : text;
}

function draftOf(item: SettingItem): boolean | string {
  return typeof item.value === 'boolean' ? item.value : String(item.value);
}

/** 前端先檢查（與服務端同一份規則：型別、整數、範圍）；通過回正規化後的值。服務端仍會再驗一次。 */
function checkDraft(item: SettingItem, draft: boolean | string): { value: SettingValue } | { error: string } {
  if (item.type === 'bool') {
    return typeof draft === 'boolean' ? { value: draft } : { error: '必須是開或關' };
  }
  const text = String(draft).trim();
  if (text === '') return { error: '請輸入數字' };
  const number = Number(text);
  if (!Number.isFinite(number)) return { error: '必須是數字' };
  if (item.type === 'int' && !Number.isInteger(number)) return { error: '必須是整數' };
  if (item.min !== null && number < item.min) return { error: `不可小於 ${formatValue(item, item.min)}` };
  if (item.max !== null && number > item.max) return { error: `不可大於 ${formatValue(item, item.max)}` };
  return { value: number };
}

function settingErrors(err: unknown): SettingError[] {
  if (!(err instanceof ApiError) || err.code !== 'invalid_setting') return [];
  const list = (err.body as { error?: { errors?: unknown } } | null)?.error?.errors;
  return Array.isArray(list) ? (list.filter((e) => e && typeof e === 'object' && typeof (e as SettingError).key === 'string') as SettingError[]) : [];
}

function ServiceSettings() {
  const { api, toast } = useApp();
  const [data, setData] = useState<SettingsResult | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [draft, setDraft] = useState<Draft>({});
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const apply = (result: SettingsResult) => {
    setData(result);
    setDraft(Object.fromEntries(result.items.map((i) => [i.key, draftOf(i)])));
    setServerErrors({});
    setSaveError(null);
  };

  const load = (signal?: AbortSignal) => {
    setLoadError(null);
    api
      .get<SettingsResult>('/v1/settings', signal)
      .then(({ data: result }) => {
        if (!signal?.aborted) apply(result);
      })
      .catch((err) => {
        if (isAbort(err) || signal?.aborted) return;
        setLoadError(err);
      });
  };

  useEffect(() => {
    const ctrl = new AbortController();
    load(ctrl.signal);
    return () => ctrl.abort();
  }, [api]);

  const items = data?.items ?? [];
  const checks = useMemo(
    () => Object.fromEntries(items.map((i) => [i.key, checkDraft(i, draft[i.key] ?? draftOf(i))])),
    [items, draft],
  );
  const changed = items.filter((i) => {
    const check = checks[i.key];
    if (!check) return false;
    if ('error' in check) return String(draft[i.key]) !== String(draftOf(i));
    return check.value !== i.value;
  });
  const hasErrors = changed.some((i) => 'error' in (checks[i.key] ?? {}));
  /** 顯示在欄位下的錯誤：服務端逐項錯誤優先，其次是有改動的欄位的前端檢查 */
  const errorFor = (item: SettingItem): string | null => {
    if (serverErrors[item.key]) return serverErrors[item.key]!;
    const check = checks[item.key];
    return check && 'error' in check && changed.includes(item) ? check.error : null;
  };

  const save = async () => {
    const values: Record<string, SettingValue> = {};
    for (const item of changed) {
      const check = checks[item.key];
      if (!check || 'error' in check) return;
      values[item.key] = check.value;
    }
    if (Object.keys(values).length === 0) return;
    setBusy(true);
    setSaveError(null);
    setServerErrors({});
    try {
      const { data: result } = await api.post<SettingsResult>('/v1/settings_update', { values });
      apply(result);
      toast(`已儲存 ${result.changed?.length ?? 0} 項設定，立即生效`);
    } catch (err) {
      const perKey = settingErrors(err);
      if (perKey.length) setServerErrors(Object.fromEntries(perKey.map((e) => [e.key, e.message])));
      setSaveError(err);
    } finally {
      setBusy(false);
    }
  };

  const reset = async (item: SettingItem) => {
    setBusy(true);
    setSaveError(null);
    try {
      const { data: result } = await api.post<SettingsResult>('/v1/settings_reset', { keys: [item.key] });
      apply(result);
      toast(`「${item.label}」已還原為預設值`);
    } catch (err) {
      setSaveError(err);
    } finally {
      setBusy(false);
    }
  };

  const labelOf = (key: string) => items.find((i) => i.key === key)?.label ?? key;

  return (
    <section class="lv-section lv-svc" aria-labelledby="settings-service">
      <h2 class="lv-section__title" id="settings-service">
        服務設定
      </h2>
      <p class="lv-section__desc">
        這些設定可以在服務執行中調整，儲存後立即生效，不需重啟。預設值來自服務的設定檔或環境變數；在這裡改過的項目會標示「已覆寫」，可隨時還原。每次修改都會記下修改者與時間。
      </p>
      {loadError !== null && <ErrorState error={loadError} onRetry={() => load()} />}
      {!data && loadError === null && <Loading />}
      {data && data.invalid_overrides.length > 0 && (
        <Banner tone="error" label="INVALID" title="有設定覆寫不合法，服務已略過、改用預設值" testId="settings-invalid">
          {data.invalid_overrides.map((o) => `${labelOf(o.key)}：${o.reason}`).join('；')}。在這裡重新儲存或還原該項即可修正。
        </Banner>
      )}
      {data && (
        <form
          class="lv-svc__form"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          {data.categories
            .filter((c) => items.some((i) => i.category === c.id))
            .map((category) => (
              <fieldset key={category.id} class="lv-svc__group" data-testid={`settings-group-${category.id}`}>
                <legend class="lv-svc__legend">{category.label}</legend>
                {items
                  .filter((i) => i.category === category.id)
                  .map((item) => (
                    <SettingRow
                      key={item.key}
                      item={item}
                      draft={draft[item.key] ?? draftOf(item)}
                      error={errorFor(item)}
                      busy={busy}
                      onChange={(value) => setDraft((prev) => ({ ...prev, [item.key]: value }))}
                      onReset={() => void reset(item)}
                    />
                  ))}
              </fieldset>
            ))}
          {saveError !== null && Object.keys(serverErrors).length === 0 && <ErrorState error={saveError} />}
          <div class="lv-actions lv-actions--wrap">
            <button type="submit" class="btn-outline btn-outline--gold" disabled={busy || changed.length === 0 || hasErrors}>
              {busy ? '儲存中…' : changed.length ? `儲存 ${changed.length} 項變更` : '沒有變更'}
            </button>
            {changed.length > 0 && (
              <button type="button" class="btn-outline" disabled={busy} onClick={() => apply(data)}>
                放棄變更
              </button>
            )}
          </div>
        </form>
      )}
      {data && <AuditList entries={data.audit} items={items} />}
    </section>
  );
}

function SettingRow({
  item,
  draft,
  error,
  busy,
  onChange,
  onReset,
}: {
  item: SettingItem;
  draft: boolean | string;
  error: string | null;
  busy: boolean;
  onChange: (value: boolean | string) => void;
  onReset: () => void;
}) {
  const id = `setting-${item.key.replace(/[^a-z0-9]+/gi, '-')}`;
  const describedBy = `${id}-desc${error ? ` ${id}-error` : ''}`;
  const overridden = item.source === 'override';
  return (
    <div class={'lv-svc__row' + (error ? ' is-invalid' : '')} data-testid={`setting-${item.key}`}>
      <div class="lv-svc__head">
        {item.type === 'bool' ? (
          <label class="lv-svc__toggle" for={id}>
            <input
              id={id}
              type="checkbox"
              checked={draft === true}
              disabled={busy}
              aria-describedby={describedBy}
              onChange={(e) => onChange((e.target as HTMLInputElement).checked)}
            />
            <span class="lv-svc__label">{item.label}</span>
          </label>
        ) : (
          <label class="lv-svc__label" for={id}>
            {item.label}
          </label>
        )}
        <Badge tone={overridden ? 'warn' : 'plain'} label="來源" testId={`setting-source-${item.key}`}>
          {overridden ? '已覆寫' : '預設'}
        </Badge>
      </div>
      {item.type !== 'bool' && (
        <div class="lv-svc__input">
          <input
            id={id}
            class="lv-input"
            type="number"
            inputMode={item.type === 'int' ? 'numeric' : 'decimal'}
            step={item.type === 'int' ? 1 : 'any'}
            min={item.min ?? undefined}
            max={item.max ?? undefined}
            value={String(draft)}
            disabled={busy}
            aria-invalid={error ? true : undefined}
            aria-describedby={describedBy}
            onInput={(e) => onChange((e.target as HTMLInputElement).value)}
          />
          {item.unit && <span class="lv-svc__unit">{item.unit}</span>}
        </div>
      )}
      {error && (
        <p class="lv-svc__error" id={`${id}-error`} role="alert">
          {error}
        </p>
      )}
      <p class="lv-svc__desc" id={`${id}-desc`}>
        {item.description}
        {item.type !== 'bool' && item.min !== null && item.max !== null && ` 範圍 ${formatValue(item, item.min)}～${formatValue(item, item.max)}。`}
      </p>
      <div class="lv-svc__meta">
        <span>預設：{formatValue(item, item.default)}</span>
        {overridden && item.override && (
          <span>
            {item.override.updated_by} 於 {formatTime(item.override.updated)} 改為 {formatValue(item, item.value)}
          </span>
        )}
        {overridden && (
          <button type="button" class="lv-link-btn" disabled={busy} onClick={onReset}>
            還原預設
          </button>
        )}
      </div>
    </div>
  );
}

function AuditList({ entries, items }: { entries: SettingsAuditEntry[]; items: SettingItem[] }) {
  const byKey = new Map(items.map((i) => [i.key, i]));
  return (
    <div class="lv-svc__audit">
      <h3 class="lv-svc__audit-title">最近的修改</h3>
      {entries.length === 0 ? (
        <p class="lv-hint lv-hint--inline">還沒有任何修改，全部使用預設值。</p>
      ) : (
        <ul class="lv-svc__audit-list" data-testid="settings-audit">
          {entries.map((e) => {
            const item = byKey.get(e.key);
            const fmt = (v: SettingValue) => (item ? formatValue(item, v) : String(v));
            return (
              <li key={e.seq}>
                <span class="lv-svc__audit-time">{formatTime(e.at)}</span>
                <span>
                  {e.display ?? e.principal}
                  {e.action === 'reset' ? ' 還原' : ' 修改'}「{item?.label ?? e.key}」：{fmt(e.old_value)} → {fmt(e.new_value)}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
