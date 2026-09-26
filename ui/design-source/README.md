# 設計來源（參考用）

Claude Design 專案 `648c6697-4437-43a8-96b6-2c93a2e61744`（2026-09-26 匯入），僅作實作參考，程式不直接依賴。

| 檔案 | 說明 |
|---|---|
| `Lore Vault.dc.html` | 主設計稿（dc 模板：`<x-dc>` 內為 HTML 模板，`script[data-dc-script]` 為邏輯） |
| `support.js` | Claude Design 的 dc-runtime（React 驅動的模板執行器，生成檔） |
| `_ds/u-e-p-imaginary-space-design-system-…/` | U.E.P Imaginary Space 設計系統：tokens、`components/components.css`、`_ds_bundle.js`（元件 React bundle） |

未匯入：設計系統的 `components/echoes/echoes.css`、`components/visuals/visuals.css`（Echoes／Visuals 閱讀器專用，Lore Vault 用不到）、`_ds_manifest.json`、`_adherence.oxlintrc.json`、`readme.md`。
設計系統的原始來源是 Eternity repo 的 `apps/uep`（`src/styles/tokens.css` 等）；readme 摘要：深色為預設、單一金色重點色 `#d5b618`、以 `data-zone` 綁定區域色、1px hairline 分隔、方角、無圖示庫（用 Unicode 字形）、字體 Cormorant Garamond／Noto Serif TC／Inter／JetBrains Mono／Cinzel。
