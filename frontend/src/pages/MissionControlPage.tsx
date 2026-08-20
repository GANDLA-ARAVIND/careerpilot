import { useCallback, useMemo, useState } from "react"
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  CircleDashed,
  Loader2,
  MinusCircle,
  Play,
  Radio,
  RotateCw,
} from "lucide-react"
import { toast } from "sonner"

import type { AgentMetric } from "@/api/client"
import { ApiError, api } from "@/api/client"
import { PageHeader } from "@/components/PageHeader"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { formatDate } from "@/lib/format"
import { useApi } from "@/lib/useApi"
import { useMode } from "@/lib/mode"
import { buildCacheStats, buildQuota, buildTimeline } from "@/lib/runTimeline"
import type { StageState, StageView } from "@/lib/runTimeline"
import { useRunStream } from "@/lib/useRunStream"
import { cn } from "@/lib/utils"

const STATE_STYLES: Record<StageState, { dot: string; label: string; text: string }> = {
  waiting: { dot: "bg-muted-foreground/25", label: "Waiting", text: "text-muted-foreground" },
  running: { dot: "bg-primary animate-pulse", label: "Running", text: "text-foreground" },
  completed: { dot: "bg-emerald-500", label: "Completed", text: "text-foreground" },
  skipped: { dot: "bg-muted-foreground/25", label: "Skipped", text: "text-muted-foreground" },
}

function StateIcon({ state }: { state: StageState }) {
  if (state === "running") return <Loader2 className="size-4 animate-spin text-primary" />
  if (state === "completed") return <CheckCircle2 className="size-4 text-emerald-600" />
  if (state === "skipped") return <MinusCircle className="size-4 text-muted-foreground" />
  return <CircleDashed className="size-4 text-muted-foreground/50" />
}

/**
 * A metric value that may legitimately be unmeasured.
 *
 * null renders as "not measured", never as 0 - `retries` in particular is
 * null by design because LangGraph retries silently and nothing observes
 * them (see db.py's RunAgentMetricsRow). Printing 0 there would be a
 * confident claim that no retry happened, which is not what the data says.
 */
function MetricValue({ value, suffix }: { value: number | null | undefined; suffix?: string }) {
  if (value === null || value === undefined) {
    return <span className="text-muted-foreground italic">not measured</span>
  }
  return (
    <span className="font-medium tabular-nums">
      {typeof value === "number" && !Number.isInteger(value) ? value.toFixed(2) : value}
      {suffix}
    </span>
  )
}

function StageMetrics({ metric }: { metric: AgentMetric | undefined }) {
  if (!metric) {
    return (
      <p className="text-xs text-muted-foreground">
        No recorded metrics for this stage in the last run.
      </p>
    )
  }

  const rows: Array<{ label: string; node: React.ReactNode }> = [
    { label: "Model", node: metric.model ?? <span className="italic text-muted-foreground">n/a</span> },
    { label: "LLM calls", node: <MetricValue value={metric.llm_calls} /> },
    { label: "Cache hits", node: <MetricValue value={metric.cache_hits} /> },
    { label: "Cache misses", node: <MetricValue value={metric.cache_misses} /> },
    {
      label: "Cache hit rate",
      node:
        metric.cache_hit_rate === null || metric.cache_hit_rate === undefined ? (
          <span className="italic text-muted-foreground">not measured</span>
        ) : (
          <span className="font-medium tabular-nums">{(metric.cache_hit_rate * 100).toFixed(0)}%</span>
        ),
    },
    { label: "Retries", node: <MetricValue value={metric.retries} /> },
    { label: "Duration", node: <MetricValue value={metric.duration_seconds} suffix="s" /> },
    { label: "Companies checked", node: <MetricValue value={metric.companies_checked} /> },
    { label: "Jobs retrieved", node: <MetricValue value={metric.jobs_retrieved} /> },
    { label: "Jobs processed", node: <MetricValue value={metric.jobs_processed} /> },
  ]

  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-xs sm:grid-cols-3">
      {rows.map(({ label, node }) => (
        <div key={label} className="flex justify-between gap-2 border-b border-dashed py-1">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="text-right">{node}</dd>
        </div>
      ))}
    </dl>
  )
}

function StageRow({
  stage,
  metric,
  isRecruiter,
  isLast,
}: {
  stage: StageView
  metric: AgentMetric | undefined
  isRecruiter: boolean
  isLast: boolean
}) {
  const [expanded, setExpanded] = useState(false)
  const styles = STATE_STYLES[stage.state]
  const showProgress = stage.current != null && stage.total != null && stage.total > 0
  const pct = showProgress ? Math.round((stage.current! / stage.total!) * 100) : 0

  return (
    <li className="relative flex gap-3 pb-5">
      {/* Connector line down to the next stage */}
      {!isLast && <span className="absolute left-[7px] top-6 h-full w-px bg-border" aria-hidden />}
      <span className={cn("relative z-10 mt-1.5 size-3.5 shrink-0 rounded-full ring-4 ring-background", styles.dot)} />

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <StateIcon state={stage.state} />
          <span className={cn("text-sm font-medium", styles.text)}>{stage.definition.label}</span>
          {stage.definition.agent && (
            <Badge variant="secondary" className="text-[0.6rem]">
              {stage.definition.agent}
            </Badge>
          )}
          <span className="text-[0.65rem] uppercase tracking-wide text-muted-foreground">{styles.label}</span>
          {showProgress && (
            <span className="ml-auto text-xs tabular-nums text-muted-foreground">
              {stage.current} / {stage.total}
            </span>
          )}
        </div>

        <p className="mt-0.5 text-xs text-muted-foreground">
          {stage.message ?? stage.definition.description}
        </p>

        {showProgress && (
          <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              className={cn("h-full rounded-full transition-all", stage.state === "running" ? "bg-primary" : "bg-emerald-500")}
              style={{ width: `${pct}%` }}
            />
          </div>
        )}

        {isRecruiter && (
          <div className="mt-2">
            <button
              type="button"
              onClick={() => setExpanded((e) => !e)}
              className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
            >
              <ChevronRight className={cn("size-3 transition-transform", expanded && "rotate-90")} />
              {expanded ? "Hide" : "Show"} stage metrics
            </button>
            {expanded && (
              <div className="mt-2 rounded-md border bg-muted/30 p-3">
                <StageMetrics metric={metric} />
              </div>
            )}
          </div>
        )}
      </div>
    </li>
  )
}

export function MissionControlPage() {
  const { isRecruiter } = useMode()
  const { events, status, connection, runState, refreshStatus } = useRunStream()
  const [starting, setStarting] = useState(false)

  const agents = useApi(() => api.meta.agents(), [runState])

  const timeline = useMemo(() => buildTimeline(events, runState), [events, runState])
  const quota = useMemo(() => buildQuota(events), [events])
  const cacheStats = useMemo(() => buildCacheStats(events), [events])

  const metricByStage = useMemo(() => {
    const map = new Map<string, AgentMetric>()
    // Only attach metrics that belong to the run currently on screen -
    // otherwise a fresh run would show the previous run's numbers under
    // its stages, which would be quietly wrong.
    if (agents.data && status?.run_id && agents.data.run_id === status.run_id) {
      for (const stage of agents.data.stages ?? []) map.set(stage.stage, stage)
    }
    return map
  }, [agents.data, status?.run_id])

  const isRunning = runState === "running"

  /**
   * A completed run that produced no events at all means the orchestrator
   * resumed an already-finished thread for today and returned the
   * checkpointed state without executing a node (see api/schemas/run.py's
   * RunStartRequest). Detecting it matters: otherwise "Start run" looks
   * broken - empty timeline, no error, status "completed".
   */
  const wasNoOp = runState === "completed" && events.length === 0

  const handleStart = useCallback(
    async (forceFresh = false) => {
      setStarting(true)
      try {
        const threadId = forceFresh ? `manual-${new Date().toISOString().replace(/[:.]/g, "-")}` : undefined
        const result = await api.run.start(threadId)
        if (result.already_running) {
          toast.info("A run is already in progress", { description: "Watching the existing run." })
        } else {
          toast.success("Run started", { description: "Fetching from the configured companies…" })
        }
        refreshStatus()
      } catch (err) {
        toast.error("Couldn't start the run", {
          description: err instanceof ApiError ? err.detail : String(err),
        })
      } finally {
        setStarting(false)
      }
    },
    [refreshStatus],
  )

  return (
    <div>
      <PageHeader
        title="Mission Control"
        description="Watch the nightly pipeline run, stage by stage."
        actions={
          <div className="flex items-center gap-2">
            <ConnectionPill connection={connection} isRunning={isRunning} />
            <button
              type="button"
              onClick={() => handleStart(false)}
              disabled={starting || isRunning}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {isRunning ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
              {isRunning ? "Running…" : "Start run"}
            </button>
          </div>
        }
      />

      {wasNoOp && (
        <div className="mb-5 flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 dark:border-amber-800 dark:bg-amber-950/40">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600" />
          <div className="min-w-0">
            <p className="text-sm font-medium">Nothing ran — today's pipeline had already completed</p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              The orchestrator keys each run to a date-based thread and resumes it. Today's thread already
              reached the end, so it returned the saved result without refetching anything. That's the same
              idempotency that stops a 2am cron doing the work twice if it fires twice.
            </p>
            <button
              type="button"
              onClick={() => handleStart(true)}
              disabled={starting}
              className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-amber-400 bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-50"
            >
              <RotateCw className="size-3" />
              Run again on a fresh thread
            </button>
          </div>
        </div>
      )}

      {runState === "failed" && status?.error && (
        <div className="mb-5 flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
          <div>
            <p className="text-sm font-medium text-destructive">The run failed</p>
            <p className="mt-0.5 text-xs text-muted-foreground">{status.error}</p>
          </div>
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-[1fr_260px]">
        <Card className="shadow-sm">
          <CardContent className="px-5 py-4">
            {events.length === 0 && runState === "idle" ? (
              <div className="py-10 text-center">
                <Radio className="mx-auto size-6 text-muted-foreground/40" />
                <p className="mt-3 text-sm font-medium">No run yet this session</p>
                <p className="mx-auto mt-1 max-w-sm text-xs text-muted-foreground">
                  Start a run to fetch fresh postings and score them. The timeline below updates live as each
                  stage reports in.
                </p>
              </div>
            ) : (
              <ol className="pt-1">
                {timeline.map((stage, index) => (
                  <StageRow
                    key={stage.definition.id}
                    stage={stage}
                    metric={metricByStage.get(stage.definition.id)}
                    isRecruiter={isRecruiter}
                    isLast={index === timeline.length - 1}
                  />
                ))}
              </ol>
            )}
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card className="shadow-sm">
            <CardContent className="px-4 py-3.5">
              <p className="text-[0.65rem] font-semibold uppercase tracking-wide text-muted-foreground">
                Run
              </p>
              <p className="mt-1 text-sm font-medium capitalize">{runState}</p>
              {status?.started_at && (
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Started {formatDate(status.started_at)}
                </p>
              )}
              <p className="mt-2 text-xs text-muted-foreground">
                {events.length} event{events.length === 1 ? "" : "s"} received
              </p>
            </CardContent>
          </Card>

          <Card className="shadow-sm">
            <CardContent className="px-4 py-3.5">
              <p className="text-[0.65rem] font-semibold uppercase tracking-wide text-muted-foreground">
                Quota used
              </p>
              {quota.length === 0 ? (
                <p className="mt-1.5 text-xs text-muted-foreground">
                  No LLM calls yet this run.
                </p>
              ) : (
                <ul className="mt-1.5 space-y-2">
                  {quota.map((entry) => (
                    <li key={entry.model}>
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="truncate text-xs" title={entry.model}>
                          {entry.model}
                        </span>
                        <span className="shrink-0 text-xs font-medium tabular-nums">
                          {entry.calls}
                          {entry.limit ? ` / ${entry.limit}` : ""}
                        </span>
                      </div>
                      {entry.limit && (
                        <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-muted">
                          <div
                            className="h-full rounded-full bg-primary"
                            style={{ width: `${Math.min(100, (entry.calls / entry.limit) * 100)}%` }}
                          />
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {(cacheStats.cached > 0 || cacheStats.fresh > 0) && (
                <p className="mt-2.5 border-t pt-2 text-xs text-muted-foreground">
                  {cacheStats.cached} cached · {cacheStats.fresh} fresh
                </p>
              )}
            </CardContent>
          </Card>

          {isRecruiter && (
            <Card className="shadow-sm">
              <CardContent className="px-4 py-3.5">
                <p className="text-[0.65rem] font-semibold uppercase tracking-wide text-muted-foreground">
                  Recruiter Mode
                </p>
                <p className="mt-1.5 text-xs text-muted-foreground">
                  Each stage above expands to its recorded metrics. Values the system doesn't measure show as
                  “not measured” rather than zero.
                </p>
                {agents.data?.notes?.map((note) => (
                  <p key={note} className="mt-2 text-[0.68rem] leading-relaxed text-muted-foreground">
                    {note}
                  </p>
                ))}
              </CardContent>
            </Card>
          )}
        </div>
      </div>

      <details className="mt-6">
        <summary className="cursor-pointer text-xs font-medium text-muted-foreground hover:text-foreground">
          Raw event log ({events.length})
        </summary>
        <div className="mt-2 max-h-80 overflow-y-auto rounded-lg border bg-muted/20 p-3">
          {events.length === 0 ? (
            <p className="text-xs text-muted-foreground">No events yet.</p>
          ) : (
            <ul className="space-y-1 font-mono text-[0.68rem]">
              {events.map((event) => (
                <li key={event.seq} className="flex gap-2">
                  <span className="shrink-0 text-muted-foreground">#{event.seq}</span>
                  <span className="shrink-0 text-primary">{event.stage}</span>
                  <span className="min-w-0 break-all">{event.message}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </details>
    </div>
  )
}

function ConnectionPill({ connection, isRunning }: { connection: string; isRunning: boolean }) {
  const map: Record<string, { label: string; className: string; icon: React.ReactNode }> = {
    open: {
      label: isRunning ? "Live" : "Connected",
      className: "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
      icon: <Radio className="size-3" />,
    },
    connecting: {
      label: "Connecting",
      className: "text-muted-foreground",
      icon: <Loader2 className="size-3 animate-spin" />,
    },
    reconnecting: {
      label: "Reconnecting",
      className: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200",
      icon: <RotateCw className="size-3 animate-spin" />,
    },
    closed: { label: "Stream closed", className: "text-muted-foreground", icon: <MinusCircle className="size-3" /> },
  }
  const entry = map[connection] ?? map.closed

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
        entry.className,
      )}
    >
      {entry.icon}
      {entry.label}
    </span>
  )
}
