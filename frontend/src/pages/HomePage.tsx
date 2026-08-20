import { Link } from "react-router-dom"
import { ArrowRight } from "lucide-react"

import { api } from "@/api/client"
import { ErrorState, PageHeader } from "@/components/PageHeader"
import { Card, CardContent } from "@/components/ui/card"
import { formatAge } from "@/lib/format"
import { useApi } from "@/lib/useApi"

function Stat({ value, label, sub }: { value: string; label: string; sub?: string }) {
  return (
    <Card className="shadow-sm">
      <CardContent className="px-5 py-4">
        <p className="text-2xl font-bold tabular-nums">{value}</p>
        <p className="mt-0.5 text-[0.68rem] font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </p>
        {sub && <p className="mt-0.5 text-xs text-primary">{sub}</p>}
      </CardContent>
    </Card>
  )
}

export function HomePage() {
  const { data, error, loading, reload } = useApi(() => api.stats(), [])

  return (
    <div>
      <PageHeader
        title="Home"
        description={
          data?.last_run
            ? `Data as of ${formatAge(data.last_run)} ago. The pipeline finds and ranks; you decide and apply.`
            : "The pipeline finds and ranks; you decide and apply."
        }
      />

      {error && <ErrorState error={error} onRetry={reload} />}

      {!error && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat value={loading ? "—" : (data?.total_fetched.toLocaleString() ?? "0")} label="Fetched" />
            <Stat
              value={loading ? "—" : String(data?.survived ?? 0)}
              label="Survived filters"
              sub={
                data && data.total_fetched > 0
                  ? `${((data.survived / data.total_fetched) * 100).toFixed(1)}% of fetched`
                  : undefined
              }
            />
            <Stat
              value={loading ? "—" : String(data?.analyzed ?? 0)}
              label="Analyzed"
              sub={
                data && data.survived > 0
                  ? `${((data.analyzed / data.survived) * 100).toFixed(0)}% of survivors`
                  : undefined
              }
            />
            <Stat value={loading ? "—" : String(data?.pending ?? 0)} label="Awaiting analysis" />
          </div>

          <div className="mt-6 flex flex-wrap gap-3">
            <Link
              to="/jobs"
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
            >
              See ranked jobs
              <ArrowRight className="size-4" />
            </Link>
            <Link
              to="/mission-control"
              className="inline-flex items-center gap-1.5 rounded-md border px-4 py-2 text-sm font-medium hover:bg-accent"
            >
              Mission Control
            </Link>
          </div>

          {data && Object.keys(data.rejected_by).length > 0 && (
            <div className="mt-8">
              <h2 className="mb-2 text-sm font-semibold">Why postings were filtered out</h2>
              <div className="flex flex-wrap gap-2">
                {Object.entries(data.rejected_by)
                  .sort(([, a], [, b]) => b - a)
                  .map(([rule, count]) => (
                    <span key={rule} className="rounded-full border px-3 py-1 text-xs text-muted-foreground">
                      {rule} <span className="ml-1 font-medium tabular-nums text-foreground">{count.toLocaleString()}</span>
                    </span>
                  ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
