"""Reciprocal Rank Fusion（純函式，不碰 DB）。

`score(d) = Σ_leg weight_leg / (k + rank_leg(d))`，rank 從 1 起算。

- 只用排名、不用原始分數：FTS 的 -bm25 只在同一查詢內可比、cosine 在 -1–1，
  兩者量級不可比，正規化分數再加權需要逐語料調參；RRF 不需要
- `k = 60`：Cormack, Clarke & Büttcher (SIGIR 2009) 的預設值，
  在多種 TREC 資料上對 k 不敏感；k 越大，前幾名與後段的差距越平緩
- 某一路沒有的文件（例如缺向量的 note）只拿到另一路的分數，不會被丟掉
- 同分依「最佳單路名次」、再依 id 排序，結果穩定
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

RRF_K = 60


@dataclass(frozen=True)
class Fused:
    id: str
    score: float
    # 每一路的名次（1 起算）；沒出現在該路就不在 dict 裡
    ranks: Mapping[str, int] = field(default_factory=dict)


def rrf_fuse(
    rankings: Mapping[str, Sequence[str]],
    *,
    k: int = RRF_K,
    weights: Mapping[str, float] | None = None,
) -> list[Fused]:
    """融合多路排名（每路是依相關度由高到低的 id 序列）。"""
    if k < 0:
        raise ValueError(f"k 不可為負，得到 {k}")
    weights = weights or {}
    unknown = sorted(set(weights) - set(rankings))
    if unknown:
        raise ValueError(f"weights 有不存在的路：{unknown}")
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for leg, ids in rankings.items():
        if isinstance(ids, str):
            raise TypeError(f"{leg} 的排名必須是 id 序列，不可傳單一字串")
        weight = float(weights.get(leg, 1.0))
        rank = 0
        seen: set[str] = set()
        for doc_id in ids:
            if doc_id in seen:  # 同一路重複的 id 只算第一次
                continue
            seen.add(doc_id)
            rank += 1
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
            ranks.setdefault(doc_id, {})[leg] = rank
    return sorted(
        (Fused(doc_id, scores[doc_id], ranks[doc_id]) for doc_id in scores),
        key=lambda f: (-f.score, min(f.ranks.values()), f.id),
    )
