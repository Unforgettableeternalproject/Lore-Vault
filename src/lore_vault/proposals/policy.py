"""哪些寫入要送審（純函式、無 I/O；政策只在這一處，要收緊只改這裡）。

適用範圍：dev 與 lore space、認證方式為 bearer 的一般 agent（`Actor.kind == agent`，
且 principal 不是 uep）。UI session、系統流程、已核准的套用、uep 與 personal 一律放行
（uep 在 lore 有自己的 pending 審核契約，在 dev 維持直接生效）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from lore_vault.schema import UEP_PRINCIPAL, WRITE_ORIGIN_UI, Note

from .types import (
    ACTOR_AGENT,
    KIND_LINK_CHANGE,
    KIND_NOTE_UPDATE,
    NO_HOLD,
    SPACES,
    Actor,
    Decision,
)


def is_governed(actor: Actor, space: str) -> bool:
    """這個寫入者在這個 space 是否受提案政策約束。"""
    return (
        actor.kind == ACTOR_AGENT
        and actor.principal != UEP_PRINCIPAL
        and space in SPACES
    )


def decide_delete(actor: Actor, space: str) -> bool:
    """note／document 刪除是否改走提案（所有 bearer 一般 agent 的刪除都是）。"""
    return is_governed(actor, space)


def decide_update(
    actor: Actor,
    space: str,
    current: Note,
    requested: Mapping[str, Any],
    *,
    removed_links: Sequence[str] = (),
) -> Decision:
    """update 是否送審。

    - 目標 write_origin 為 ui（人類寫的）→ 整筆 `note_update`，不拆
    - 否則只有「明確的連結移除」與「supersedes 由非空改成其他值或清空」送審
      （`link_change`），其餘欄位與新增連結立即套用（`partial`）；新增連結與 body
      衍生的連結增減不送審
    `requested`：呼叫端明確要改的欄位（title／body／topics／links／supersedes）；
    `removed_links`：服務層算出的「明確移除」連結 id（已排除舊 body 衍生的）。
    """
    if not is_governed(actor, space):
        return NO_HOLD
    if current.write_origin == WRITE_ORIGIN_UI:
        return Decision(True, KIND_NOTE_UPDATE, {"changes": dict(requested)})
    removed = sorted(dict.fromkeys(removed_links))
    supersedes_moved = (
        "supersedes" in requested
        and current.supersedes is not None
        and requested["supersedes"] != current.supersedes
    )
    if not removed and not supersedes_moved:
        return NO_HOLD
    payload: dict[str, Any] = {
        "remove": removed,
        "supersedes": (
            {"from": current.supersedes, "to": requested["supersedes"]}
            if supersedes_moved
            else None
        ),
    }
    remaining = dict(requested)
    if supersedes_moved:
        remaining.pop("supersedes")
    if removed and "links" in remaining:
        explicit = list(remaining["links"])
        additions = [i for i in explicit if i not in current.links]
        if additions:
            # 被攔下的移除先留著，新增的照常套用
            remaining["links"] = tuple(dict.fromkeys([*explicit, *removed]))
        else:
            remaining.pop("links")
    return Decision(
        True, KIND_LINK_CHANGE, payload, partial=bool(remaining), remaining=remaining
    )
