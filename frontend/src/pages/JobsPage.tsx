import { useCallback, useMemo, useState } from "react"
import { toast } from "sonner"

import type { ApplicationStatus, JobSummary } from "@/api/client"
import { APPLICATION_STATUSES, ApiError, api } from "@/api/client"
import { JobCard, JobCardSkeleton } from "@/components/JobCard"
import { EmptyState, ErrorState, PageHeader } from "@/components/PageHeader"
import { Badge } from "@/components/ui/badge"
import { useApi } from "@/lib/useApi"
import { cn } from "@/lib/utils"

const DEFAULT_STATUSES: ApplicationStatus[] = ["new", "interviewing"]

export function JobsPage() {
  const [statuses, setStatuses] = useState<ApplicationStatus[]>(DEFAULT_STATUSES)
  const [pendingHash, setPendingHash] = useState<string>()
  // Local overlay of status changes, so the list updates instantly without
  // refetching every job (and without the card jumping out of the filtered
  // view while the user is still looking at it).
  const [overrides, setOverrides] = useState<Record<string, ApplicationStatus>>({})

  const { data, error, loading, reload } = useApi<JobSummary[]>(
    () => api.jobs.list(),
    [],
  )
  const stats = useApi(() => api.stats(), [])

  const toggleStatus = useCallback((status: ApplicationStatus) => {
    setStatuses((current) =>
      current.includes(status) ? current.filter((s) => s !== status) : [...current, status],
    )
  }, [])

  const handleStatusChange = useCallback(
    async (job: JobSummary, next: ApplicationStatus) => {
      const previous = overrides[job.content_hash] ?? job.application_status
      setPendingHash(job.content_hash)
      setOverrides((o) => ({ ...o, [job.content_hash]: next }))
      try {
        const result = await api.jobs.setStatus(job.content_hash, next)
        toast.success(`${job.company} — marked ${result.application_status}`, {
          description:
            result.applied_at && next === "applied"
              ? "Application date recorded. You applied; the system only tracks it."
              : undefined,
        })
      } catch (err) {
        // Roll the optimistic change back - showing a status the server
        // rejected would be worse than a brief flicker.
        setOverrides((o) => ({ ...o, [job.content_hash]: previous as ApplicationStatus }))
        toast.error("Couldn't update status", {
          description: err instanceof ApiError ? err.detail : String(err),
        })
      } finally {
        setPendingHash(undefined)
      }
    },
    [overrides],
  )

  const jobs = useMemo(() => {
    if (!data) return []
    return data.map((job) => ({
      ...job,
      application_status: overrides[job.content_hash] ?? job.application_status,
    }))
  }, [data, overrides])

  const visible = useMemo(
    () => (statuses.length === 0 ? jobs : jobs.filter((j) => statuses.includes(j.application_status as ApplicationStatus))),
    [jobs, statuses],
  )

  const scored = visible.filter((j) => !j.is_unscored)
  const unscored = visible.filter((j) => j.is_unscored)

  return (
    <div>
      <PageHeader
        title="Jobs"
        description={
          stats.data
            ? `${stats.data.survived} survived filters out of ${stats.data.total_fetched.toLocaleString()} fetched · ${stats.data.analyzed} analyzed${stats.data.pending ? ` · ${stats.data.pending} awaiting analysis` : ""}`
            : "Ranked by how well the Analyst thinks you fit."
        }
        actions={
          <button
            type="button"
            onClick={reload}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
          >
            Refresh
          </button>
        }
      />

      <div className="mb-5 flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-muted-foreground">Status</span>
        {APPLICATION_STATUSES.map((status) => {
          const active = statuses.includes(status)
          const count = jobs.filter((j) => j.application_status === status).length
          return (
            <button
              key={status}
              type="button"
              onClick={() => toggleStatus(status)}
              className={cn(
                "rounded-full border px-3 py-1 text-xs transition-colors",
                active
                  ? "border-primary bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-accent",
              )}
            >
              {status}
              <span className={cn("ml-1.5 tabular-nums", active ? "opacity-80" : "opacity-60")}>{count}</span>
            </button>
          )
        })}
        {statuses.length === 0 && (
          <span className="text-xs text-muted-foreground">(no filter — showing everything)</span>
        )}
      </div>

      {error && <ErrorState error={error} onRetry={reload} />}

      {loading && !error && (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <JobCardSkeleton key={i} />
          ))}
        </div>
      )}

      {!loading && !error && visible.length === 0 && (
        <EmptyState
          title={jobs.length === 0 ? "No analyzed jobs yet" : "Nothing matches this filter"}
          body={
            jobs.length === 0
              ? "Run the pipeline from Mission Control to fetch and score postings."
              : "Try selecting more statuses above — everything currently scored is filtered out."
          }
        />
      )}

      {!loading && !error && visible.length > 0 && (
        <div className="space-y-3">
          {scored.map((job) => (
            <JobCard
              key={job.content_hash}
              job={job}
              pending={pendingHash === job.content_hash}
              onStatusChange={(next) => handleStatusChange(job, next)}
            />
          ))}

          {unscored.length > 0 && (
            <div className="pt-4">
              <div className="mb-2 flex items-center gap-2">
                <h2 className="text-sm font-semibold">Could not evaluate</h2>
                <Badge variant="secondary">{unscored.length}</Badge>
              </div>
              <p className="mb-3 text-xs text-muted-foreground">
                No technical requirements were extracted from these postings, so there is no real fit score to
                show — not a low one.
              </p>
              <div className="space-y-3">
                {unscored.map((job) => (
                  <JobCard
                    key={job.content_hash}
                    job={job}
                    pending={pendingHash === job.content_hash}
                    onStatusChange={(next) => handleStatusChange(job, next)}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
