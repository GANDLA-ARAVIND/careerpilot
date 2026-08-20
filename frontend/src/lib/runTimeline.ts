/**
 * Turns a flat stream of ProgressEvents into the four-stage timeline
 * Mission Control renders.
 *
 * Separated from the component because the state rules have real subtleties
 * worth stating once, in one place:
 *
 * - Stages can be REVISITED. print_analyst_stage2 re-runs stage 1 as a
 *   ranking input before the deep pass (see pipeline.py), so "stage1"
 *   events arrive in two bursts with "stage2" after them. State is
 *   therefore derived from the highest-sequence event, never from "have I
 *   seen this stage before".
 *
 * - A stage that never emits is SKIPPED, not waiting. route_after_fetch
 *   ends the run early when nothing new arrived, and route_after_stage1
 *   skips the deep pass when stage 1 produced nothing rankable. Once the
 *   run is over, showing those as "waiting" would imply work still to come
 *   that is never coming.
 */

import type { RunEvent } from "@/api/client"

export type StageState = "waiting" | "running" | "completed" | "skipped"

export interface StageDefinition {
  id: string
  label: string
  /** null where no agent is involved - fetch and filter are pipeline
   *  stages, not agents. Mirrors ProgressEvent.agent on the backend. */
  agent: string | null
  description: string
}

export const STAGES: StageDefinition[] = [
  {
    id: "fetch",
    label: "Fetch",
    agent: null,
    description: "Pull open postings from every configured ATS board.",
  },
  {
    id: "filter",
    label: "Filter",
    agent: null,
    description: "Rule filters on title, seniority and location. No LLM.",
  },
  {
    id: "stage1",
    label: "Screening pass",
    agent: "Analyst",
    description: "Cheap model scores every survivor against the resume.",
  },
  {
    id: "stage2",
    label: "Deep pass",
    agent: "Analyst",
    description: "Stronger model re-checks the top-ranked survivors.",
  },
]

export interface StageView {
  definition: StageDefinition
  state: StageState
  message: string | undefined
  current: number | null | undefined
  total: number | null | undefined
  eventCount: number
  lastSeq: number
}

export interface QuotaUsage {
  model: string
  calls: number
  limit: number | null
}

export function buildTimeline(events: RunEvent[], runState: string): StageView[] {
  const runFinished = runState === "completed" || runState === "failed"
  const latest = events.length > 0 ? events[events.length - 1] : undefined

  return STAGES.map((definition) => {
    const forStage = events.filter((event) => event.stage === definition.id)
    const last = forStage[forStage.length - 1]

    let state: StageState
    if (forStage.length === 0) {
      state = runFinished ? "skipped" : "waiting"
    } else if (!runFinished && latest?.stage === definition.id) {
      state = "running"
    } else {
      state = "completed"
    }

    return {
      definition,
      state,
      message: last?.message,
      current: last?.current,
      total: last?.total,
      eventCount: forStage.length,
      lastSeq: last?.seq ?? 0,
    }
  })
}

/**
 * Quota per model, from the per-job events the Analyst emits.
 *
 * `call_count` is cumulative within a run, so the highest value seen for a
 * model is that model's spend - not a sum across events, which would count
 * the same call once per subsequent event.
 */
export function buildQuota(events: RunEvent[]): QuotaUsage[] {
  const byModel = new Map<string, QuotaUsage>()

  for (const event of events) {
    const extra = (event.extra ?? {}) as Record<string, unknown>
    const model = typeof extra.model === "string" ? extra.model : undefined
    const calls = typeof extra.call_count === "number" ? extra.call_count : undefined
    if (!model || calls === undefined) continue

    const limit = typeof extra.rpd === "number" ? extra.rpd : null
    const existing = byModel.get(model)
    if (!existing || calls > existing.calls) {
      byModel.set(model, { model, calls, limit })
    }
  }

  return Array.from(byModel.values())
}

/** Cache hits vs fresh LLM calls, counted from the per-job events. */
export function buildCacheStats(events: RunEvent[]): { cached: number; fresh: number } {
  let cached = 0
  let fresh = 0
  for (const event of events) {
    const source = (event.extra ?? {})["source" as keyof typeof event.extra]
    if (source === "cache") cached += 1
    else if (source === "fresh") fresh += 1
  }
  return { cached, fresh }
}
