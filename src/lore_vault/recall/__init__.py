"""服務層：統一檢索（範圍過濾 → 候選 → 排序 → 預算裁切）。"""

from .embedder import Embedder, QueryVector, embed_text
from .rrf import RRF_K, Fused, rrf_fuse
from .service import RecallItem, RecallResult, UnsupportedKind, recall

__all__ = [
    "RRF_K",
    "Embedder",
    "Fused",
    "QueryVector",
    "RecallItem",
    "RecallResult",
    "UnsupportedKind",
    "embed_text",
    "recall",
    "rrf_fuse",
]
