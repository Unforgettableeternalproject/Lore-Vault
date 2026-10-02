"""concept scope 對帳：`scope` 是否與自己的 `anchors` 一致（2026-10-02 事故）。

事故：一組蒸餾候選同時碰到 monorepo 底下兩個子專案（`JSAI-Functions/`、
`JSAI-API/`），LLM 把兩條 concept 的 `scope` 都填成不存在的上位名稱
`'JSAI'`——不是任何 vault 的名稱或別名，`source_turns` 也可能查無對應
episode——推送時整批被服務端拒收（`vault_unresolved`），卡住所有 concept。

源頭已在 `agent_memory_spike/distill.py` 的 `resolve_scope`／`scope_from_anchors`
修：anchors 比自由文字的 scope 更具體，不一致且不歧義時以 anchors 覆蓋。
這項 doctor 檢查是第二層防線——驗證本機 `concepts.json` 裡沒有殘留的
scope／anchors 不一致（例如修復前就已經 ingest 進池子的舊記錄，或未來
`resolve_scope` 被繞過／改壞），在排程推送前、而不是被服務端拒收後才發現。

只讀 `id`／`scope`／`anchors` 欄位，不讀 `statement`／`why` 等含語料原文的欄位。

設定鍵：
- `spike_home`：spike 資料目錄（含 `concepts.json`）；未設時用 `spool_dir`
  上一層。兩者皆無，或 `concepts.json` 不存在 → skipped
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .concept_push_check import CONCEPTS_NAME, _spike_home
from .framework import CheckResult, CheckSkipped, DoctorContext

SAMPLE_LIMIT = 5


def _scope_from_anchors(anchors: Any) -> str | None:
    """與 `agent_memory_spike/distill.py` 的 `scope_from_anchors` 同一定義。

    doctor（`src/`）不 import `agent_memory_spike`（spike 刻意保持獨立，
    見 distill.py 開頭），所以這裡保留一份；兩邊的行為由
    `agent_memory_spike/test_distill.py` 與本模組的測試分別驗證，
    任一邊改了判準都要同步另一邊。
    """
    heads: set[str] = set()
    for anchor in anchors or []:
        if not isinstance(anchor, str) or "/" not in anchor:
            continue
        head = anchor.split("/", 1)[0].strip()
        if head:
            heads.add(head)
    if len(heads) == 1:
        return next(iter(heads))
    return None


def concept_scope_anchor_agreement(ctx: DoctorContext) -> CheckResult:
    home = _spike_home(ctx)
    concepts_path = home / CONCEPTS_NAME
    if not concepts_path.exists():
        raise CheckSkipped(f"{concepts_path} 不存在")
    try:
        concepts = json.loads(concepts_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return CheckResult.fail(f"{CONCEPTS_NAME} 讀不出來：{type(exc).__name__}")
    if not isinstance(concepts, list):
        return CheckResult.fail(f"{CONCEPTS_NAME} 不是 concept 陣列")

    # 已知合法 vault 名單：被**多個不同候選組**獨立用過的 scope。
    # 單純比對 anchor 開頭段落與 scope 不一致、或單純看 scope 出現次數，
    # 會在真實池子裡大量假警報（實測 398/1573 筆，例如 scope='Eternity' 的
    # anchor 剛好有一段叫 'workers'——那是目錄名，不是 repo）；只看出現次數
    # 也擋不住本次事故本身（同一組候選一次產出兩條，同一個錯字串出現兩次，
    # 看起來像「有共識」）。只有「anchor 指的名字是已知合法 vault、宣稱的
    # scope 卻不是」才算得上證據，與 agent_memory_spike/distill.py 的
    # resolve_scope／known_repos_from_concepts 用同一道門檻（MIN_DISTINCT_CANDIDATES）。
    MIN_DISTINCT_CANDIDATES = 2
    candidates_by_scope: dict[str, set[str]] = {}
    for c in concepts:
        if not isinstance(c, dict):
            continue
        s = c.get("scope")
        if not isinstance(s, str) or not s.strip():
            continue
        s = s.strip()
        cand = c.get("source_candidate")
        key = cand if isinstance(cand, str) and cand else f"id:{c.get('id')}"
        candidates_by_scope.setdefault(s, set()).add(key)
    known_repos = {
        s for s, cands in candidates_by_scope.items() if len(cands) >= MIN_DISTINCT_CANDIDATES
    }

    mismatches: list[str] = []
    checked = 0
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        scope = concept.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            continue  # None／空值＝通用或未表態，不是這項檢查的範圍
        scope = scope.strip()
        checked += 1
        anchor_scope = _scope_from_anchors(concept.get("anchors"))
        if (
            anchor_scope
            and anchor_scope != scope
            and anchor_scope in known_repos
            and scope not in known_repos
        ):
            cid = concept.get("id") if isinstance(concept.get("id"), str) else "?"
            mismatches.append(f"{cid}: scope={scope!r} 但 anchors 指向已知 vault {anchor_scope!r}")

    counts = {"checked": checked, "mismatched": len(mismatches)}
    if mismatches:
        details = mismatches[:SAMPLE_LIMIT]
        if len(mismatches) > SAMPLE_LIMIT:
            details.append(f"…等 {len(mismatches)} 筆")
        return CheckResult.fail(
            f"{len(mismatches)} 筆 concept 的 scope 與自己的 anchors 不一致"
            "（scope 很可能是蒸餾時猜錯的上位名稱，推送會被服務端以"
            " vault_unresolved 拒收）",
            details=details,
            counts=counts,
        )
    return CheckResult.ok(f"{checked} 筆帶 scope 的 concept 皆與 anchors 一致", counts=counts)
