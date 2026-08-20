import type { JobSummary } from "@/api/client"

/** "2h", "3d" - compact relative age, matching the dashboard's own wording. */
export function formatAge(iso: string | null | undefined): string {
  if (!iso) return "-"
  // Backend datetimes are naive UTC (see db.py's _to_naive_utc). Without
  // the explicit Z, browsers parse them as LOCAL time, which shows "in 5
  // hours" for something that just happened. See parseUtc.
  const parsed = parseUtc(iso)
  const hours = (Date.now() - parsed.getTime()) / 36e5
  if (!Number.isFinite(hours)) return "-"
  if (hours < 1) return `${Math.max(0, Math.round(hours * 60))}m`
  if (hours < 48) return `${hours.toFixed(1)}h`
  return `${(hours / 24).toFixed(1)}d`
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "-"
  const parsed = parseUtc(iso)
  return Number.isNaN(parsed.getTime())
    ? "-"
    : parsed.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
}

/**
 * Backend datetimes are naive UTC (db.py normalizes every stored datetime
 * with _to_naive_utc), so they arrive WITHOUT a timezone suffix. A bare
 * `new Date("2026-08-06T15:05:59")` is interpreted as LOCAL time by every
 * browser, silently shifting each timestamp by the viewer's UTC offset -
 * which showed up as run times displaying 5.5 hours early. Always parse
 * through here rather than calling `new Date` on an API datetime.
 */
export function parseUtc(iso: string): Date {
  return new Date(/[Zz]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`)
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "-"
  const parsed = parseUtc(iso)
  return Number.isNaN(parsed.getTime()) ? "-" : parsed.toLocaleString()
}

/**
 * How to describe a job's experience requirement.
 *
 * null means the parser found nothing to anchor on - NOT zero years. The
 * backend is careful about that distinction (see filters.py) and throwing
 * it away in the UI would undo the point.
 */
export function formatExperience(job: JobSummary): string {
  if (job.years_required == null) return "experience not stated"
  const years = Number.isInteger(job.years_required) ? job.years_required : job.years_required.toFixed(1)
  return `${years} yrs required · resume meets it: ${job.resume_meets_experience ? "yes" : "no"}`
}

export type Verdict = "strong" | "possible" | "weak" | "unscored" | "unanalyzed"

const SCORED_VERDICTS: readonly string[] = ["strong", "possible", "weak"]

export function verdictOf(job: JobSummary): Verdict {
  if (job.is_unscored) return job.verdict === "unanalyzed" ? "unanalyzed" : "unscored"
  // Anything the backend sends that isn't one of the three known scored
  // verdicts falls back to "unscored" rather than being rendered raw -
  // an unknown verdict should look uncertain, not authoritative.
  return SCORED_VERDICTS.includes(job.verdict) ? (job.verdict as Verdict) : "unscored"
}
