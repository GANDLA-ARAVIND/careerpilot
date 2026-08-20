import { useState } from "react"
import { ChevronRight } from "lucide-react"

import type { AgentMetric, RunHistoryEntry } from "@/api/client"
import { api } from "@/api/client"
import { EmptyState, ErrorState, PageHeader } from "@/components/PageHeader"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { formatAge, formatDateTime } from "@/lib/format"
import { useApi } from "@/lib/useApi"
import { cn } from "@/lib/utils"

/**
 * Renders a metric that may legitimately be unmeasured.
 *
 * null means the system does not measure this, and says so. It never
 * renders as 0 - `retries` in particular is always null because LangGraph
 * retries nodes silently, and a 0 would be a confident claim that no
 * retry happened.
 */
function Metric({
  label,
  value,
  suffix,
  format,
}: {
  label: string
  value: number | string | null | undefined
  suffix?: string
  format?: (n: number) => string
}) {
  const unmeasured = value === null || value === undefined
  return (
    <div className="flex justify-between gap-2 border-b border-dashed py-1">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={cn("text-right", unmeasured ? "italic text-muted-foreground" : "font-medium tabular-nums")}>
        {unmeasured
          ? "not measured"
          : typeof value === "number"
            ? `${format ? format(value) : value}${suffix ?? ""}`
            : value}
      </dd>
    </div>
  )
}

function StageCard({ stage }: { stage: AgentMetric }) {
  return (
    <div className="rounded-md border bg-muted/20 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <span className="text-xs font-semibold">{stage.stage}</span>
        {stage.agent ? (
          <Badge variant="secondary" className="text-[0.6rem]">
            {stage.agent}
          </Badge>
        ) : (
          <span className="text-[0.6rem] uppercase tracking-wide text-muted-foreground">pipeline stage</span>
        )}
      </div>
      <dl className="grid grid-cols-1 gap-x-5 text-xs sm:grid-cols-2">
        <Metric label="Model" value={stage.model ?? null} />
        <Metric label="Duration" value={stage.duration_seconds} suffix="s" format={(n) => n.toFixed(2)} />
        <Metric label="LLM calls" value={stage.llm_calls} />
        <Metric label="Retries" value={stage.retries} />
        <Metric label="Cache hits" value={stage.cache_hits} />
        <Metric label="Cache misses" value={stage.cache_misses} />
        <Metric
          label="Cache hit rate"
          value={stage.cache_hit_rate}
          format={(n) => `${(n * 100).toFixed(0)}%`}
        />
        <Metric label="Companies checked" value={stage.companies_checked} />
        <Metric label="Jobs retrieved" value={stage.jobs_retrieved} />
        <Metric label="Jobs processed" value={stage.jobs_processed} />
      </dl>
    </div>
  )
}

function RunRow({ run, defaultOpen }: { run: RunHistoryEntry; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div className="rounded-lg border">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-accent/50"
      >
        <ChevronRight className={cn("size-4 shrink-0 transition-transform", open && "rotate-90")} />
        <span
          className={cn(
            "size-2 shrink-0 rounded-full",
            run.status === "completed" && "bg-emerald-500",
            run.status === "failed" && "bg-destructive",
            run.status === "running" && "animate-pulse bg-primary",
          )}
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{formatDateTime(run.started_at)}</span>
            <Badge variant="secondary" className="text-[0.6rem] capitalize">
              {run.status}
            </Badge>
            {run.trigger && (
              <span className="text-[0.65rem] uppercase tracking-wide text-muted-foreground">
                via {run.trigger}
              </span>
            )}
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {(run.stages ?? []).length} stage(s) · {run.total_llm_calls} LLM call(s) ·{" "}
            {run.duration_seconds == null ? (
              <span className="italic">duration not measured</span>
            ) : (
              `${run.duration_seconds.toFixed(1)}s`
            )}
          </p>
          {run.error && <p className="mt-0.5 text-xs text-destructive">{run.error}</p>}
        </div>
        <code className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[0.6rem] text-muted-foreground">
          {run.run_id}
        </code>
      </button>

      {open && (
        <div className="border-t px-4 py-3">
          {(run.stages ?? []).length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No stage metrics recorded — the run ended before any stage reported.
            </p>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {(run.stages ?? []).map((stage, i) => (
                <StageCard key={`${stage.stage}-${i}`} stage={stage} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function AgentMetricsPage() {
  const { data, error, loading, reload } = useApi(() => api.meta.runs(20), [])
  const runtime = useApi(() => api.meta.runtime(), [])

  return (
    <div>
      <PageHeader
        title="Agent Metrics"
        description="What each recorded run actually did, stage by stage."
        actions={
          <button type="button" onClick={reload} className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent">
            Refresh
          </button>
        }
      />

      {error && <ErrorState error={error} onRetry={reload} />}
      {loading && !error && <p className="text-sm text-muted-foreground">Loading…</p>}

      {data && (data.runs ?? []).length === 0 && (
        <EmptyState
          title="No runs recorded yet"
          body="Start one from Mission Control. Runs launched from the CLI spend the same quota but don't write a metrics row."
        />
      )}

      {data && (data.runs ?? []).length > 0 && (
        <div className="space-y-2.5">
          {(data.runs ?? []).map((run, i) => (
            <RunRow key={run.run_id} run={run} defaultOpen={i === 0} />
          ))}
        </div>
      )}

      {runtime.data && (
        <Card className="mt-5 shadow-sm">
          <CardContent className="px-4 py-3.5">
            <h2 className="mb-2 text-sm font-semibold">Runtime</h2>
            <dl className="grid gap-x-6 text-xs sm:grid-cols-2 lg:grid-cols-3">
              <Metric label="Test functions" value={runtime.data.test_count} />
              {/* null on Postgres - there is no local file to stat, so Metric
                  renders "not measured" rather than a misleading "0.0 MB". */}
              <Metric
                label="Database size"
                value={runtime.data.db_size_bytes}
                format={(n) => `${(n / 1024 / 1024).toFixed(1)} MB`}
              />
              <Metric label="Database" value={runtime.data.db_backend} />
              <Metric label="Jobs stored" value={runtime.data.job_count} />
              <Metric label="Analyst results" value={runtime.data.analyst_result_count} />
              <Metric label="Tokens recorded" value={runtime.data.total_tokens_recorded} />
              {/* Data freshness read from the database (the newest completed
                  run's finish time), not a local file mtime - the API and the
                  process that writes the data are different machines once
                  this is deployed. null = no completed run on record. */}
              <Metric
                label="Last successful run"
                value={
                  runtime.data.last_successful_run
                    ? `${formatDateTime(runtime.data.last_successful_run)} (${formatAge(
                        runtime.data.last_successful_run,
                      )} ago)`
                    : null
                }
              />
            </dl>

            <h3 className="mb-1.5 mt-3 text-xs font-semibold">Quota today</h3>
            <ul className="space-y-1">
              {(runtime.data.quota ?? []).map((q) => (
                <li key={q.model} className="flex items-baseline justify-between gap-2 text-xs">
                  <span className="truncate text-muted-foreground">{q.model}</span>
                  <span className="shrink-0 font-medium tabular-nums">
                    {q.calls_today} / {q.daily_limit}
                  </span>
                </li>
              ))}
            </ul>

            {(runtime.data.caveats ?? []).length > 0 && (
              <ul className="mt-3 space-y-1 border-t pt-2">
                {(runtime.data.caveats ?? []).map((c) => (
                  <li key={c} className="text-[0.68rem] leading-relaxed text-muted-foreground">
                    — {c}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      {data?.notes && data.notes.length > 0 && (
        <ul className="mt-4 space-y-1">
          {data.notes.map((n) => (
            <li key={n} className="text-[0.68rem] leading-relaxed text-muted-foreground">
              — {n}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
