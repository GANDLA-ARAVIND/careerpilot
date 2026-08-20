import { useState } from "react"
import { ChevronDown, ExternalLink } from "lucide-react"

import type { ApplicationStatus, JobSummary } from "@/api/client"
import { APPLICATION_STATUSES } from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { formatAge, formatExperience, verdictOf } from "@/lib/format"
import { cn } from "@/lib/utils"

/**
 * Verdict styling. Restrained tints, not traffic lights - and the score is
 * a visual element (the badge) rather than a number inside a heading.
 */
const VERDICT_STYLES = {
  strong: {
    border: "border-l-emerald-500",
    badge: "bg-emerald-50 text-emerald-800 border-emerald-300 dark:bg-emerald-950 dark:text-emerald-200 dark:border-emerald-800",
    tag: "bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  },
  possible: {
    border: "border-l-amber-500",
    badge: "bg-amber-50 text-amber-900 border-amber-300 dark:bg-amber-950 dark:text-amber-200 dark:border-amber-800",
    tag: "bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  },
  weak: {
    border: "border-l-rose-300",
    badge: "bg-rose-50 text-rose-900 border-rose-300 dark:bg-rose-950 dark:text-rose-200 dark:border-rose-900",
    tag: "bg-rose-50 text-rose-900 dark:bg-rose-950 dark:text-rose-200",
  },
  unscored: {
    border: "border-l-slate-400",
    badge: "bg-slate-100 text-slate-700 border-slate-300 dark:bg-slate-800 dark:text-slate-200 dark:border-slate-700",
    tag: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200",
  },
  unanalyzed: {
    border: "border-l-slate-300",
    badge: "bg-slate-100 text-slate-600 border-slate-300 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700",
    tag: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
  },
} as const

const VISIBLE_MISSING_SKILLS = 6

function Chips({ skills = [], tone }: { skills?: string[]; tone: "matched" | "missing" }) {
  const [expanded, setExpanded] = useState(false)
  if (skills.length === 0) {
    return (
      <p className="text-xs italic text-muted-foreground">
        {tone === "matched" ? "No matched skills" : "Nothing stated as missing"}
      </p>
    )
  }

  const shown = expanded ? skills : skills.slice(0, VISIBLE_MISSING_SKILLS)
  const hidden = skills.length - shown.length

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {shown.map((skill) => (
        <span
          key={skill}
          className={cn(
            "rounded-full border px-2 py-0.5 text-xs",
            tone === "matched"
              ? "border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200"
              : "border-rose-200 bg-rose-50 text-rose-900 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-200",
          )}
        >
          {skill}
        </span>
      ))}
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="text-xs font-medium text-primary hover:underline"
        >
          +{hidden} more
        </button>
      )}
      {expanded && skills.length > VISIBLE_MISSING_SKILLS && (
        <button
          type="button"
          onClick={() => setExpanded(false)}
          className="text-xs text-muted-foreground hover:underline"
        >
          show less
        </button>
      )}
    </div>
  )
}

export function JobCard({
  job,
  onStatusChange,
  pending,
}: {
  job: JobSummary
  onStatusChange: (status: ApplicationStatus) => void
  pending?: boolean
}) {
  const verdict = verdictOf(job)
  const styles = VERDICT_STYLES[verdict]
  const unevaluated = verdict === "unscored" || verdict === "unanalyzed"

  return (
    <article
      className={cn(
        "rounded-lg border border-l-4 bg-card p-4 shadow-sm transition-opacity",
        styles.border,
        pending && "opacity-60",
      )}
    >
      <div className="flex items-start gap-3">
        {/*
          fit_score is null for a job the Analyst couldn't compare - a "?"
          rather than a 0, so it never reads as a measured low score.
        */}
        <div
          className={cn(
            "flex size-12 shrink-0 items-center justify-center rounded-full border-2 text-lg font-bold",
            styles.badge,
          )}
        >
          {job.fit_score ?? "?"}
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-start gap-2">
            <h3 className="min-w-0 text-[0.95rem] font-semibold leading-snug">
              {job.title}
              {job.is_new && (
                <Badge variant="secondary" className="ml-2 align-middle text-[0.6rem] font-bold">
                  NEW
                </Badge>
              )}
            </h3>
            <span
              className={cn(
                "ml-auto shrink-0 rounded-full px-2 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide",
                styles.tag,
              )}
            >
              {unevaluated ? "no comparison" : job.verdict}
            </span>
          </div>
          <p className="text-sm text-muted-foreground">{job.company}</p>

          <p className="mt-1.5 text-xs text-muted-foreground">
            {job.location ?? "-"} · {formatExperience(job)} · seen {formatAge(job.first_seen)} ago
            {!unevaluated && ` · scored by ${job.model}`}
          </p>
        </div>
      </div>

      {job.reasoning && <p className="mt-3 text-sm leading-relaxed">{job.reasoning}</p>}

      {unevaluated ? (
        <p className="mt-3 text-xs italic text-muted-foreground">
          No technical requirements were extracted from this posting — there was nothing concrete to compare
          against your resume.
        </p>
      ) : (
        <div className="mt-3 space-y-2.5">
          <div>
            <p className="mb-1 text-[0.65rem] font-semibold uppercase tracking-wide text-muted-foreground">
              Matched
            </p>
            <Chips skills={job.matched_skills} tone="matched" />
          </div>
          <div>
            <p className="mb-1 text-[0.65rem] font-semibold uppercase tracking-wide text-muted-foreground">
              Missing
            </p>
            <Chips skills={job.missing_skills} tone="missing" />
          </div>
        </div>
      )}

      <div className="mt-4 flex items-center justify-between gap-3 border-t pt-3">
        <Select
          value={job.application_status}
          onValueChange={(value) => onStatusChange(value as ApplicationStatus)}
          disabled={pending}
        >
          <SelectTrigger className="h-8 w-[150px] text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {APPLICATION_STATUSES.map((status) => (
              <SelectItem key={status} value={status} className="text-xs">
                {status}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {/*
          A plain link out to the employer's own posting. Applying is the
          human's action, always - nothing here submits anything.
        */}
        <a
          href={job.url}
          target="_blank"
          rel="noreferrer noopener"
          className="inline-flex items-center gap-1 text-sm font-medium text-primary hover:underline"
        >
          Open posting
          <ExternalLink className="size-3.5" />
        </a>
      </div>
    </article>
  )
}

export function JobCardSkeleton() {
  return (
    <div className="rounded-lg border border-l-4 border-l-muted bg-card p-4">
      <div className="flex items-start gap-3">
        <div className="size-12 shrink-0 animate-pulse rounded-full bg-muted" />
        <div className="flex-1 space-y-2">
          <div className="h-4 w-2/3 animate-pulse rounded bg-muted" />
          <div className="h-3 w-1/3 animate-pulse rounded bg-muted" />
          <div className="h-3 w-1/2 animate-pulse rounded bg-muted" />
        </div>
      </div>
      <div className="mt-4 space-y-2">
        <div className="h-3 w-full animate-pulse rounded bg-muted" />
        <div className="h-3 w-4/5 animate-pulse rounded bg-muted" />
      </div>
    </div>
  )
}

export { ChevronDown }
