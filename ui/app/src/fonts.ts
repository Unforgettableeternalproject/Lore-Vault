// 字型自託管（取代設計系統 tokens/fonts.css 的 Google Fonts @import）：
// CSP 維持 font-src 'self'，不對外連線。只載入實際用到的字重；拉丁字型只取 latin 子集，
// Noto Serif TC 依 unicode-range 分片、瀏覽器只下載畫面用到的片。
// 從 JS 匯入（而非 CSS @import）才會經過 vite.config.ts 的 woff2Only 轉換，建置產物只含 woff2。
// Cinzel（設計系統的年表字型）Lore Vault 用不到，不載入，token 會退回 serif。
import '@fontsource/inter/latin-400.css';
import '@fontsource/inter/latin-500.css';
import '@fontsource/inter/latin-600.css';
import '@fontsource/jetbrains-mono/latin-400.css';
import '@fontsource/jetbrains-mono/latin-500.css';
import '@fontsource/jetbrains-mono/latin-700.css';
import '@fontsource/cormorant-garamond/latin-400.css';
import '@fontsource/cormorant-garamond/latin-500.css';
import '@fontsource/cormorant-garamond/latin-600.css';
import '@fontsource/noto-serif-tc/400.css';
import '@fontsource/noto-serif-tc/600.css';
