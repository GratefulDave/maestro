import { describe, expect, test } from 'bun:test'
import {
  MAX_STEPS_PER_LANE,
  RUN_LANE,
  appendSteps,
  emptyStepFeed,
  latestStep,
  stepsFor,
} from './stepFeed'
import type { StepLine, StepLogPage } from './types'

const RUN = 'f50638ab0000'

function step(over: Partial<StepLine> = {}): StepLine {
  return {
    ts: '2026-09-01T22:31:04.512+00:00',
    run_id: RUN,
    lane_id: 'lane-wp7-build',
    message: 'asking code reviewer',
    detail: '',
    ...over,
  }
}

function page(steps: StepLine[], over: Partial<StepLogPage> = {}): StepLogPage {
  return {
    present: true,
    steps,
    cursor: steps.length * 100,
    has_more: false,
    size: steps.length * 100,
    ...over,
  }
}

describe('step feed', () => {
  test('an untouched feed is empty and knows it has not been asked', () => {
    const feed = emptyStepFeed()
    expect(feed.loaded).toBe(false)
    expect(feed.present).toBe(false)
    expect(stepsFor(feed, 'lane-a')).toEqual([])
    expect(latestStep(feed, 'lane-a')).toBeNull()
  })

  test('a log the scheduler has not written is loaded and absent, not an error', () => {
    const feed = appendSteps(emptyStepFeed(), {
      present: false,
      steps: [],
      cursor: 0,
      has_more: false,
      size: 0,
    })
    expect(feed.loaded).toBe(true)
    expect(feed.present).toBe(false)
    expect(feed.byLane.size).toBe(0)
  })

  test('steps group by lane, oldest first', () => {
    const feed = appendSteps(
      emptyStepFeed(),
      page([
        step({ lane_id: RUN_LANE, message: 'run opened' }),
        step({ lane_id: 'lane-a', message: 'first' }),
        step({ lane_id: 'lane-b', message: 'other lane' }),
        step({ lane_id: 'lane-a', message: 'second' }),
      ]),
    )
    expect(stepsFor(feed, 'lane-a').map((s) => s.message)).toEqual(['first', 'second'])
    expect(stepsFor(feed, RUN_LANE).map((s) => s.message)).toEqual(['run opened'])
    expect(latestStep(feed, 'lane-a')?.message).toBe('second')
  })

  test('an empty page leaves what is on screen alone', () => {
    const first = appendSteps(emptyStepFeed(), page([step({ message: 'working' })]))
    const second = appendSteps(first, page([], { cursor: first.cursor, size: first.cursor }))
    expect(stepsFor(second, 'lane-wp7-build').map((s) => s.message)).toEqual(['working'])
    expect(second.cursor).toBe(first.cursor)
    // Nothing new means nothing re-rendered: the same map is handed back.
    expect(second.byLane).toBe(first.byLane)
  })

  test('appending returns a new map so the view actually updates', () => {
    const first = appendSteps(emptyStepFeed(), page([step({ message: 'one' })]))
    const second = appendSteps(first, page([step({ message: 'two' })], { cursor: 999 }))
    expect(second.byLane).not.toBe(first.byLane)
    expect(stepsFor(first, 'lane-wp7-build').map((s) => s.message)).toEqual(['one'])
    expect(stepsFor(second, 'lane-wp7-build').map((s) => s.message)).toEqual(['one', 'two'])
  })

  test('a lane keeps only its most recent steps', () => {
    let feed = emptyStepFeed()
    for (let i = 0; i < MAX_STEPS_PER_LANE + 25; i += 1) {
      feed = appendSteps(feed, page([step({ message: `step ${i}` })], { cursor: i + 1 }))
    }
    const kept = stepsFor(feed, 'lane-wp7-build')
    expect(kept).toHaveLength(MAX_STEPS_PER_LANE)
    // The newest survive, which is what the operator is looking at.
    expect(kept.at(-1)!.message).toBe(`step ${MAX_STEPS_PER_LANE + 24}`)
    expect(kept[0]!.message).toBe('step 25')
  })

  test('the cursor never moves backwards', () => {
    const first = appendSteps(emptyStepFeed(), page([step()], { cursor: 5000 }))
    const rewound = appendSteps(first, page([], { cursor: 12, size: 12 }))
    expect(rewound.cursor).toBe(5000)
  })

  test('a lane_id the writer left blank falls back to the run lane', () => {
    const feed = appendSteps(emptyStepFeed(), page([step({ lane_id: '', message: 'run opened' })]))
    expect(stepsFor(feed, RUN_LANE).map((s) => s.message)).toEqual(['run opened'])
  })
})
