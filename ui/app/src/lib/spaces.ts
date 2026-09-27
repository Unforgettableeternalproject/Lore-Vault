// space（A18）與設計系統 zone 的對應（UI 實作計畫 1.3）。
export const SPACE_IDS = ['dev', 'lore', 'personal'] as const;
export type SpaceId = (typeof SPACE_IDS)[number];

export interface SpaceMeta {
  id: SpaceId;
  zone: 'concepts' | 'history' | 'echoes';
  glyph: string;
  name: string;
  en: string;
  desc: string;
}

export const SPACES: Record<SpaceId, SpaceMeta> = {
  dev: { id: 'dev', zone: 'concepts', glyph: '>', name: '專案開發', en: 'DEV', desc: '專案決策、踩坑、交接' },
  lore: { id: 'lore', zone: 'history', glyph: '❖', name: '世界觀', en: 'LORE', desc: '設定、角色、年表' },
  personal: { id: 'personal', zone: 'echoes', glyph: '•', name: '私人', en: 'PERSONAL', desc: '私人筆記與日記' },
};
