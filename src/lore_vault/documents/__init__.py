"""文件存儲與檢索（A19）：抽取器（`documents.extract`）等不碰儲存層的處理邏輯。

metadata 與 blob 在 `storage.documents`／`storage.blobs`；切段、非同步抽取 worker、
索引寫入屬 T-63 之後。
"""
