"""文件存儲與檢索（A19）：抽取器（`extract`）、切段（`chunking`）、上傳與 get／list
項目（`service`）、背景抽取與向量補算 worker（`worker`）。

metadata、chunk、索引與對帳在 `storage.documents`／`storage.document_index`／
`storage.chunk_vectors`；原始檔在 `storage.blobs`。
"""
