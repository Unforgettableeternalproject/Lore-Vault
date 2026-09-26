// 各畫面的佔位：之後由對應的畫面卡（T-79～T-85）取代。
import type { SpaceMeta } from '../lib/spaces';

interface Props {
  eyebrow: string;
  title: string;
  space: SpaceMeta;
  card: string;
}

export function Placeholder({ eyebrow, title, space, card }: Props) {
  return (
    <section class="lv-screen">
      <div class="lv-eyebrow">
        {eyebrow} · {space.en} SPACE
      </div>
      <h1 class="lv-title">{title}</h1>
      <div class="zone-state lv-placeholder">
        此畫面尚未實作（{card}）。
      </div>
    </section>
  );
}
