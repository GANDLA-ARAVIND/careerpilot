import { useMemo } from "react"
import { ExternalLink } from "lucide-react"

import type { AppliedJobsResponse } from "@/api/client"
import { api } from "@/api/client"
import { EmptyState, ErrorState, PageHeader } from "@/components/PageHeader"
import { Badge } from "@/components/ui/badge"
import { formatDate } from "@/lib/format"
import { useApi } from "@/lib/useApi"
import { cn } from "@/lib/utils"

const STATUS_STYLES: Record<string, string> = {
  applied: "bg-blue-50 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
  interviewing: "bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  rejected: "bg-rose-50 text-rose-900 dark:bg-rose-950 dark:text-rose-200",
  new: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200",
}

export function ApplicationsPage() {
  // /api/applications, NOT jobs.list(). The old version derived this page
  // from the survivor list, so a job applied to that later failed a rule
  // filter vanished from the application record - and filters do change.
  // The backend now queries applied_at directly, independent of filter
  // state; see api/routers/jobs.py's list_applications.
  const { data, error, loading, reload } = useApi<AppliedJobsResponse>(() => api.applications(), [])

  const applied = useMemo(() => (data?.items ?? []).filter((j) => j.applied_at != null), [data])
  // Marked applied before applied_at existed as a column, so the date was
  // never recorded. Surfaced separately rather than silently omitted or
  // given a made-up date.
  const undated = useMemo(() => (data?.items ?? []).filter((j) => j.applied_at == null), [data])

  return (
    <div>
      <PageHeader
        title="Applications"
        description="Everything you've applied to, most recent first. Kept independently of the job filters."
        actions={
          <button type="button" onClick={reload} className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent">
            Refresh
          </button>
        }
      />

      {error && <ErrorState error={error} onRetry={reload} />}
      {loading && !error && <p className="text-sm text-muted-foreground">Loading…</p>}

      {!loading && !error && applied.length === 0 && undated.length === 0 && (
        <EmptyState
          title="Nothing applied to yet"
          body="Mark a job “applied” on the Jobs page and it appears here with the date it was recorded."
        />
      )}

      {applied.length > 0 && (
        <ol className="relative space-y-0 border-l pl-6">
          {applied.map((job) => (
            <li key={job.content_hash} className="relative pb-6">
              <span className="absolute -left-[1.655rem] top-1.5 size-3 rounded-full bg-primary ring-4 ring-background" />
              <p className="text-xs font-medium tabular-nums text-muted-foreground">
                {formatDate(job.applied_at)}
              </p>
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <h3 className="text-sm font-semibold">{job.title}</h3>
                <span
                  className={cn(
                    "rounded-full px-2 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide",
                    STATUS_STYLES[job.application_status] ?? STATUS_STYLES.new,
                  )}
                >
                  {job.application_status}
                </span>
                {!job.still_a_survivor && (
                  <Badge variant="outline" className="text-[0.65rem] font-normal text-muted-foreground">
                    no longer passes current filters
                  </Badge>
                )}
              </div>
              <p className="text-sm text-muted-foreground">
                {job.company}
                {job.location ? ` · ${job.location}` : ""}
              </p>
              <a
                href={job.url}
                target="_blank"
                rel="noreferrer noopener"
                className="mt-1 inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
              >
                Open posting
                <ExternalLink className="size-3" />
              </a>
            </li>
          ))}
        </ol>
      )}

      {undated.length > 0 && (
        <div className="mt-6 rounded-lg border border-dashed px-4 py-3">
          <h2 className="text-sm font-medium">Applied, date not recorded ({undated.length})</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Marked applied before the application date started being recorded, so there's no real date to show.
            Listed here rather than given an invented one.
          </p>
          <ul className="mt-2 space-y-1">
            {undated.map((job) => (
              <li key={job.content_hash} className="text-sm">
                <span className="font-medium">{job.company}</span>{" "}
                <span className="text-muted-foreground">· {job.title}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
