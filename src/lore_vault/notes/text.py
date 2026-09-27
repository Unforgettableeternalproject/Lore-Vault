"""note 文字的單一來源：embedding 輸入等衍生文字。

葉子模組（只用標準庫）：notes 服務的查重與 enrich worker 的補算都 import 這裡，
兩邊算出的向量才能互相比較。
"""

from __future__ import annotations


def embedding_text(title: str, body: str) -> str:
    """embedding 的輸入：title + body（與 storage 在 title／body 變動時刪向量一致）。"""
    return f"{title}\n\n{body}" if body else title
