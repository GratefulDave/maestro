<script setup lang="ts">
/**
 * A lifecycle state, shown as itself.
 *
 * StatusChip maps everything onto the tracer's four words, which would print
 * "success" where the ledger says MERGED and "fail" where it says BLOCKED —
 * and BLOCKED vs CANCELLED is the difference between a node an operator can
 * rescue and one that is absolutely terminal. The state string is the label;
 * only the colour is a judgement.
 */
import { computed } from 'vue'
import { Ban, Check, CircleDashed, LoaderCircle, OctagonX, ShieldCheck } from 'lucide-vue-next'

const props = defineProps<{ state: string; small?: boolean }>()

/** tone drives colour only — never what the chip says. */
const TONES: Record<string, 'good' | 'bad' | 'live' | 'idle' | 'warn'> = {
  MERGED: 'good',
  VERIFIED: 'good',
  ACCEPTED: 'good',
  RUNNING: 'live',
  CANCELLING: 'warn',
  PENDING: 'idle',
  QUIESCENT: 'idle',
  EMPTY: 'idle',
  BLOCKED: 'bad',
  CANCELLED: 'bad',
  STUCK: 'bad',
}

const ICONS: Record<string, unknown> = {
  MERGED: Check,
  VERIFIED: ShieldCheck,
  ACCEPTED: Check,
  RUNNING: LoaderCircle,
  CANCELLING: Ban,
  CANCELLED: Ban,
  BLOCKED: OctagonX,
  STUCK: OctagonX,
}

const tone = computed(() => TONES[props.state] ?? 'idle')
const icon = computed(() => ICONS[props.state] ?? CircleDashed)
</script>

<template>
  <span class="chip" :class="[tone, { small }]">
    <component :is="icon" class="chip-icon" :size="small ? 14 : 17" :stroke-width="2.5" />
    {{ state }}
  </span>
</template>

<style scoped>
.chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 3px 12px 3px 9px;
  border-radius: 999px;
  border: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 15px;
  letter-spacing: 0.03em;
  color: var(--dim);
  white-space: nowrap;
}

.chip.small {
  font-size: 13px;
  padding: 1px 9px 1px 7px;
  gap: 5px;
}

.chip-icon {
  flex: none;
}

.chip.good {
  color: var(--green);
  border-color: rgba(74, 222, 128, 0.45);
  background: rgba(74, 222, 128, 0.09);
}

.chip.bad {
  color: var(--red);
  border-color: rgba(255, 111, 103, 0.45);
  background: rgba(255, 111, 103, 0.09);
}

.chip.warn {
  color: var(--amber);
  border-color: rgba(232, 182, 74, 0.45);
  background: rgba(232, 182, 74, 0.09);
}

.chip.live {
  color: var(--blue);
  border-color: rgba(108, 182, 255, 0.5);
  background: rgba(108, 182, 255, 0.1);
  box-shadow: 0 0 12px rgba(108, 182, 255, 0.18);
}

.chip.live .chip-icon {
  animation: spin 1.1s linear infinite;
}

.chip.idle {
  color: var(--faint);
  border-style: dashed;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
