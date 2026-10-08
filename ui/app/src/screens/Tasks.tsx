// 任務層（TASK_LAYER_UI，唯讀）：各 repo 的 change 推導狀態，來源是任務層 CLI 推送到通用側載的快照
// （`/v1/blob_get` key `tasks-snapshot`）。狀態改變只能在本機以 CLI 操作，這裡不提供任何動作。
// 任務層只屬於 dev space，其他 space 只顯示說明（比照記憶層）。
import { useEffect, useState } from 'preact/hooks';

import { ChipGroup, FilterPanel } from '../components/Filters';
import { Badge, Banner, EmptyState, ErrorState, Loading } from '../components/ui';
import { VaultPicker } from '../components/VaultPicker';
import { ALL, useApp, vaultName } from '../lib/context';
import { formatTime, isAbort } from '../lib/format';
import { formatAge, hoursSince } from '../lib/health';
import { Markdown } from '../lib/markdown';
import { routePath } from '../lib/router';
import {
  blockerLabel,
  fetchTaskSnapshots,
  filterRows,
  groupCounts,
  isStale,
  statusGroup,
  statusView,
  STATUS_DONE,
  STATUS_UNKNOWN,
  taskRows,
  TASK_STALE_HOURS,
  type TaskFilter,
  type TaskGroup,
  type TaskRow,
  type VaultSnapshot,
} from '../lib/tasks';
import type { GetResult, NoteFull, TaskChange } from '../lib/types';

export function Tasks({ params = [] }: { params?: string[] }) {
  const { space, switchSpace } = useApp();
  const [vaultKey, name] = params;
  // 詳情頁比照筆記詳情以麵包屑開頭，不再疊一行 eyebrow
  const eyebrow = <div class="lv-eyebrow">TASK LAYER · 唯讀 · {space.en}</div>;
  return (
    <section class="lv-screen lv-screen--wide">
      {space.id !== 'dev' ? (
        <>
          {eyebrow}
          <h1 class="lv-title">任務</h1>
          <EmptyState
            testId="tasks-dev-only"
            title={`${space.en} space 沒有任務層`}
            action={
              <button type="button" class="btn-outline btn-outline--gold" onClick={() => switchSpace('dev', routePath('tasks'))}>
                切換到 DEV 檢視
              </button>
            }
          >
            任務層（OpenSpec 格式的 change 與推導狀態）只屬於 dev space：它記錄的是各 repo 的工程變更。
          </EmptyState>
        </>
      ) : vaultKey && name ? (
        <TaskDetail vaultKey={vaultKey} name={name} />
      ) : (
        <>
          {eyebrow}
          <h1 class="lv-title">任務</h1>
          <p class="lv-section__desc">
            各 repo 任務層的 change 與推導狀態，內容是任務層 CLI 最後一次推送的快照。這裡只能檢視；狀態要在本機用 CLI 改變。
          </p>
          <TaskList />
        </>
      )}
    </section>
  );
}

const GROUP_ORDER: Record<TaskGroup, number> = { ready: 0, auth: 1, blocked: 2, done: 3 };

function useSnapshots(vault: string) {
  const { api, space } = useApp();
  const [data, setData] = useState<VaultSnapshot[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    fetchTaskSnapshots(api, space.id, vault, ctrl.signal)
      .then((result) => {
        setData(result);
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, space.id, vault, tick]);
  return { data, error, loading, retry: () => setTick((t) => t + 1) };
}

function TaskList() {
  const { vault, vaults } = useApp();
  const [filter, setFilter] = useState<TaskFilter>('');
  const [includeDone, setIncludeDone] = useState(false);
  const { data, error, loading, retry } = useSnapshots(vault);

  const rows = data ? taskRows(data).sort((a, b) => GROUP_ORDER[statusGroup(a.change.status)] - GROUP_ORDER[statusGroup(b.change.status)]) : [];
  const counts = groupCounts(rows);
  const visible = filterRows(rows, filter, includeDone);
  const options = [
    { id: '' as const, label: `全部 ${includeDone ? rows.length : rows.length - counts.done}` },
    { id: 'ready' as const, label: `可開工 ${counts.ready}` },
    { id: 'blocked' as const, label: `被擋住 ${counts.blocked}` },
    { id: 'auth' as const, label: `待授權 ${counts.auth}` },
    { id: 'done' as const, label: `已完成 ${counts.done}` },
  ];
  const showVault = vault === ALL;

  return (
    <div class="lv-tasks-view">
      <FilterPanel
        summary={
          <>
            範圍：{vaultName({ vaults }, vault)}
            {filter === '' && !includeDone ? ' · 已完成的 change 已隱藏' : ''}
          </>
        }
      >
        <div class="lv-filter-panel__row">
          <VaultPicker />
          <ChipGroup label="狀態" groupLabel="change 狀態" options={options} value={filter} onChange={setFilter} mono={false} />
          <label class="lv-check">
            <input
              type="checkbox"
              checked={includeDone}
              disabled={filter !== ''}
              onChange={(e) => setIncludeDone((e.target as HTMLInputElement).checked)}
            />
            <span>含已完成</span>
          </label>
        </div>
      </FilterPanel>

      {error !== null && <ErrorState error={error} onRetry={retry} />}
      {loading && !data && <Loading />}
      {data && error === null && <SyncList snapshots={data} showVault={showVault} />}
      {data && error === null && data.length === 0 && (
        <EmptyState testId="tasks-not-synced" title="尚未同步">
          {vault === ALL ? '這個 space 還沒有任何 repo 推送任務層快照。' : '這個 vault 還沒有推送任務層快照。'}
          在 repo 執行任務層的 sync（或 propose／validate／archive／list）後，這裡會顯示最新狀態。
        </EmptyState>
      )}
      {data && error === null && data.length > 0 && rows.length === 0 && data.some((s) => s.snapshot) && (
        <EmptyState testId="tasks-empty" title="快照裡沒有任何 change">任務層目前沒有進行中或已封存的 change。</EmptyState>
      )}
      {rows.length > 0 && visible.length === 0 && (
        <EmptyState testId="tasks-filter-empty" title="沒有符合篩選的 change">
          {filter === '' && !includeDone ? '目前只有已完成的 change；勾選「含已完成」即可列出。' : '換一個狀態或 vault 再試一次。'}
        </EmptyState>
      )}
      {visible.length > 0 && (
        <ul class="lv-tasks" data-testid="tasks" aria-busy={loading} aria-label="change 列表">
          {visible.map((r) => (
            <TaskRowView key={`${r.vault}:${r.change.name}`} row={r} showVault={showVault} />
          ))}
        </ul>
      )}
    </div>
  );
}

/** 各 vault 的同步時間；超過門檻標「可能已過時」，快照格式不對時以錯誤顯示。 */
function SyncList({ snapshots, showVault }: { snapshots: VaultSnapshot[]; showVault: boolean }) {
  const { vaults } = useApp();
  if (snapshots.length === 0) return null;
  return (
    <ul class="lv-task-syncs" data-testid="task-syncs" aria-label="快照同步時間">
      {snapshots.map((s) => {
        const stale = isStale(s.updated);
        return (
          <li key={s.vault} class="lv-task-sync" data-stale={String(stale)}>
            {showVault && (
              <Badge tone="vault" label="vault" title={s.vault}>
                {vaultName({ vaults }, s.vault)}
              </Badge>
            )}
            <span class="lv-task-sync__time" title={formatTime(s.updated)}>
              同步於 {formatAge(hoursSince(s.updated))}
            </span>
            {stale && <StaleBadge />}
            {s.error && (
              <Banner tone="error" label="INVALID" title="快照格式不正確" testId="task-snapshot-invalid">
                {s.error}。請在該 repo 重跑任務層的 sync。
              </Banner>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function StaleBadge() {
  return (
    <Badge tone="warn" label="同步狀態" title={`超過 ${TASK_STALE_HOURS} 小時沒有同步，本機狀態可能已改變`} testId="task-stale">
      可能已過時
    </Badge>
  );
}

function StatusBadge({ status }: { status: string }) {
  const view = statusView(status);
  return (
    <Badge tone={view.tone} label="狀態" testId="task-status">
      {view.label}
    </Badge>
  );
}

function Progress({ tasks }: { tasks: TaskChange['tasks'] }) {
  if (tasks.total <= 0) return <span class="lv-muted lv-small">沒有 tasks</span>;
  return (
    <span class="lv-task-progress">
      <progress class="lv-progress" value={tasks.done} max={tasks.total} aria-label={`tasks 完成 ${tasks.done}／${tasks.total}`} />
      <span class="lv-mono lv-small" data-testid="task-progress">
        {tasks.done}/{tasks.total}
      </span>
    </span>
  );
}

function BlockerBadges({ change }: { change: TaskChange }) {
  return (
    <>
      {change.blocked_by.map((b) => (
        <Badge key={b.id} tone={b.resolved === true ? 'tag' : b.resolved === false ? 'warn' : 'error'} label="阻塞裁決">
          {b.id} {blockerLabel(b.resolved)}
        </Badge>
      ))}
      {change.depends_on.map((d) => (
        <Badge key={d.name} tone={d.archived ? 'tag' : 'warn'} label="依賴 change">
          依賴 {d.name}（{d.archived ? '已封存' : '未封存'}）
        </Badge>
      ))}
    </>
  );
}

function TaskRowView({ row, showVault }: { row: TaskRow; showVault: boolean }) {
  const { vaults, navigate } = useApp();
  const { change } = row;
  const href = routePath('tasks', [row.vault, change.name]);
  return (
    <li class="lv-task" data-status={change.status}>
      <div class="lv-task__main">
        <a
          class="lv-task__name"
          href={href}
          onClick={(e) => {
            e.preventDefault();
            navigate(href);
          }}
        >
          {change.name}
        </a>
        <div class="lv-concept__meta">
          <StatusBadge status={change.status} />
          {change.requires_authorization && change.status !== STATUS_DONE && (
            <Badge tone="auth" label="授權">
              需授權
            </Badge>
          )}
          {showVault && (
            <Badge tone="vault" label="vault" title={row.vault}>
              {vaultName({ vaults }, row.vault)}
            </Badge>
          )}
          {change.source && (
            <Badge tone="kind" label="來源卡">
              {change.source}
            </Badge>
          )}
          <BlockerBadges change={change} />
        </div>
      </div>
      <div class="lv-concept__side">
        <Progress tasks={change.tasks} />
        <Badge tone="time" label="同步" title={formatTime(row.updated)}>
          同步 {formatAge(hoursSince(row.updated))}
        </Badge>
      </div>
    </li>
  );
}

// ── 詳情 ──

function TaskDetail({ vaultKey, name }: { vaultKey: string; name: string }) {
  const { space, vaults, navigate } = useApp();
  const { data, error, loading, retry } = useSnapshots(vaultKey);
  const snap = data?.[0] ?? null;
  const change = snap?.snapshot?.changes.find((c) => c.name === name) ?? null;
  const back = (
    <button type="button" class="btn-outline btn-outline--sm" onClick={() => navigate(routePath('tasks'))}>
      回任務列表
    </button>
  );

  return (
    <div class="lv-detail">
      <article class="lv-detail__main">
        <nav class="lv-crumb" aria-label="位置">
          <button type="button" class="lv-crumb__link" onClick={() => navigate(routePath('tasks'))}>
            {space.en} / 任務
          </button>
          <span aria-hidden="true">/</span>
          <span title={vaultKey}>{vaultName({ vaults }, vaultKey)}</span>
          <span aria-hidden="true">/</span>
          <span>CHANGE</span>
        </nav>
        <h1 class="lv-note-title">{name}</h1>

        {error !== null && <ErrorState error={error} onRetry={retry} />}
        {loading && !data && <Loading />}
        {data && error === null && !snap && (
          <EmptyState testId="tasks-not-synced" title="這個 vault 尚未同步" action={back}>
            服務端沒有這個 vault 的任務層快照。
          </EmptyState>
        )}
        {snap?.error && (
          <Banner tone="error" label="INVALID" title="快照格式不正確" testId="task-snapshot-invalid">
            {snap.error}。請在該 repo 重跑任務層的 sync。
          </Banner>
        )}
        {snap?.snapshot && !change && (
          <EmptyState testId="task-missing" title="快照裡找不到這個 change" action={back}>
            它可能已改名或移除；最後一次同步於 {formatTime(snap.updated)}。
          </EmptyState>
        )}
        {snap && change && <ChangeBody change={change} updated={snap.updated} />}
      </article>
      {snap?.snapshot && change && (
        <aside class="lv-detail__side" aria-label="change 關聯">
          <Relations change={change} vaultKey={vaultKey} others={snap.snapshot.changes} />
        </aside>
      )}
    </div>
  );
}

function ChangeBody({ change, updated }: { change: TaskChange; updated: string }) {
  const view = statusView(change.status);
  const stale = isStale(updated);
  return (
    <>
      <div class="lv-meta-line">
        <StatusBadge status={change.status} />
        {change.requires_authorization && (
          <Badge tone="auth" label="授權">
            需授權
          </Badge>
        )}
        {change.source && <span>來源 {change.source}</span>}
        {change.archived_at && <span>封存 {formatTime(change.archived_at)}</span>}
        <span title={formatTime(updated)} data-testid="task-synced">
          同步於 {formatAge(hoursSince(updated))}
        </span>
        {stale && <StaleBadge />}
      </div>

      {change.reasons.length > 0 && (
        <Banner
          tone={view.tone === 'error' ? 'error' : 'warn'}
          label={change.status === STATUS_UNKNOWN || view.unrecognized ? 'UNRESOLVED' : 'BLOCKED'}
          title={change.status === STATUS_UNKNOWN ? '狀態無法判定，需要人介入' : '狀態說明'}
          testId="task-reasons"
        >
          <ul class="lv-plain-list">
            {change.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </Banner>
      )}

      <section class="lv-task-section" aria-labelledby="task-why">
        <h2 id="task-why" class="lv-side-block__label">
          提案摘要 · WHY
        </h2>
        {change.why ? (
          <div class="lv-summary-box__text">
            <Markdown source={change.why} />
          </div>
        ) : (
          <p class="lv-muted">快照沒有提案摘要。</p>
        )}
      </section>

      <section class="lv-task-section" aria-labelledby="task-tasks">
        <h2 id="task-tasks" class="lv-side-block__label">
          TASKS 完成度
        </h2>
        <Progress tasks={change.tasks} />
      </section>

      <section class="lv-task-section" aria-labelledby="task-specs">
        <h2 id="task-specs" class="lv-side-block__label">
          SPEC DELTA
        </h2>
        {change.specs.length === 0 ? (
          <p class="lv-muted">沒有涉及規格的變更。</p>
        ) : (
          <ul class="lv-task-specs" data-testid="task-specs">
            {change.specs.map((s) => (
              <li key={`${s.op}:${s.capability}/${s.requirement}`} class="lv-task-spec">
                <Badge tone={s.op === 'REMOVED' ? 'warn' : s.op === 'ADDED' ? 'ready' : 'kind'} label="操作">
                  {s.op}
                </Badge>
                <span class="lv-mono lv-small">{s.capability}</span>
                <span>{s.requirement}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

function Relations({ change, vaultKey, others }: { change: TaskChange; vaultKey: string; others: TaskChange[] }) {
  const { navigate } = useApp();
  const known = new Set(others.map((c) => c.name));
  return (
    <>
      <div>
        <div class="lv-side-block__label">封存筆記 · NOTE</div>
        <ArchiveNote change={change} />
      </div>
      <div>
        <div class="lv-side-block__label">阻塞裁決 · BLOCKED BY</div>
        {change.blocked_by.length === 0 ? (
          <p class="lv-muted lv-small">沒有</p>
        ) : (
          <ul class="lv-side-list" data-testid="task-blockers">
            {change.blocked_by.map((b) => (
              <li key={b.id}>
                <Badge tone={b.resolved === true ? 'tag' : b.resolved === false ? 'warn' : 'error'} label="阻塞裁決">
                  {b.id} {blockerLabel(b.resolved)}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <div class="lv-side-block__label">依賴 · DEPENDS ON</div>
        {change.depends_on.length === 0 ? (
          <p class="lv-muted lv-small">沒有</p>
        ) : (
          <ul class="lv-side-list" data-testid="task-deps">
            {change.depends_on.map((d) => {
              const href = routePath('tasks', [vaultKey, d.name]);
              return (
                <li key={d.name}>
                  {known.has(d.name) ? (
                    <a
                      href={href}
                      onClick={(e) => {
                        e.preventDefault();
                        navigate(href);
                      }}
                    >
                      {d.name}
                    </a>
                  ) : (
                    <span class="lv-mono">{d.name}</span>
                  )}{' '}
                  <Badge tone={d.archived ? 'tag' : 'warn'} label="依賴狀態">
                    {d.archived ? '已封存' : '未封存'}
                  </Badge>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </>
  );
}

/** 已封存且帶 note_id：用 /v1/get 取標題並連過去；note 不存在時明說。進行中的 change 本來就沒有 note。 */
function ArchiveNote({ change }: { change: TaskChange }) {
  const { api, space, navigate } = useApp();
  const noteId = change.note_id;
  const [note, setNote] = useState<{ title: string } | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!noteId) return;
    const ctrl = new AbortController();
    setError(null);
    setMissing(false);
    api
      .post<GetResult<NoteFull>>('/v1/get', { space: space.id, vault: '*', ids: [noteId], fields: 'meta' }, ctrl.signal)
      .then(({ data }) => {
        const found = data.items.find((n) => n.id === noteId);
        if (found) setNote({ title: found.title });
        else setMissing(true);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
      });
    return () => ctrl.abort();
  }, [api, space.id, noteId, tick]);

  if (!noteId) {
    return (
      <p class="lv-muted lv-small" data-testid="task-note-none">
        {statusGroup(change.status) === 'done' ? '已封存，但快照沒有對應的 note。' : '進行中的 change 不寫 note，封存後才會有連結。'}
      </p>
    );
  }
  if (error !== null) return <ErrorState error={error} onRetry={() => setTick((t) => t + 1)} />;
  if (missing) {
    return (
      <p class="lv-notice lv-notice--error" role="alert" data-testid="task-note-missing">
        找不到對應的 note（可能已刪除）：<span class="lv-mono">{noteId}</span>
      </p>
    );
  }
  if (!note) return <Loading />;
  const href = routePath('notes', [noteId]);
  return (
    <a
      href={href}
      data-testid="task-note-link"
      onClick={(e) => {
        e.preventDefault();
        navigate(href);
      }}
    >
      {note.title}
    </a>
  );
}
