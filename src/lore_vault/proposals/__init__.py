"""待審提案（add-agent-review-proposals）：一般 agent 的受保護操作先進提案、
經 UI 核准才生效。

分層（依賴單向）：`types`、`policy`（純函式）→ `storage.proposals`（資料列）→
`service`（核准、駁回、claim，套用時呼叫 notes.service／storage.admin）。
本檔不 import 任何子模組，避免 notes.service ↔ proposals.service 循環。
"""
