<script setup lang="ts">
/**
 * One Maestro run, live.
 *
 * The DAG is laid out in depth columns because depth is the ledger's own
 * ordering of the graph, and each node repeats its `needs` as chips — the
 * edges have to be readable for an arbitrary plan, not only for the shallow
 * one-lane plans that happen to run today.
 *
 * Elapsed times are measured against the SERVER's clock (`server_now_ms`),
 * offset by the browser's, because attempt timestamps are stamped by the
 * scheduler's host and a browser several seconds out would otherwise show a
 * negative or inflated in-flight duration.
 */
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { Ban, ChevronRight, GitBranch, TriangleAlert } from 'lucide-vue-next'
import type { MaestroAttempt, MaestroNode, MaestroRunDetail } from '../lib/types'
import { ApiHttpError, fetchRun } from '../lib/api'
import { fmtDate, fmtDuration, ts } from '../lib/format'
import MaestroStateChip from './MaestroStateChip.vue'

const props = defineProps<{ sourceId: string; runId: string }>()

const run = shallowRef<MaestroRunDetail | null>(null)
const apiError = ref<string | null>(null)
/**
 * A 404 for this run id, as opposed to a transient `apiError`. Distinct from
 * "no run yet" (`!run`) because that state is also true while the first
 * fetch is in flight — a plain `!run` render would say "loading" forever for
 * a run that will never come back, and worse, a `catch` that only set
 * `apiError` without clearing `run` would leave the *previous* run's panes
 * on screen looking live while a small banner most operators never read said
 * otherwise. This is the state that was silently missing.
 */
const notFound = ref(false)
const open = ref<Set<string>>(new Set())
/** Browser clock minus server clock, so both agree on "now". */
const skewMs = ref(0)
const nowMs = ref(Date.now())

let timer: ReturnType<typeof setInterval> | undefined
let clock: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight || notFound.value) return
  inflight = true
  try {
    const detail = await fetchRun(props.sourceId, props.runId)
    skewMs.value = Date.now() - detail.server_now_ms
    run.value = detail
    apiError.value = null
  } catch (err) {
    if (err instanceof ApiHttpError && err.status === 404) {
      // The run this URL names is gone from the ledger — stop polling for it
      // and drop any stale copy so nothing keeps rendering it as live.
      notFound.value = true
      run.value = null
      apiError.value = null
      if (timer) clearInterval(timer)
    } else {
      apiError.value = err instanceof Error ? err.message : String(err)
    }
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void tick()
  timer = setInterval(() => void tick(), 1000)
  // A separate, faster clock so an in-flight attempt's timer keeps counting
  // between polls instead of stepping once a second behind the network.
  clock = setInterval(() => (nowMs.value = Date.now()), 250)
})
onUnmounted(() => {
  clearInterval(timer)
  clearInterval(clock)
})
watch(
  () => props.runId,
  () => {
    notFound.value = false
    run.value = null
    if (!timer) timer = setInterval(() => void tick(), 1000)
    void tick()
  },
)

/** The server's clock, right now, as this browser can best estimate it. */
const serverNow = computed(() => nowMs.value - skewMs.value)

const isLive = computed(
  () => run.value?.state === 'RUNNING' || run.value?.state === 'CANCELLING',
)

/**
 * Why `run resume` will not take this cancelled run.
 *
 * The two refusals are not alike and an operator needs to know which they are
 * looking at: an ABANDONED run gave every node up deliberately and nothing
 * reopens it, while a run with no recorded cause is refused because the ledger
 * predates the column and reading an unrecorded cancellation as a pause is the
 * guess that reopens an adjudicated run.
 */
const notResumableWhy = computed(() =>
  run.value?.cancel_cause === 'ABANDONED'
    ? 'given up node by node — `run resume` is refused'
    : 'cause unrecorded in this ledger — `run resume` is refused',
)

const elapsed = computed(() => {
  const from = ts(run.value?.created_at)
  if (!Number.isFinite(from)) return '—'
  const to = isLive.value ? serverNow.value : ts(run.value?.last_transition_at)
  return fmtDuration((Number.isFinite(to) ? to : serverNow.value) - from)
})

const idle = computed(() => {
  const from = ts(run.value?.last_transition_at)
  return Number.isFinite(from) ? fmtDuration(serverNow.value - from) : '—'
})

/** Nodes grouped into depth columns — the DAG's own layering. */
const columns = computed(() => {
  const byDepth = new Map<number, MaestroNode[]>()
  for (const node of run.value?.nodes ?? []) {
    const list = byDepth.get(node.depth)
    if (list) list.push(node)
    else byDepth.set(node.depth, [node])
  }
  return [...byDepth.entries()]
    .toSorted((a, b) => a[0] - b[0])
    .map(([depth, nodes]) => ({ depth, nodes }))
})

/** Every attempt still in flight, anywhere in the graph. */
const inFlight = computed(() =>
  (run.value?.nodes ?? []).flatMap((node) => node.attempts.filter((a) => a.running)),
)

function attemptStart(attempt: MaestroAttempt): number | null {
  return attempt.launched_at_ms ?? attempt.started_at_ms
}

/** How long an attempt has been running; blank once it is closed. */
function attemptElapsed(attempt: MaestroAttempt): string {
  const from = attemptStart(attempt)
  if (from === null) return '—'
  if (!attempt.running) return ''
  return fmtDuration(serverNow.value - from)
}

/** Why an attempt ended: the recorded verdict, else its retry class. */
function attemptWhy(attempt: MaestroAttempt): string | null {
  if (attempt.verdict) return attempt.verdict
  if (attempt.retry_class) return `retried as ${attempt.retry_class}`
  return null
}

function toggle(nodeId: string) {
  const next = new Set(open.value)
  if (next.has(nodeId)) next.delete(nodeId)
  else next.add(nodeId)
  open.value = next
}

/** A node opens itself when it is the one that needs looking at. */
function expanded(node: MaestroNode): boolean {
  if (open.value.has(node.node_id)) return true
  if (open.value.size > 0) return false
  return node.state === 'RUNNING' || node.state === 'BLOCKED'
}
</script>

<template>
  <div class="detail">
    <div v-if="notFound" class="empty-state error-state">
      run <code class="run-id">{{ runId }}</code> not found — it is no longer in this ledger
    </div>

    <template v-else>
      <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>
      <div v-if="!run" class="empty-state">loading run…</div>

      <template v-else>
      <section class="head">
        <div class="head-line">
          <MaestroStateChip :state="run.state" />
          <h1>{{ run.plan_name ?? 'plan not installed' }}</h1>
          <span v-if="run.cancel_requested" class="cancel-flag">
            <TriangleAlert :size="16" /> cancel requested
          </span>
        </div>
        <code class="run-id">{{ run.run_id }}</code>

        <div class="facts">
          <div class="fact">
            <span class="k">declared outcome</span>
            <span class="v" :class="{ none: !run.declared_outcome }">
              {{ run.declared_outcome ?? 'none yet' }}
              <template v-if="run.declared_outcome_at">
                · {{ fmtDate(run.declared_outcome_at) }}
              </template>
            </span>
          </div>
          <div v-if="run.declared_outcome === 'CANCELLED'" class="fact">
            <span class="k">cancel cause</span>
            <span class="v" :class="{ none: !run.cancel_cause }">
              {{ run.cancel_cause ?? 'not recorded' }} ·
              <template v-if="run.resumable">
                <code>maestro run resume</code> will take it
              </template>
              <template v-else>{{ notResumableWhy }}</template>
            </span>
          </div>
          <div class="fact">
            <span class="k">started</span>
            <span class="v">
              {{ fmtDate(run.created_at) }} ·
              {{ isLive ? 'running' : 'took' }} {{ elapsed }}
            </span>
          </div>
          <div class="fact">
            <span class="k">last transition</span>
            <span class="v">{{ fmtDate(run.last_transition_at) }} · {{ idle }} ago</span>
          </div>
          <div class="fact">
            <span class="k">plan digest</span>
            <span class="v mono">{{ run.plan_digest }}</span>
          </div>
        </div>

        <div class="integration" :class="{ missing: !run.integration }">
          <GitBranch :size="17" />
          <template v-if="run.integration">
            <span class="branch">{{ run.integration.branch ?? 'detached' }}</span>
            <code class="sha">{{ (run.integration.head ?? '').slice(0, 12) || '—' }}</code>
            <span class="subject">{{ run.integration.subject ?? '' }}</span>
            <code class="path">{{ run.integration.path }}</code>
          </template>
          <span v-else>no integration worktree on disk for this run</span>
        </div>

        <div v-if="inFlight.length" class="inflight">
          <span
            v-for="attempt in inFlight"
            :key="`${attempt.node_id}#${attempt.attempt_no}`"
            class="inflight-pill"
          >
            <span class="live-dot" />
            {{ attempt.node_id }} · attempt {{ attempt.attempt_no }} ·
            {{ attemptElapsed(attempt) }} · {{ attempt.turn_count }} turns
          </span>
        </div>
      </section>

      <section class="dag">
        <div v-for="column in columns" :key="column.depth" class="column">
          <div class="column-head">depth {{ column.depth }}</div>
          <article
            v-for="node in column.nodes"
            :key="node.node_id"
            class="node"
            :class="[node.state.toLowerCase(), { open: expanded(node) }]"
          >
            <button class="node-head" type="button" @click="toggle(node.node_id)">
              <ChevronRight class="caret" :class="{ down: expanded(node) }" :size="17" />
              <span class="node-id">{{ node.node_id }}</span>
              <span class="node-meta">
                <span class="kind">{{ node.kind ?? '?' }}</span>
                <MaestroStateChip :state="node.state" small />
              </span>
            </button>

            <div class="node-sub">
              <span class="attempt-count">
                attempt {{ node.attempt_no }}
                <template v-if="node.attempts.length > node.attempt_no">
                  of {{ node.attempts.length }} recorded
                </template>
                <template v-if="node.granted_extra_attempts">
                  · +{{ node.granted_extra_attempts }} granted
                </template>
              </span>
              <span v-if="node.attempts.length && !expanded(node)" class="attempt-hint">
                {{ node.attempts.length }}
                {{ node.attempts.length === 1 ? 'attempt' : 'attempts' }} — click to open
              </span>
              <code v-if="node.output_sha" class="sha" :title="node.output_sha">
                → {{ node.output_sha.slice(0, 12) }}
              </code>
            </div>

            <div v-if="node.needs.length" class="needs">
              <span class="needs-label">needs</span>
              <code v-for="dep in node.needs" :key="dep" class="dep">{{ dep }}</code>
            </div>

            <div v-if="node.block_reason" class="blocked-why">
              <TriangleAlert :size="15" /> {{ node.block_reason }}
            </div>

            <div v-if="node.cancel_cause" class="blocked-why">
              <Ban :size="15" /> cancelled · {{ node.cancel_cause }}
              <template v-if="node.cancel_cause === 'ABANDONED'">
                — the operator gave this node up; a resume does not reopen it
              </template>
            </div>

            <div v-if="expanded(node)" class="attempts">
              <div
                v-for="attempt in node.attempts"
                :key="attempt.attempt_no"
                class="attempt"
                :class="{ running: attempt.running }"
              >
                <div class="attempt-line">
                  <span class="a-no">a{{ attempt.attempt_no }}</span>
                  <MaestroStateChip :state="attempt.state" small />
                  <span class="a-turns">{{ attempt.turn_count }} turns</span>
                  <span v-if="attempt.running" class="a-elapsed">
                    {{ attemptElapsed(attempt) }}
                  </span>
                  <code v-if="attempt.base_sha" class="sha">
                    base {{ attempt.base_sha.slice(0, 8) }}
                  </code>
                </div>
                <div v-if="attemptWhy(attempt)" class="a-why">{{ attemptWhy(attempt) }}</div>
                <code v-if="attempt.session_path" class="a-session" :title="attempt.session_path">
                  {{ attempt.session_path }}
                </code>
              </div>
              <div v-if="!node.attempts.length" class="a-none">no attempt has started yet</div>
            </div>
          </article>
        </div>
      </section>

      <section v-if="run.results.length" class="results">
        <h2>results</h2>
        <div v-for="(result, i) in run.results" :key="i" class="result">
          <div class="result-head">
            <code>{{ result.node_id }}#{{ result.attempt_no }}</code>
            <span class="adjudication">{{ result.adjudication ?? 'unadjudicated' }}</span>
            <span class="dim">{{ fmtDate(result.created_at) }}</span>
          </div>
          <pre>{{ JSON.stringify(result.payload, null, 2) }}</pre>
        </div>
      </section>
      </template>
    </template>
  </div>
</template>

<style scoped>
.detail {
  padding: 18px 24px 40px;
  display: flex;
  flex-direction: column;
  gap: 22px;
}

.head {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px 20px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
}

.head-line {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.head-line h1 {
  margin: 0;
  font-size: 22px;
}

.cancel-flag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--amber);
  font-size: 15px;
}

.run-id {
  font-family: var(--mono);
  font-size: 14px;
  color: var(--faint);
}

.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 8px 24px;
}

.fact {
  display: flex;
  flex-direction: column;
}

.fact .k {
  font-size: 13px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--faint);
}

.fact .v {
  font-size: 16px;
  color: var(--dim);
  overflow-wrap: anywhere;
}

.fact .v.none {
  font-style: italic;
  color: var(--faint);
}

.mono {
  font-family: var(--mono);
  font-size: 14px;
}

.integration {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 9px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 9px;
  background: var(--panel-3);
  color: var(--dim);
  font-size: 15px;
}

.integration.missing {
  color: var(--faint);
  font-style: italic;
}

.integration .branch {
  color: var(--cyan);
  font-family: var(--mono);
}

.integration .subject {
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 50%;
}

.integration .path {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--faint);
}

.sha {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--amber);
}

.inflight {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.inflight-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 13px;
  border-radius: 999px;
  border: 1px solid rgba(108, 182, 255, 0.5);
  background: rgba(108, 182, 255, 0.08);
  color: var(--blue);
  font-size: 15px;
  font-family: var(--mono);
}

.live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--blue);
  animation: pulse 1.6s ease-in-out infinite;
}

.dag {
  display: flex;
  gap: 18px;
  align-items: flex-start;
  overflow-x: auto;
  padding-bottom: 6px;
}

.column {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 360px;
  flex: 1 1 360px;
}

.column-head {
  font-size: 13px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--faint);
}

.node {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--border);
  border-radius: 11px;
  background: var(--surface);
}

.node.running {
  border-left-color: var(--blue);
  box-shadow: 0 0 20px rgba(108, 182, 255, 0.1);
}

.node.merged,
.node.verified {
  border-left-color: var(--green);
}

.node.blocked,
.node.cancelled {
  border-left-color: var(--red);
}

.node-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--text);
  font: inherit;
  cursor: pointer;
  text-align: left;
}

.node-id {
  font-size: 17px;
  font-weight: 700;
  overflow-wrap: anywhere;
  flex: 1 1 auto;
}

.node-meta {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex: none;
}

.kind {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--purple);
}

.caret {
  flex: none;
  color: var(--faint);
  transition: transform 0.12s ease;
}

.caret.down {
  transform: rotate(90deg);
}

.node-sub {
  display: flex;
  gap: 12px;
  align-items: center;
  color: var(--dim);
  font-size: 15px;
}

.attempt-hint {
  color: var(--faint);
  font-size: 14px;
}

.needs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.needs-label {
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--faint);
}

.dep {
  font-family: var(--mono);
  font-size: 13px;
  padding: 1px 8px;
  border-radius: 6px;
  border: 1px solid var(--border-soft);
  color: var(--violet);
}

.blocked-why {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--red);
  font-family: var(--mono);
  font-size: 14px;
}

.attempts {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 6px;
  border-top: 1px solid var(--border-soft);
}

.attempt {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 7px 9px;
  border-radius: 8px;
  background: var(--panel-3);
}

.attempt.running {
  background: rgba(108, 182, 255, 0.07);
}

.attempt-line {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.a-no {
  font-family: var(--mono);
  font-size: 15px;
  color: var(--faint);
}

.a-turns,
.a-elapsed {
  font-family: var(--mono);
  font-size: 14px;
  color: var(--dim);
}

.a-why {
  color: var(--amber);
  font-size: 15px;
}

.a-session {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.a-none {
  color: var(--faint);
  font-size: 15px;
  font-style: italic;
}

.results h2 {
  margin: 0 0 10px;
  font-size: 18px;
}

.result {
  margin-bottom: 12px;
}

.result-head {
  display: flex;
  gap: 12px;
  align-items: center;
  font-family: var(--mono);
  font-size: 14px;
  margin-bottom: 6px;
}

.adjudication {
  color: var(--cyan);
}

.dim {
  color: var(--dim);
}

.empty-state {
  padding: 40px 0;
  color: var(--dim);
}

.error-bar {
  color: var(--red);
}

.error-state {
  color: var(--red);
  font-weight: 600;
}
</style>
