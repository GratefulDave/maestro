import type { StepLine, StepLogPage } from './types'

/**
 * The scheduler's narration, accumulated across polls and grouped by lane.
 *
 * The server hands out only what is new past a byte cursor, so the running
 * feed lives here rather than in the response: a poll that returns nothing
 * must leave what is on screen alone, not blank it.
 *
 * This is display state over a display-only file. Nothing here is workflow
 * authority — the lane's stage comes from the ledger, and a step that
 * disagrees with it is stale narration, not a state change.
 */

/** The lane id the writer uses for a run-level step. */
export const RUN_LANE = '-'

/**
 * How many steps a lane keeps.
 *
 * A long-running lane narrates continuously and the operator is looking at
 * what it is doing *now*; an unbounded feed would grow without limit for the
 * life of the page to hold lines nobody scrolls back to.
 */
export const MAX_STEPS_PER_LANE = 80

export interface StepFeed {
  /** Whether the scheduler has written a step log at all. */
  present: boolean
  /** Byte offset the next poll asks from. */
  cursor: number
  /** Steps per lane, oldest first. Run-level steps are under `RUN_LANE`. */
  byLane: Map<string, StepLine[]>
  /** True once a page has come back, so "no steps" can be told from "not yet asked". */
  loaded: boolean
}

export function emptyStepFeed(): StepFeed {
  return { present: false, cursor: 0, byLane: new Map(), loaded: false }
}

/**
 * Fold one page into the feed, returning a new object.
 *
 * A new object rather than a mutation because the component holds this in a
 * `shallowRef`: mutating the map in place would update nothing on screen.
 */
export function appendSteps(feed: StepFeed, page: StepLogPage): StepFeed {
  const byLane = page.steps.length ? new Map(feed.byLane) : feed.byLane
  for (const step of page.steps) {
    const lane = step.lane_id || RUN_LANE
    const existing = byLane.get(lane)
    const next = existing ? [...existing, step] : [step]
    byLane.set(lane, next.length > MAX_STEPS_PER_LANE ? next.slice(-MAX_STEPS_PER_LANE) : next)
  }
  return {
    present: page.present,
    // The cursor only ever moves forward. A page read from a file that was
    // replaced under us reports a smaller cursor; taking it would re-deliver
    // lines already on screen.
    cursor: Math.max(feed.cursor, page.cursor),
    byLane,
    loaded: true,
  }
}

/** The steps for one lane, oldest first — newest is last. */
export function stepsFor(feed: StepFeed, laneId: string): StepLine[] {
  return feed.byLane.get(laneId) ?? []
}

/** The most recent step for one lane, which is what a collapsed card shows. */
export function latestStep(feed: StepFeed, laneId: string): StepLine | null {
  return feed.byLane.get(laneId)?.at(-1) ?? null
}
