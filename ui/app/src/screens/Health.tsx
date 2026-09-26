// 系統健康（T-83）：`/v1/status` 的 doctor 報告依分類列出（fail／warn 醒目、可展開明細）、
// 背景補算與文件抽取積壓、最近備份、暖機狀態，以及各機器收料新鮮度（`episode_summary`）。
// 兩個請求各自成敗：收料概況失敗不遮蔽健檢結果，反之亦然。
import { useEffect, useState } from 'preact/hooks';

import { Banner, EmptyState, ErrorState, Loading } from '../components/ui';
import { useApp } from '../lib/context';
import { describeError, formatTime, isAbort, stripInternalRefs } from '../lib/format';
import {
  EPISODE_STALE_HOURS,
  STATUS_LABEL,
  formatAge,
  groupChecks,
  healthBadge,
  CLIENT_DOCTOR_COMMAND,
  hoursSince,
  parseBackupDetail,
  splitClientChecks,
  statusRank,
} from '../lib/health';
import type { BacklogStatus, CheckStatus, DoctorCheck, EpisodeSummary, StatusResult, WorkerStatus } from '../lib/types';

const WARMUP_LABEL: Record<string, string> = {
  disabled: '未啟用暖機',
  pending: '等待暖機',
  running: '暖機中',
  ok: '已就緒',
  failed: '暖機失敗（語意檢索可能降級）',
};

const COUNT_TEXT: Record<string, string> = {
  summary_pending: '摘要待補',
  embedding_pending: '向量待補',
  summary_failed: '摘要失敗',
  embedding_failed: '向量失敗',
  documents_pending: '文件待抽取',
  chunks_missing_vectors: '段落缺向量',
  oldest_age_seconds: '最舊積壓（秒）',
};

export function Health() {
  const { api, space, reportHealth } = useApp();
  const [status, setStatus] = useState<StatusResult | null>(null);
  const [statusError, setStatusError] = useState<unknown>(null);
  const [episodes, setEpisodes] = useState<EpisodeSummary | null>(null);
  const [episodeError, setEpisodeError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setStatusError(null);
    setEpisodeError(null);
    const s = api
      .post<StatusResult>('/v1/status', { space: space.id }, ctrl.signal)
      .then(({ data }) => {
        setStatus(data);
        reportHealth(healthBadge(data));
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setStatusError(err);
        reportHealth({ ok: false, fail: 0, warn: 0, checkedAt: null, error: describeError(err) });
      });
    // episode 只屬於 dev：收料新鮮度固定查 dev 全部 vault
    const e = api
      .post<EpisodeSummary>('/v1/episode_summary', { space: 'dev', vault: '*' }, ctrl.signal)
      .then(({ data }) => setEpisodes(data))
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setEpisodeError(err);
      });
    void Promise.allSettled([s, e]).then(() => {
      if (!ctrl.signal.aborted) setLoading(false);
    });
    return () => ctrl.abort();
  }, [api, space.id, tick]);

  const refresh = () => setTick((t) => t + 1);

  return (
    <section class="lv-screen lv-screen--wide">
      <div class="lv-screen__head">
        <div>
          <div class="lv-eyebrow">
            DOCTOR · 全系統對帳{status ? ` · ${formatTime(status.checked_at)}` : ''}
          </div>
          <h1 class="lv-title lv-title--tight">系統健康</h1>
        </div>
        <button type="button" class="btn-outline btn-outline--gold" disabled={loading} onClick={refresh}>
          {loading ? '檢查中…' : '重新整理'}
        </button>
      </div>

      {loading && !status && !statusError && <Loading label="正在執行健檢…" />}
      {statusError !== null && <ErrorState error={statusError} onRetry={refresh} />}
      {status && statusError !== null && (
        <p class="lv-notice lv-notice--warn" role="status">
          以下是上一次成功取得的結果（{formatTime(status.checked_at)}）。
        </p>
      )}

      {status && <StatusView status={status} />}

      <section class="lv-section" aria-labelledby="health-machines">
        <h2 class="lv-section__title" id="health-machines">
          收料新鮮度
        </h2>
        <p class="lv-section__desc">
          依機器列出最近一次收到 episode 的時間（episode 只屬於 dev）。超過 {EPISODE_STALE_HOURS} 小時標紅（暫定門檻）。
        </p>
        {episodeError !== null && <ErrorState error={episodeError} onRetry={refresh} />}
        {episodes && <MachineList summary={episodes} />}
      </section>
    </section>
  );
}

function StatusView({ status }: { status: StatusResult }) {
  const doctor = status.doctor;
  const { server, client } = splitClientChecks(doctor.checks);
  // 客戶端檢查在服務端必然略過：不算進「SKIP」，免得看起來像設定缺漏
  const clientSkipped = client.filter((c) => c.status === 'skipped').length;
  const counts: Record<CheckStatus, number> = {
    fail: doctor.summary.fail ?? 0,
    warn: doctor.summary.warn ?? 0,
    skipped: Math.max(0, (doctor.summary.skipped ?? 0) - clientSkipped),
    pass: doctor.summary.pass ?? 0,
  };
  const groups = groupChecks(server);
  const backup = doctor.checks.find((c) => c.name === 'backup.recent') ?? null;
  const fatal = [
    ['摘要／向量 worker', status.enrich.worker],
    ['文件抽取 worker', status.documents.worker],
  ].filter(([, w]) => (w as WorkerStatus).fatal_error) as [string, WorkerStatus][];
  const schemaMismatch = status.schema.version !== status.schema.expected;

  return (
    <>
      {!status.ok && (
        <Banner tone="error" label="UNHEALTHY" title="系統健檢未通過" testId="health-unhealthy">
          {counts.fail > 0 ? `${counts.fail} 項檢查失敗。` : ''}
          {fatal.map(([label, w]) => `${label} 無法執行：${w.fatal_error}。`).join(' ')}
        </Banner>
      )}
      {schemaMismatch && (
        <Banner tone="error" label="SCHEMA" title="資料庫 schema 版本與服務不符">
          目前 v{status.schema.version}，服務預期 v{status.schema.expected}。
        </Banner>
      )}

      <div class="lv-health-counts" role="list" aria-label="檢查結果計數">
        {(['fail', 'warn', 'skipped', 'pass'] as const).map((k) => (
          <div key={k} role="listitem" class={`lv-health-count lv-health-count--${k}` + (counts[k] > 0 ? ' is-nonzero' : '')} data-testid={`count-${k}`}>
            <div class="lv-health-count__label">{STATUS_LABEL[k]}</div>
            <div class="lv-health-count__n">{counts[k]}</div>
          </div>
        ))}
      </div>

      <div class="lv-health-grid">
        <div class="lv-health-groups">
          {groups.map((g) => (
            <section key={g.category} class={`lv-check-group lv-check-group--${g.worst}`} data-category={g.category}>
              <h2 class="lv-check-group__title">{g.category}</h2>
              {g.checks.map((c) => (
                <CheckRow key={c.name} check={c} />
              ))}
            </section>
          ))}
          {client.length > 0 && <ClientChecks checks={client} />}
        </div>

        <aside class="lv-health-side">
          <div class="lv-side-block">
            <div class="lv-side-block__label">背景補算</div>
            <BacklogView title="摘要與向量" backlog={status.enrich.backlog} worker={status.enrich.worker} />
            {status.documents.enabled ? (
              <BacklogView title="文件抽取" backlog={status.documents.backlog} worker={status.documents.worker} />
            ) : (
              <p class="lv-muted lv-small">文件儲存未設定（documents.blob_dir），不收文件。</p>
            )}
            <p class="lv-hint lv-hint--inline">補算失敗目前沒有手動重試端點；失敗項於重試上限後停止。</p>
          </div>
          <div class="lv-side-block">
            <div class="lv-side-block__label">服務狀態</div>
            <dl class="lv-kv lv-kv--stack">
              <dt>最近備份</dt>
              <dd data-testid="health-backup">
                <BackupView check={backup} />
              </dd>
              <dt>語意模型</dt>
              <dd data-testid="health-warmup" class={status.embedding.warmup.status === 'failed' ? 'lv-text-error' : undefined}>
                {WARMUP_LABEL[status.embedding.warmup.status] ?? status.embedding.warmup.status}
                {status.embedding.warmup.error && <span class="lv-status__raw">{status.embedding.warmup.error}</span>}
              </dd>
              <dt>schema</dt>
              <dd>v{status.schema.version}</dd>
            </dl>
          </div>
        </aside>
      </div>
    </>
  );
}

/** 客戶端檢查：預設收合的一組，說明要到 agent 機器上執行；有非略過的結果時照常標示狀態。 */
function ClientChecks({ checks }: { checks: DoctorCheck[] }) {
  const ran = checks.filter((c) => c.status !== 'skipped');
  const worst = ran.map((c) => c.status).sort((a, b) => statusRank(a) - statusRank(b))[0] ?? 'client';
  return (
    <details class={`lv-check-group lv-check-group--client lv-check-group--${worst}`} data-testid="client-checks" open={worst === 'fail'}>
      <summary class="lv-check-group__title lv-check-group__summary">
        客戶端檢查 · {checks.length} 項
        <span class="lv-check-group__note">{ran.length === 0 ? '在 agent 機器上執行' : `${ran.length} 項有結果`}</span>
      </summary>
      <div class="lv-client-note">
        <p>
          這些檢查看的是 agent 機器上的快照、spool 與 client.env，服務端沒有這些目錄，所以在這裡不會執行——不是設定缺漏。
          請在 agent 機器上以 doctor 執行：
        </p>
        <pre class="lv-md__pre lv-client-note__cmd">{CLIENT_DOCTOR_COMMAND}</pre>
      </div>
      {checks.map((c) => (
        <CheckRow key={c.name} check={c} client />
      ))}
    </details>
  );
}

function CheckRow({ check, client = false }: { check: DoctorCheck; client?: boolean }) {
  const hasMore = check.details.length > 0 || Object.keys(check.counts).length > 0;
  // 客戶端檢查在服務端略過是預期的：標成「AGENT」並顯示檢查用途，不顯示「缺少設定」
  const clientSkip = client && check.status === 'skipped';
  const tone = clientSkip ? 'skipped' : check.status in STATUS_LABEL ? check.status : 'unknown';
  const label = clientSkip ? 'AGENT' : (STATUS_LABEL[check.status as CheckStatus] ?? check.status.toUpperCase());
  const head = (
    <>
      <span class={`lv-check__status lv-check__status--${tone}`}>{label}</span>
      <span class="lv-check__main">
        <span class="lv-check__name lv-mono">{check.name}</span>
        <span class="lv-check__desc">{stripInternalRefs(clientSkip ? check.description || check.summary : check.summary || check.description)}</span>
      </span>
    </>
  );
  if (!hasMore) {
    return (
      <div class="lv-check" data-status={check.status} data-testid={`check-${check.name}`}>
        <div class="lv-check__head">{head}</div>
      </div>
    );
  }
  return (
    <details
      class="lv-check"
      data-status={check.status}
      data-testid={`check-${check.name}`}
      open={check.status === 'fail'}
    >
      <summary class="lv-check__head">{head}</summary>
      <div class="lv-check__body">
        {check.description && check.summary && <p class="lv-muted lv-small">{stripInternalRefs(check.description)}</p>}
        {Object.keys(check.counts).length > 0 && (
          <ul class="lv-plan__counts">
            {Object.entries(check.counts).map(([k, n]) => (
              <li key={k}>
                <span class="lv-mono">{k}</span>
                <span class="lv-mono">{n}</span>
              </li>
            ))}
          </ul>
        )}
        {check.details.length > 0 && (
          <ul class="lv-check__details">
            {check.details.map((d, i) => (
              <li key={i} class="lv-mono">
                {d}
              </li>
            ))}
          </ul>
        )}
      </div>
    </details>
  );
}

function BacklogView({ title, backlog, worker }: { title: string; backlog: BacklogStatus; worker: WorkerStatus }) {
  const entries = Object.entries(backlog.counts).filter(([k]) => k !== 'oldest_age_seconds');
  const bad = backlog.status !== 'pass';
  return (
    <div class="lv-backlog" data-status={backlog.status}>
      <div class="lv-backlog__head">
        <span>{title}</span>
        <span class={'lv-mono ' + (bad ? 'lv-text-error' : 'lv-muted')}>{backlog.status.toUpperCase()}</span>
      </div>
      <p class="lv-small">{stripInternalRefs(backlog.summary)}</p>
      {entries.length > 0 && (
        <ul class="lv-plan__counts">
          {entries.map(([k, n]) => (
            <li key={k}>
              <span>{COUNT_TEXT[k] ?? k}</span>
              <span class="lv-mono">{n}</span>
            </li>
          ))}
        </ul>
      )}
      <p class="lv-small lv-muted">
        worker：{!worker.enabled ? '未啟用' : worker.running ? '執行中' : '已停止'}
        {worker.last_run ? ` · 最近一輪 ${formatTime(worker.last_run)}` : ''}
      </p>
      {worker.fatal_error && <p class="lv-small lv-text-error">無法執行：{worker.fatal_error}</p>}
      {worker.last_error && <p class="lv-small lv-text-warn">最近錯誤：{worker.last_error}</p>}
    </div>
  );
}

function BackupView({ check }: { check: DoctorCheck | null }) {
  if (!check) return <span class="lv-muted">服務未回報備份檢查</span>;
  if (check.status === 'skipped') return <span class="lv-muted">未檢查：{stripInternalRefs(check.summary)}</span>;
  const last = parseBackupDetail(check.details.find((d) => d.startsWith('最近一次')) ?? null);
  return (
    <span class={check.status === 'pass' ? undefined : 'lv-text-error'}>
      {stripInternalRefs(check.summary)}
      {last && (
        <span class="lv-status__raw" data-testid="health-backup-last">
          最近一次：{last.time}
          {last.file && <span class="lv-backup-file lv-mono">{last.file}</span>}
        </span>
      )}
    </span>
  );
}

function MachineList({ summary }: { summary: EpisodeSummary }) {
  if (summary.by_machine.length === 0) {
    return (
      <EmptyState size="sm" title="還沒有收到任何 episode">agent 機器開始收料後，這裡會依機器列出最近收料時間。</EmptyState>
    );
  }
  return (
    <ul class="lv-machines" data-testid="machines">
      {summary.by_machine.map((m) => {
        const age = hoursSince(m.last_recorded);
        const stale = age === null || age > EPISODE_STALE_HOURS;
        return (
          <li key={m.machine ?? '(未知)'} class={'lv-machine' + (stale ? ' is-stale' : '')} data-stale={String(stale)}>
            <span class="lv-status__dot" aria-hidden="true" />
            <span class="lv-machine__name lv-mono">{m.machine ?? '（未記錄機器）'}</span>
            <span class="lv-machine__n lv-mono">{m.episodes} 輪</span>
            <span class="lv-machine__last">
              {formatAge(age)}
              {stale && <span class="lv-visually-hidden">（過久未收料）</span>}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
