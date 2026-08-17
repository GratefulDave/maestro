<script setup lang="ts">
/**
 * The run index for one Maestro lifecycle ledger.
 *
 * Two states per run, side by side, because they are genuinely two facts: the
 * LIVE state derived from the node rows right now, and the last outcome a
 * scheduler DECLARED — which survives a resume, so a rescued run still reads
 * BLOCKED there long after it started moving again.
 */
import { onMounted, onUnmounted, ref, shallowRef } from 'vue'
import type { MaestroRunSummary } from '../lib/types'
import { fetchRuns } from '../lib/api'
import { fmtDate, fmtDuration, ts } from '../lib/format'
import { hrefForSource } from '../lib/router'
import MaestroStateChip from './MaestroStateChip.vue'

const props = defineProps<{ sourceId: string }>()

const runs = shallowRef<MaestroRunSummary[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)
const nowMs = ref(Date.now())

let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight) return
  inflight = true
  try {
    runs.value = await fetchRuns(props.sourceId)
    nowMs.value = Date.now()
    apiError.value = null
    loaded.value = true
  } catch (err) {
    apiError.value = err instanceof Error ? err.message : String(err)
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void tick()
  timer = setInterval(() => void tick(), 1000)
})
onUnmounted(() => clearInterval(timer))

/** Wall time from the run's first row to now, or to its last transition. */
function span(run: MaestroRunSummary): string {
  const from = ts(run.created_at)
  if (!Number.isFinite(from)) return '—'
  const to = run.state === 'RUNNING' || run.state === 'CANCELLING' ? nowMs.value : ts(run.last_transition_at)
  return fmtDuration((Number.isFinite(to) ? to : nowMs.value) - from)
}

/** One dot per node, so the shape of the run is legible from the index. */
function dotClass(state: string): string {
  if (state === 'MERGED' || state === 'VERIFIED') return 'good'
  if (state === 'RUNNING') return 'live'
  if (state === 'BLOCKED' || state === 'CANCELLED') return 'bad'
  return 'idle'
}
</script>

<template>
  <div class="runs">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div v-if="runs.length" class="list-head dim">{{ runs.length }} maestro runs</div>

    <div v-if="runs.length" class="cards">
      <a
        v-for="run in runs"
        :key="run.run_id"
        class="card"
        :class="{ live: run.state === 'RUNNING' }"
        :href="hrefForSource(sourceId, run.run_id)"
      >
        <div class="row top">
          <span class="plan">{{ run.plan_name ?? 'plan not installed' }}</span>
          <MaestroStateChip :state="run.state" />
        </div>

        <div class="row">
          <code class="run-id">{{ run.run_id }}</code>
        </div>

        <div class="dots" :title="`${run.node_count} nodes`">
          <span
            v-for="node in run.node_states"
            :key="node.node_id"
            class="dot"
            :class="dotClass(node.state)"
            :title="`${node.node_id} — ${node.state}`"
          />
          <span v-if="!run.node_states.length" class="dim small">no nodes projected</span>
        </div>

        <div class="row foot">
          <span class="dim small">{{ fmtDate(run.created_at) }} · {{ span(run) }}</span>
          <span class="declared small" :class="{ none: !run.declared_outcome }">
            declared {{ run.declared_outcome ?? 'nothing yet' }}
          </span>
        </div>
      </a>
    </div>
    <div v-else-if="loaded" class="empty-state">
      no maestro runs in this ledger yet — start one with <code>maestro run start &lt;plan&gt;</code>
    </div>
    <div v-else-if="!apiError" class="empty-state">loading runs…</div>
  </div>
</template>

<style scoped>
.runs {
  display: flex;
  flex-direction: column;
}

.list-head {
  padding: 16px 24px 0;
  font-size: 16px;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
  gap: 18px;
  padding: 16px 24px 28px;
}

.card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 18px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  color: var(--text);
}

.card:hover {
  border-color: var(--violet);
}

.card.live {
  border-color: rgba(108, 182, 255, 0.5);
  box-shadow: 0 0 22px rgba(108, 182, 255, 0.1);
}

.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
}

.top .plan {
  font-size: 18px;
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.run-id {
  font-family: var(--mono);
  font-size: 14px;
  color: var(--faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dots {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  min-height: 14px;
}

.dot {
  width: 11px;
  height: 11px;
  border-radius: 3px;
  background: var(--border);
}

.dot.good {
  background: var(--green);
}

.dot.bad {
  background: var(--red);
}

.dot.idle {
  background: #2b3448;
}

.dot.live {
  background: var(--blue);
  box-shadow: 0 0 9px rgba(108, 182, 255, 0.8);
  animation: pulse 1.6s ease-in-out infinite;
}

.small {
  font-size: 14px;
}

.dim {
  color: var(--dim);
}

.declared {
  font-family: var(--mono);
  color: var(--dim);
}

.declared.none {
  color: var(--faint);
  font-style: italic;
}

.empty-state {
  padding: 40px 24px;
  color: var(--dim);
}

.error-bar {
  padding: 10px 24px;
  color: var(--red);
}
</style>
