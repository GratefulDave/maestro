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
import { Ban, ChevronRight, GitBranch, TriangleAlert, UserRound } from 'lucide-vue-next'
import type { MaestroAttempt, MaestroNode, MaestroRunDetail } from '../lib/types'
import { ApiHttpError, fetchRun } from '../lib/api'
import { candidateLifecycleForNode, nodeAuthorityState } from '../lib/maestroLifecycle'
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

const candidateLifecycleByNode = computed(
  () =>
    new Map(
      (run.value?.nodes ?? []).map((node) => [
        node.node_id,
        run.value ? candidateLifecycleForNode(run.value, node) : [],
      ]),
    ),
)

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

function mergedRejecting(node: MaestroNode): MaestroAttempt['review_findings'] {
  if (node.state !== 'MERGED') return []
  const attempt = node.attempts.find((item) => item.attempt_no === node.attempt_no)
  return attempt?.review_findings ?? []
}

const rejectingMerges = computed(() =>
  (run.value?.nodes ?? [])
    .map((node) => ({ node, findings: mergedRejecting(node) }))
    .filter((item) => item.findings.length > 0),
)
function expanded(node: MaestroNode): boolean {
  if (open.value.has(node.node_id)) return true
  if (open.value.size > 0) return false
  return (
    ['BLOCKED', 'RUNNING', 'CANDIDATE_READY', 'REVIEWING', 'REPAIR_HANDOFF', 'REPAIRING', 'WAITING_FOR_NEW_CANDIDATE'].includes(nodeAuthorityState(node)) ||
    mergedRejecting(node).length > 0
  )
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

        <div
          v-if="run.declared_outcome === 'ACCEPTED' && rejectingMerges.length"
          class="findings-banner"
        >
          ACCEPTED with rejecting findings on
          {{ rejectingMerges.length }}
          merged {{ rejectingMerges.length === 1 ? 'node' : 'nodes' }}
          — review advised; it did not adjudicate
          <ul>
            <li v-for="item in rejectingMerges" :key="item.node.node_id">
              <code>{{ item.node.node_id }}</code>
              · a{{ item.node.attempt_no }} ·
              {{ item.findings.length }} blocking
              <div v-for="(finding, i) in item.findings" :key="i">
                <code>{{ finding.check_id }}</code>
                {{ finding.object_id }}
                — {{ finding.message }}
              </div>
            </li>
          </ul>
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
            :class="[nodeAuthorityState(node).toLowerCase(), { open: expanded(node) }]"
          >
            <button class="node-head" type="button" @click="toggle(node.node_id)">
              <ChevronRight class="caret" :class="{ down: expanded(node) }" :size="17" />
              <span class="node-id">{{ node.node_id }}</span>
              <span class="node-meta">
                <span class="kind">{{ node.kind ?? '?' }}</span>
                <MaestroStateChip :state="nodeAuthorityState(node)" small />
              </span>
              <span v-if="node.lane_phase" class="node-ledger-state">
                node {{ node.state.toLowerCase() }}
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

            <!--
              A MERGED node says how it got there. The state chip still reads
              MERGED, because that is the state and the frontier keys on it;
              this line is the provenance the chip cannot carry. Rendered only
              where there is something to say — a node the run merged is the
              ordinary case and needs no annotation.
            -->
            <div v-if="node.merge_cause === 'OPERATOR_ACCEPTED'" class="accepted-why">
              <UserRound :size="15" /> operator-accepted — the operator
              supplied this work by hand and proved its identity; the run did
              not establish an evidence chain for it
            </div>


            <div v-if="mergedRejecting(node).length" class="findings-why">
              merged with {{ mergedRejecting(node).length }}
              rejecting {{ mergedRejecting(node).length === 1 ? 'finding' : 'findings' }}
            </div>
            <div v-if="node.merge_cause === 'UNRECORDED'" class="accepted-why">
              <TriangleAlert :size="15" /> merged before this ledger recorded
              how — run-merged or operator-accepted cannot be told apart here
            </div>

            <div
              v-if="candidateLifecycleByNode.get(node.node_id)?.length"
              class="review-ledger"
            >
              <span class="review-ledger-title">candidate review authority</span>
              <div
                v-for="item in candidateLifecycleByNode.get(node.node_id)"
                :key="item.candidate.candidate_sha"
                class="candidate-row"
              >
                <div class="candidate-head">
                  <span>c{{ item.candidate.candidate_seq }}</span>
                  <code :title="item.candidate.candidate_sha">
                    {{ item.candidate.candidate_sha.slice(0, 12) }}
                  </code>
                  <MaestroStateChip :state="item.review?.state ?? 'NOT_DISPATCHED'" small />
                  <strong v-if="item.review?.verdict">{{ item.review.verdict }}</strong>
                </div>
                <details v-if="item.review?.findings.length" class="candidate-findings">
                  <summary>{{ item.review.findings.length }} blocking findings</summary>
                  <ul class="a-findings">
                    <li v-for="(finding, i) in item.review.findings" :key="i">
                      <code>{{ finding.check_id }}</code>
                      {{ finding.object_id }}
                      <div>{{ finding.message }}</div>
                    </li>
                  </ul>
                </details>
                <div v-if="item.handoff" class="handoff-state">
                  repair handoff · {{ item.handoff.state }} · builder g{{ item.handoff.builder_generation }}
                </div>
              </div>
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
                <ul v-if="attempt.review_findings.length" class="a-findings">
                  <li v-for="(finding, i) in attempt.review_findings" :key="i">
                    <code>{{ finding.check_id }}</code>
                    {{ finding.object_id }}
                    <div>{{ finding.message }}</div>
                  </li>
                </ul>
              </div>
              <div v-if="!node.attempts.length" class="a-none">no attempt has started yet</div>
            </div>
          </article>
        </div>
      </section>

      <section v-if="run.results.length" class="results">
        <h2>results</h2>
        <!--
          `adjudication` says the agent's declared envelope was accepted as
          well-formed. It is not the reviewer's verdict on the work, and a
          bare ACCEPTED badge here read as one: in
          run-9e9ac412669140039ae078601048f6c7 all nine result rows showed
          ACCEPTED, including `lane-p2-s3-inventory` a2, which was settled
          retry:SEMANTIC on the blocking check
          `diff.implements_the_stated_instruction` at the same time. The
          review verdict lives in the transition's detail_json, not here.
        -->
        <p class="results-note">
          envelope adjudication only — whether the agent's declared result
          parsed and bound to this attempt. The reviewer's verdict is on the
          node's transitions, not here.
        </p>
        <div v-for="(result, i) in run.results" :key="i" class="result">
          <div class="result-head">
            <code>{{ result.node_id }}#{{ result.attempt_no }}</code>
            <span
              class="adjudication"
              title="Envelope adjudication: the agent's declared result was accepted as well-formed and bound to this attempt. Not the code reviewer's verdict."
            >envelope {{ result.adjudication ?? 'unadjudicated' }}</span>
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

.node.reviewing,
.node.candidate_ready,
.node.waiting_for_new_candidate {
  border-left-color: var(--blue);
}

.node.repair_handoff,
.node.repairing {
  border-left-color: var(--amber);
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


.node-ledger-state {
  color: var(--faint);
  font-family: var(--mono);
  font-size: 12px;
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

/*
 * Amber rather than red. An operator-accepted node is not a failure and must
 * not read as one — it is a node whose evidence chain the run did not
 * establish, which is a thing to know rather than a thing to fix.
 */
.accepted-why {
  display: inline-flex;
  align-items: flex-start;
  gap: 7px;
  color: var(--amber);
  font-family: var(--mono);
  font-size: 13px;
  line-height: 1.45;
}

.findings-banner {
  padding: 10px 14px;
  border: 1px solid rgba(232, 168, 56, 0.45);
  border-radius: 10px;
  background: rgba(232, 168, 56, 0.08);
  color: var(--amber);
  font-size: 15px;
  line-height: 1.45;
}

.findings-banner ul {
  margin: 8px 0 0;
  padding-left: 18px;
}

.findings-why {
  display: inline-flex;
  align-items: flex-start;
  gap: 7px;
  color: var(--amber);
  font-family: var(--mono);
  font-size: 13px;
}

.a-findings {
  margin: 4px 0 0;
  padding-left: 18px;
  color: var(--amber);
  font-size: 14px;
}

.a-findings code {
  font-family: var(--mono);
  font-size: 13px;
}

.review-ledger {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-soft);
}

.review-ledger-title {
  color: var(--faint);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.candidate-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px;
  background: var(--panel-3);
  border-radius: 8px;
}

.candidate-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-family: var(--mono);
  font-size: 13px;
}

.candidate-head strong {
  color: var(--text);
}

.handoff-state {
  color: var(--amber);
  font-family: var(--mono);
  font-size: 13px;
}

.candidate-findings summary {
  color: var(--amber);
  cursor: pointer;
  font-size: 13px;
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

/* Deliberately dim rather than a warning colour: the note is orientation, not
   an alert. What it must not be is invisible, since its whole job is to stop
   the badge beside it being read as the reviewer's verdict. */
.results-note {
  margin: 0 0 10px;
  color: var(--dim);
  max-width: 70ch;
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
