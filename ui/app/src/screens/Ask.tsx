// 問答（D11）：檢索頁的 Ask 分頁。問題 → /v1/ask（note 範圍）→ 逐點回答，每點引用可點進筆記。
// 定位是「片段整理、信心有限」：回答旁固定說明，不把它當唯一事實來源。
// 模型呼叫要花錢又會撞限流：只在使用者按下送出時呼叫，vault 篩選改變或網址帶問題進來都不自動重問。
import { useEffect, useState } from 'preact/hooks';

import { Badge, Banner, EmptyState, ErrorState, Loading } from '../components/ui';
import { VaultPicker } from '../components/VaultPicker';
import { ApiError } from '../lib/api';
import { useApp, vaultName } from '../lib/context';
import { describeDegradedReason, formatTime, isAbort } from '../lib/format';
import { routePath } from '../lib/router';
import type { AskResult, AskSource } from '../lib/types';

interface Submission {
  question: string;
  vault: string;
  /** 同一題重送（重試）也要觸發請求 */
  tick: number;
}

// 問答專屬錯誤碼（服務端 ask_*）；其餘錯誤沿用共用的 describeError
const ASK_ERROR_TEXT: Record<string, string> = {
  ask_not_configured: '服務沒有設定問答模型，暫時無法使用問答',
  ask_provider_error: '問答模型服務連不上或回應異常',
  ask_timeout: '問答模型逾時沒有回應',
  ask_invalid_output: '問答模型回傳的內容無法解析',
  ask_failed: '問答失敗',
};

export function Ask({
  active,
  initialQuestion,
  inputId,
  onSubmitted,
}: {
  active: boolean;
  initialQuestion: string;
  /** 目前分頁的輸入框才掛快捷鍵 `/` 要找的 id */
  inputId?: string;
  onSubmitted: (question: string) => void;
}) {
  const { api, space, vault, vaults, navigate } = useApp();
  const [input, setInput] = useState(initialQuestion);
  const [submission, setSubmission] = useState<Submission | null>(null);
  const [result, setResult] = useState<AskResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);

  // 網址帶問題進來（切回分頁、重新整理）：只填入輸入框，不自動送出
  useEffect(() => {
    const q = initialQuestion.trim();
    if (q && q !== submission?.question) setInput(q);
  }, [initialQuestion]);

  useEffect(() => {
    if (!submission) return;
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    setResult(null);
    api
      .post<AskResult>(
        '/v1/ask',
        { space: space.id, question: submission.question, vault: submission.vault },
        ctrl.signal,
      )
      .then(({ data }) => {
        setResult(data);
        setLoading(false);
      })
      .catch((err) => {
        if (isAbort(err) || ctrl.signal.aborted) return;
        setError(err);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [api, space.id, submission]);

  const submit = (e: Event) => {
    e.preventDefault();
    const q = input.trim();
    if (!q || loading) return;
    setSubmission((prev) => ({ question: q, vault, tick: (prev?.tick ?? 0) + 1 }));
    onSubmitted(q);
  };

  const retry = () => setSubmission((prev) => (prev ? { ...prev, tick: prev.tick + 1 } : prev));

  const openNote = (id: string) => navigate(routePath('notes', [id]));

  const sources = new Map<string, AskSource>((result?.sources ?? []).map((s) => [s.id, s]));
  const points = result?.answer.points ?? [];
  const insufficient = result?.status === 'insufficient';
  const rateLimited = error instanceof ApiError && error.code === 'ask_rate_limited';

  return (
    <>
      <form class="lv-search" aria-label="問答" onSubmit={submit}>
        <span class="lv-search__prompt" aria-hidden="true">
          ?
        </span>
        <input
          id={inputId}
          class="lv-search__input"
          type="text"
          name="question"
          aria-label="問題"
          placeholder="用一句話問，例如：hook 為什麼只能用標準庫？（Enter 送出）"
          autoComplete="off"
          value={input}
          onInput={(e) => setInput((e.target as HTMLInputElement).value)}
        />
        <button type="submit" class="btn-outline btn-outline--sm" disabled={!input.trim() || loading}>
          {loading ? '整理中…' : '提問'}
        </button>
      </form>

      <div class="lv-filters">{active && <VaultPicker />}</div>

      <p class="lv-ask__notice" data-testid="ask-notice">
        <span class="lv-ask__notice-label">信心有限</span>
        回答是把檢索到的筆記片段交模型整理而成，可能漏掉或誤讀內容；關鍵事實請點引用開原筆記核對。目前只使用筆記，不含文件段落。
      </p>

      {!submission && (
        <EmptyState title="輸入問題開始問答">
          會先檢索相關筆記，再整理成逐點回答並標出引用的筆記。每次提問都會呼叫問答模型，需要幾秒。
        </EmptyState>
      )}
      {loading && <Loading label="檢索並整理回答中…（約數秒）" />}

      {rateLimited && (
        <Banner
          tone="warn"
          label="RATE LIMITED"
          testId="ask-rate-limited"
          title="問答模型目前忙碌，請稍後再試"
          action={
            <button type="button" class="btn-outline btn-outline--sm" onClick={retry}>
              再試一次
            </button>
          }
        >
          短時間內提問太多次，模型服務暫時限流
          {retryAfterSeconds(error) !== null ? `，大約 ${retryAfterSeconds(error)} 秒後再試` : '，稍等片刻再試'}。
        </Banner>
      )}
      {error !== null && !rateLimited && (
        <AskErrorView error={error} onRetry={retry} />
      )}

      {result && submission && !loading && (
        <div class="lv-ask" data-testid="ask-result">
          {result.degraded && (
            <Banner tone="warn" label="DEGRADED" testId="ask-degraded" title="檢索降級：只有關鍵字比對">
              原因：{describeDegradedReason(result.degraded_reason)}
              {result.degraded_detail ? `（${result.degraded_detail}）` : ''}。交給模型的片段沒有經過語意排序，回答可能漏掉相關筆記。
            </Banner>
          )}

          <div class="lv-list-head">
            <span>
              問：{submission.question} · {vaultName({ vaults }, submission.vault)}
            </span>
            <span>
              參考 {result.sources.length} 則筆記
              {result.latency_ms?.total ? ` · ${(result.latency_ms.total / 1000).toFixed(1)} 秒` : ''}
            </span>
          </div>

          {insufficient && points.length === 0 && (
            <EmptyState testId="ask-insufficient" title="找到的筆記不足以回答這個問題">
              {result.sources.length === 0
                ? '沒有檢索到相關筆記。換個說法，或把 vault 篩選改成「本 space 全部」。'
                : '模型判斷這些片段沒有答案，因此不作答。可改用檢索分頁直接查看相關筆記。'}
            </EmptyState>
          )}
          {insufficient && points.length > 0 && (
            <Banner tone="warn" label="PARTIAL" testId="ask-partial" title="片段只能部分回答">
              以下是片段裡有提到、但不足以完整回答問題的內容
              {result.status_downgraded ? '；模型的回答沒有任何可核對的引用，已改判為依據不足' : ''}。
            </Banner>
          )}

          {points.length > 0 && (
            <ol class="lv-ask__points" aria-label="回答">
              {points.map((p, i) => (
                <li key={i} class={'lv-ask__point' + (p.unsupported ? ' is-unsupported' : '')} data-testid="ask-point">
                  <p class="lv-ask__claim">
                    {p.unsupported && (
                      <Badge tone="warn" label="依據" title="這一點沒有可核對的引用，可能是模型自行補充" testId="ask-unsupported">
                        無依據
                      </Badge>
                    )}
                    <span>{p.claim}</span>
                  </p>
                  {p.note_ids.length > 0 && (
                    <ul class="lv-ask__cites" aria-label={`第 ${i + 1} 點的引用`}>
                      {p.note_ids.map((id) => (
                        <li key={id}>
                          <NoteLink id={id} source={sources.get(id)} onOpen={openNote} />
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ol>
          )}

          {result.sources.length > 0 && (
            <details class="lv-ask__sources">
              <summary>交給模型的筆記（{result.sources.length}）</summary>
              <ul>
                {result.sources.map((s) => (
                  <li key={s.id}>
                    <NoteLink id={s.id} source={s} onOpen={openNote} />
                    <span class="lv-ask__source-meta">
                      {vaultName({ vaults }, s.vault)} · {formatTime(s.updated)}
                    </span>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </>
  );
}

function NoteLink({ id, source, onOpen }: { id: string; source: AskSource | undefined; onOpen: (id: string) => void }) {
  const href = routePath('notes', [id]);
  return (
    <a
      class="lv-ask__cite"
      href={href}
      data-testid="ask-cite"
      onClick={(e) => {
        e.preventDefault();
        onOpen(id);
      }}
    >
      {source?.title ?? id}
    </a>
  );
}

function retryAfterSeconds(err: unknown): number | null {
  if (!(err instanceof ApiError)) return null;
  if (err.retryAfter !== null) return Math.ceil(err.retryAfter);
  const value = (err.body as { error?: { retry_after?: unknown } } | null)?.error?.retry_after;
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.ceil(value) : null;
}

function AskErrorView({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (error instanceof ApiError && ASK_ERROR_TEXT[error.code]) {
    return (
      <div class="zone-state zone-state--error" role="alert">
        {ASK_ERROR_TEXT[error.code]}（{error.code}）
        {error.code !== 'ask_not_configured' && (
          <button type="button" onClick={onRetry}>
            重試
          </button>
        )}
      </div>
    );
  }
  return <ErrorState error={error} onRetry={onRetry} />;
}
