<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";

const { t } = useI18n();

const props = defineProps<{
  size?: number;
  transparent?: boolean;
}>();

/**
 * The Farna Control mark is NOT square: its brand viewBox is 240 x 183.72
 * (aspect 1.3063:1). `size` therefore sets the HEIGHT and the width is derived
 * from the mark's own aspect ratio, so the art is never squashed or letterboxed.
 */
const MARK_WIDTH = 240;
const MARK_HEIGHT = 183.72;
const MARK_ASPECT = MARK_WIDTH / MARK_HEIGHT;

const markHeight = computed(() => props.size ?? 40);
const markWidth = computed(
  () => Math.round((props.size ?? 40) * MARK_ASPECT * 100) / 100,
);
</script>

<template>
  <svg
    :width="markWidth"
    :height="markHeight"
    viewBox="0 0 240 183.72"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    role="img"
    :aria-label="t('components.LogoMark.logo')"
    :class="transparent ? 'text-foreground' : 'text-ink-50'"
  >
    <rect
      v-if="!transparent"
      width="240"
      height="183.72"
      rx="28.8"
      fill="currentColor"
      class="text-ink-900"
    />
    <!-- FAR-2582: the canonical two-tone mark, geometry copied verbatim from
         Brand/Farna Control/01-mark/farna-control-mark-{dark,light}-bg.svg.
         The hexagons are drawn in the foreground ink (currentColor): the
         theme's --foreground when transparent, the brand dark-bg ink
         (ink-50, #E8EDF2) on the opaque ink-900 plate. Only the diagonal
         stroke carries the accent — .logo-mark-accent (style.css) resolves it
         per theme to the neon dark accent or the deep-teal light accent. The
         mono-* brand files are fallbacks for media that cannot carry colour;
         the browser can, so they are not used here. -->
    <g
      transform="translate(-55.14 -88.02) scale(3.24)"
      fill="none"
      stroke="currentColor"
      stroke-width="6"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <polygon
        points="52,46.14 44,60 28,60 20,46.14 28,32.28 44,32.28"
        fill="currentColor"
      />
      <polygon points="88,66.93 80,80.79 64,80.79 56,66.93 64,53.07 80,53.07" />
      <line class="logo-mark-accent" x1="52" y1="46.14" x2="79.71" y2="30.14" />
    </g>
  </svg>
</template>
