<script setup lang="ts">
/**
 * One dashboard over every factory database the server was pointed at.
 *
 * `/api/sources` says which schema each database holds, and the view is chosen
 * from that `kind` — the SSSF tracer's session waterfall, or Maestro's DAG run
 * view. Neither runtime writes the other's schema; adding a third is a kind, a
 * reader, and a view here.
 *
 * With a single source the tab strip stays hidden, so the tracer-only case
 * looks and behaves exactly as it did.
 */
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import type { SourceInfo } from './lib/types'
import { fetchSources } from './lib/api'
import { useRoute, hrefFor, hrefForSource, phaseCrumb } from './lib/router'
import SessionsList from './components/SessionsList.vue'
import SessionTrace from './components/SessionTrace.vue'
import MaestroRunsList from './components/MaestroRunsList.vue'
import MaestroRunDetail from './components/MaestroRunDetail.vue'

const route = useRoute()
const sources = shallowRef<SourceInfo[]>([])
const sourcesLoaded = ref(false)

let timer: ReturnType<typeof setInterval> | undefined

async function loadSources() {
  try {
    sources.value = await fetchSources()
    sourcesLoaded.value = true
  } catch {
    // The sessions/runs views already surface an unreachable API; the tab
    // strip simply stays as it was rather than flickering an error of its own.
  }
}

onMounted(() => {
  void loadSources()
  // Slow poll: sources change only when the server is restarted with different
  // databases, but picking that up beats making the operator reload.
  timer = setInterval(() => void loadSources(), 15000)
})
onUnmounted(() => clearInterval(timer))

const tracerSource = computed(() => sources.value.find((s) => s.kind === 'sssf') ?? null)

/** The source the current route is in, when it names one. */
const activeSource = computed(
  () => sources.value.find((s) => s.id === route.value.sourceId) ?? null,
)

/**
 * Where a bare `#/` lands.
 *
 * A tracer database keeps the historical home, so nothing moves for an
 * existing install. With only Maestro ledgers loaded there is no session list
 * to show, so the first of them becomes the landing view instead of an empty
 * page telling the operator to run an ADW they are not running.
 */
const fallbackMaestro = computed(() =>
  !tracerSource.value ? (sources.value.find((s) => s.kind === 'maestro') ?? null) : null,
)

const showTabs = computed(() => sources.value.length > 1)
</script>

<template>
  <div class="app">
    <header class="topbar">
      <nav class="crumbs">
        <!-- Inline copy of public/logo.svg (the favicon) so the mark renders
             crisply with no fetch; keep the two in sync. -->
        <svg class="logo" viewBox="0 0 32 32" aria-hidden="true">
          <rect x="4" y="6" width="17" height="5" rx="2.5" fill="#e8b64a" />
          <rect x="8" y="13.5" width="20" height="5" rx="2.5" fill="#c89bff" />
          <rect x="4" y="21" width="13" height="5" rx="2.5" fill="#5ad2dd" />
        </svg>
        <span class="brand">Super Simple Software Factory</span>
        <template v-if="activeSource">
          <span class="sep">›</span>
          <a
            :href="hrefForSource(activeSource.id)"
            :class="{ current: !route.runId }"
            >{{ activeSource.label }} runs</a
          >
          <template v-if="route.runId">
            <span class="sep">›</span>
            <span class="current">{{ route.runId }}</span>
          </template>
        </template>
        <template v-else>
          <span class="sep">›</span>
          <a :href="hrefFor()" :class="{ current: !route.adwId }">sessions</a>
          <template v-if="route.adwId">
            <span class="sep">›</span>
            <a :href="hrefFor(route.adwId)" :class="{ current: !route.phaseId }">{{
              route.adwId
            }}</a>
          </template>
          <template v-if="route.adwId && route.phaseId">
            <span class="sep">›</span>
            <span class="current">{{ phaseCrumb ?? route.phaseId }}</span>
          </template>
        </template>
      </nav>
      <span class="live-hint"><span class="live-dot" /> live</span>
    </header>

    <!-- One strip per loaded database. Hidden entirely when there is only one,
         so a tracer-only install is untouched. -->
    <nav v-if="showTabs" class="tabs">
      <a
        v-for="source in sources"
        :key="source.id"
        class="tab"
        :class="{ current: source.kind === 'sssf' ? !route.sourceId : route.sourceId === source.id }"
        :href="source.kind === 'sssf' ? hrefFor() : hrefForSource(source.id)"
      >
        <span class="tab-kind">{{ source.kind }}</span>
        {{ source.label }}
        <span class="tab-count">{{ source.count }}</span>
      </a>
    </nav>

    <main>
      <template v-if="activeSource">
        <MaestroRunDetail
          v-if="route.runId"
          :key="`${activeSource.id}/${route.runId}`"
          :source-id="activeSource.id"
          :run-id="route.runId"
        />
        <MaestroRunsList v-else :key="activeSource.id" :source-id="activeSource.id" />
      </template>
      <div v-else-if="route.sourceId && sourcesLoaded" class="unknown-source">
        no source “{{ route.sourceId }}” is loaded — this server is serving
        {{ sources.map((s) => s.id).join(', ') || 'nothing' }}
      </div>
      <MaestroRunsList
        v-else-if="fallbackMaestro && !route.adwId"
        :key="fallbackMaestro.id"
        :source-id="fallbackMaestro.id"
      />
      <SessionsList v-else-if="!route.adwId" />
      <SessionTrace v-else :key="route.adwId" :adw-id="route.adwId" :phase-id="route.phaseId" />
    </main>
  </div>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 15px 28px;
  background: rgba(11, 15, 24, 0.72);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  position: sticky;
  top: 0;
  z-index: 10;
}

/* Gradient hairline instead of a hard border — the brand colors, whispered. */
.topbar::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 1px;
  background: linear-gradient(
    90deg,
    rgba(200, 155, 255, 0.45),
    rgba(90, 210, 221, 0.35) 40%,
    rgba(90, 210, 221, 0.06)
  );
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 17px;
  min-width: 0;
}

.logo {
  width: 28px;
  height: 28px;
  flex: none;
  filter: drop-shadow(0 0 8px rgba(200, 155, 255, 0.35));
}

.brand {
  background: linear-gradient(90deg, var(--purple), var(--cyan));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  font-weight: 700;
  letter-spacing: 0.05em;
  white-space: nowrap;
}

.sep {
  color: var(--faint);
}

.crumbs a {
  color: var(--dim);
}

.crumbs a:hover {
  color: var(--text);
}

.crumbs .current {
  color: var(--text);
}

.live-hint {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--dim);
  font-size: 16px;
  white-space: nowrap;
}

.live-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 10px rgba(74, 222, 128, 0.7);
  animation: pulse 1.6s ease-in-out infinite;
}

.tabs {
  display: flex;
  gap: 8px;
  padding: 10px 24px 0;
  flex-wrap: wrap;
}

.tab {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--dim);
  font-size: 16px;
}

.tab:hover {
  border-color: var(--violet);
  color: var(--text);
}

.tab.current {
  color: var(--text);
  border-color: var(--purple);
  background: rgba(200, 155, 255, 0.09);
}

.tab-kind {
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: var(--faint);
}

.tab-count {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--faint);
}

.unknown-source {
  padding: 40px 24px;
  color: var(--dim);
}
</style>
