// 任務層（TASK_LAYER_UI）：各 repo 的 change 推導狀態，來源是任務層 CLI 推送到通用側載的快照
// （`/v1/blob_get` key `tasks-snapshot`）。狀態改變在本機以 CLI 或經 MCP 操作；這裡唯一的動作是
// `requires_authorization` change 的人類核准（TASK_LAYER_MCP §3.3）：讀服務端 change 與授權紀錄的核准狀態（內容雜湊比對），
// 經 UI session 限定的 `/v1/tasks_authorize` 寫入，MCP 的 archive 只認這份紀錄。
// 任務層只屬於 dev space，其他 space 只顯示說明（比照記憶層）。
import { useEffect, useState } from 'preact/hooks';

import { ChipGroup, FilterPanel, queryChoice, screenQuery, useQuerySync } from '../components/Filters';
import { Badge, Banner, Dialog, EmptyState, ErrorState, Loading } from '../components/ui';
import { VaultPicker } from '../components/VaultPicker';
import { ALL, useApp, vaultName } from '../lib/context';
import { describeError, formatTime, isAbort } from '../lib/format';
import { formatAge, hoursSince } from '../lib/health';
import { Markdown } from '../lib/markdown';
import { routePath } from '../lib/router';
import {
  approvalState,
  approveChange,
  blockerLabel,
  canApprove,
  fetchApproval,
  fetchTaskSnapshots,
  filterRows,
  isStale,
  statusGroup,
  statusView,
  STATUS_DONE,
  STATUS_UNKNOWN,
  taskRows,
  TASK_FILTER_IDS,
  TASK_FILTERS,
  TASK_STALE_HOURS,
  type ApprovalInfo,
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
  const eyebrow = <div class="lv-eyebrow">TASK LAYER · {space.en}</div>;
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
            各 repo 任務層的 change 與推導狀態，內容是任務層 CLI 最後一次推送的快照。狀態要在本機用 CLI 或經 MCP 改變；需授權的 change 在詳情頁核准。
          </p>
          <TaskList />
        </>
      )}
    </section>
  );
}

const GROUP_ORDER: Record<TaskGroup, number> = { ready: 0, auth: 1, blocked: 2, done: 3 };

/**
 * 取回快照。資料綁定發出請求時的 space＋vault：切換 vault 後、新回應到達前不回傳舊 vault 的資料
 * （畫面顯示載入中），晚到的過時回應（快速切 A→B 時 A 的回應）直接丟棄，不覆蓋目前的結果。
 */
function useSnapshots(vault: string) {
  const { api, space } = useApp();
  const key = space.id + '|' + vault;
  const [result, setResult] = useState<{ key: string; data: VaultSnapshot[] } | null>(null);
  const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setFailure(null);
    fetchTaskSnapshots(api, space.id, vault, ctrl.signal)
      .then((data) => {
        if (ctrl.signal.aborted) return;
        setResult({ key, data });
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setFailure({ key, error: err });
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, space.id, vault, tick]);
  const data = result?.key === key ? result.data : null;
  const error = failure?.key === key ? failure.error : null;
  return { data, error, loading, retry: () => setTick((t) => t + 1) };
}

function TaskList() {
  const { vault, vaults } = useApp();
  // 篩選初值取自網址（重新整理、從詳情返回都保留），與筆記、文件同一套
  const [initial] = useState(() => screenQuery('tasks'));
  const [filter, setFilter] = useState<TaskFilter>(() => queryChoice(initial, 'status', TASK_FILTER_IDS, ''));
  useQuerySync('tasks', { status: filter });
  const { data, error, loading, retry } = useSnapshots(vault);
  const single = vault !== ALL;

  const groups = (data ?? []).map((s) => ({ snap: s, rows: filterRows(sortRows(taskRows([s])), filter) }));
  const total = groups.reduce((n, g) => n + (g.snap.snapshot?.changes.length ?? 0), 0);
  const shown = groups.reduce((n, g) => n + g.rows.length, 0);
  // 篩選後沒有項目的 vault 不顯示；快照格式不對的 vault 一律顯示（錯誤不能被篩掉）
  const visible = groups.filter((g) => g.rows.length > 0 || g.snap.error);
  const filterLabel = TASK_FILTERS.find((f) => f.id === filter)?.label ?? '';

  return (
    <div class="lv-tasks-view">
      <FilterPanel
        active={filter !== ''}
        onClear={() => setFilter('')}
        summary={
          <>
            範圍：{vaultName({ vaults }, vault)} · {filterLabel}
          </>
        }
      >
        <div class="lv-filter-panel__row">
          <VaultPicker />
          <ChipGroup label="狀態" groupLabel="change 狀態" options={TASK_FILTERS} value={filter} onChange={setFilter} mono={false} />
        </div>
      </FilterPanel>

      {error !== null && <ErrorState error={error} onRetry={retry} />}
      {loading && !data && <Loading />}
      {data && error === null && data.length === 0 && (
        <EmptyState testId="tasks-not-synced" title="尚未同步">
          {single ? '這個 vault 還沒有推送任務層快照。' : '這個 space 還沒有任何 repo 推送任務層快照。'}
          在 repo 執行任務層的 sync（或 propose／validate／archive／list）後，這裡會顯示最新狀態。
        </EmptyState>
      )}
      {data && error === null && data.length > 0 && total === 0 && data.some((s) => s.snapshot) && !data.some((s) => s.error) && (
        <EmptyState testId="tasks-empty" title="快照裡沒有任何 change">任務層目前沒有進行中或已封存的 change。</EmptyState>
      )}
      {data && error === null && total > 0 && shown === 0 && (
        <EmptyState testId="tasks-filter-empty" title="沒有符合篩選的 change">
          {filter === '' ? '目前沒有未完成的 change；選「已完成」或「全部」可列出已完成的項目。' : '換一個狀態或 vault 再試一次。'}
        </EmptyState>
      )}
      {data && error === null && visible.length > 0 && (
        <div class="lv-task-groups" data-testid="tasks" aria-busy={loading}>
          {visible.map((g) => (
            <TaskGroupView key={g.snap.vault} snap={g.snap} rows={g.rows} header={!single} />
          ))}
        </div>
      )}
    </div>
  );
}

function sortRows(rows: TaskRow[]): TaskRow[] {
  return rows.sort((a, b) => GROUP_ORDER[statusGroup(a.change.status)] - GROUP_ORDER[statusGroup(b.change.status)]);
}

/** 單一 vault 的區塊：標頭（vault 名、同步時間、過時標示）、格式錯誤橫幅、該 vault 的 change。
 *  只看單一 vault 時不另加標頭，同步資訊以一行 metadata 呈現。 */
function TaskGroupView({ snap, rows, header }: { snap: VaultSnapshot; rows: TaskRow[]; header: boolean }) {
  const { vaults } = useApp();
  const stale = isStale(snap.updated);
  const name = vaultName({ vaults }, snap.vault);
  const sync = (
    <>
      <span title={formatTime(snap.updated)} data-testid="task-synced">
        同步於 {formatAge(hoursSince(snap.updated))}
      </span>
      {stale && <StaleBadge />}
    </>
  );
  return (
    <section class="lv-task-group" data-testid="task-group" data-vault={snap.vault} data-stale={String(stale)} aria-label={`${name} 的 change`}>
      {header ? (
        <header class="lv-task-group__head">
          <h2 class="lv-task-group__title" title={snap.vault}>
            {name}
          </h2>
          <div class="lv-meta-line">{sync}</div>
        </header>
      ) : (
        <div class="lv-meta-line lv-task-group__sync">{sync}</div>
      )}
      {snap.error && (
        <Banner tone="error" label="INVALID" title="快照格式不正確" testId="task-snapshot-invalid">
          {snap.error}。請在該 repo 重跑任務層的 sync。
        </Banner>
      )}
      {rows.length > 0 && (
        <ul class="lv-tasks" aria-label={`${name} 的 change 列表`}>
          {rows.map((r) => (
            <TaskRowView key={r.change.name} row={r} />
          ))}
        </ul>
      )}
    </section>
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

function TaskRowView({ row }: { row: TaskRow }) {
  const { navigate } = useApp();
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
          {change.source && (
            <Badge tone="kind" label="來源卡">
              {change.source}
            </Badge>
          )}
          <BlockerBadges change={change} />
        </div>
      </div>
      <div class="lv-task__side">
        <Progress tasks={change.tasks} />
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
        {snap && change && <ChangeBody change={change} updated={snap.updated} vaultKey={vaultKey} />}
      </article>
      {snap?.snapshot && change && (
        <aside class="lv-detail__side" aria-label="change 關聯">
          <Relations change={change} vaultKey={vaultKey} others={snap.snapshot.changes} />
        </aside>
      )}
    </div>
  );
}

function ChangeBody({ change, updated, vaultKey }: { change: TaskChange; updated: string; vaultKey: string }) {
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

      {change.requires_authorization && change.status !== STATUS_DONE && <Approval vaultKey={vaultKey} name={change.name} />}

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

// ── 人類核准（TASK_LAYER_MCP §3.3）──

/**
 * 需授權 change 的核准區塊。核准狀態一律讀服務端（`/v1/tasks_authorization_status`），不信快照：
 * 核准綁定服務端目前的內容（內容雜湊；archive 的簿記寫入不算修改），之後內容再改即過期，要重新核准。
 * 顯示條件是「需授權且未完成」而不只「待授權」：快照的推導狀態日後可能把已核准的 change 算成別的狀態。
 */
function Approval({ vaultKey, name }: { vaultKey: string; name: string }) {
  const { api, toast } = useApp();
  const [info, setInfo] = useState<ApprovalInfo | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    setError(null);
    fetchApproval(api, vaultKey, name, ctrl.signal)
      .then((data) => {
        if (!ctrl.signal.aborted) setInfo(data);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
      });
    return () => ctrl.abort();
  }, [api, vaultKey, name, tick]);

  const reload = () => setTick((t) => t + 1);
  const version = info?.change?.version;
  const approve = async () => {
    setBusy(true);
    try {
      await approveChange(api, vaultKey, name);
      toast(`已核准 ${name}（v${version}）`, 'success');
    } catch (err) {
      toast(`核准失敗：${describeError(err)}`, 'error');
    } finally {
      setBusy(false);
      setConfirming(false);
      // 成功或失敗（版本已變、change 已封存等）都重讀服務端狀態
      reload();
    }
  };

  let body;
  if (error !== null) body = <ErrorState error={error} onRetry={reload} />;
  else if (!info) body = <Loading />;
  else body = <ApprovalBody info={info} busy={busy} onApprove={() => setConfirming(true)} />;

  return (
    <section class="lv-task-section" aria-labelledby="task-approval" data-testid="task-approval">
      <h2 id="task-approval" class="lv-side-block__label">
        人類核准 · AUTHORIZATION
      </h2>
      {body}
      {confirming && info?.change && (
        <Dialog
          title={`核准 ${name} 的 v${version}？`}
          onClose={() => setConfirming(false)}
          actions={
            <>
              <button type="button" class="uep-dialog__btn uep-dialog__btn--cancel" onClick={() => setConfirming(false)}>
                取消
              </button>
              <button type="button" class="uep-dialog__btn uep-dialog__btn--confirm" disabled={busy} onClick={() => void approve()}>
                確認核准
              </button>
            </>
          }
        >
          核准後，AI 可以經 MCP 封存這個 change（併入主 spec、寫入總結 note），紀錄以你的登入身分留存。
          核准只對服務端目前 v{version} 的內容有效；之後內容再被修改，核准即失效、需要重新核准（封存過程的簿記寫入不算修改）。
        </Dialog>
      )}
    </section>
  );
}

function ApprovalBody({ info, busy, onApprove }: { info: ApprovalInfo; busy: boolean; onApprove: () => void }) {
  const { change, record } = info;
  const state = approvalState(info);
  const formatError = info.error && (
    <Banner tone="error" label="INVALID" title="服務端內容格式不正確" testId="task-approval-invalid">
      {info.error}
    </Banner>
  );
  if (!change) {
    return (
      <div class="lv-stack lv-task-approval">
        {formatError}
        <p class="lv-muted" data-testid="task-approval-no-change">
          服務端沒有這個 change 的內容（尚未經 MCP 或同步推送到服務），無法在這裡核准。
        </p>
      </div>
    );
  }
  const button = (label: string) => (
    <button type="button" class="btn-outline btn-outline--gold" disabled={busy} onClick={onApprove}>
      {label}
    </button>
  );
  const approvable = canApprove(change);
  // 徽章列、核准紀錄、說明與按鈕之間用 lv-stack 的間距，不靠各元素自帶的 margin
  return (
    <div class="lv-stack lv-task-approval">
      {formatError}
      <div class="lv-meta-line">
        {state === 'approved' ? (
          <Badge tone="ready" label="核准狀態" testId="task-approval-state">
            已核准
          </Badge>
        ) : state === 'stale' ? (
          <Badge tone="warn" label="核准狀態" testId="task-approval-state">
            核准已過期
          </Badge>
        ) : (
          <Badge tone="auth" label="核准狀態" testId="task-approval-state">
            尚未核准
          </Badge>
        )}
        <span data-testid="task-approval-version">服務端目前版本 v{change.version}</span>
      </div>
      {record && (
        <p class="lv-small" data-testid="task-approval-record">
          {record.authorizedBy} 於 {record.authorizedAt ? formatTime(record.authorizedAt) : '（時間不明）'} 核准了 v{record.changeVersion}
          {state === 'stale' ? `；之後內容已修改（目前 v${change.version}），需要重新核准。` : '。'}
        </p>
      )}
      {!approvable ? (
        <p class="lv-muted lv-small" data-testid="task-approval-unavailable">
          {change.state !== 'active'
            ? `服務端的 change 已不在進行中（${change.state || '狀態不明'}），不能再核准。`
            : '服務端的 change 沒有標記需授權，不需要核准。'}
        </p>
      ) : state === 'none' ? (
        button(`核准 v${change.version}`)
      ) : state === 'stale' ? (
        button(`重新核准 v${change.version}`)
      ) : null}
    </div>
  );
}
