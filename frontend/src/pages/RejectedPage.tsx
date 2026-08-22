import { useCallback, useEffect, useState } from "react"

import type { RejectedPage as RejectedPageData } from "@/api/client"
import { ApiError, api } from "@/api/client"
import { EmptyState, ErrorState, PageHeader } from "@/components/PageHeader"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { cn } from "@/lib/utils"

const PAGE_SIZE = 50

/**
 * Why a posting did not make the list.
 *
 * The counterpart to the Jobs page: 12k+ postings are rejected by rule
 * filters every run, and until now the only evidence of that was a total on
 * the Home page. Tuning a keyword list without being able to read what it
 * actually rejected is guesswork - every filter change this project has made
 * needed a before/after sample, and that sample had to be produced by hand
 * from a script each time.
 *
 * Paginated deliberately: the honest thing is a readable sample with the
 * real total stated, not an attempt to render twelve thousand rows.
 */
export function RejectedPage() {
  const [rule, setRule] = useState<string>()
  const [page, setPage] = useState(1)
  const [data, setData] = useState<RejectedPageData>()
  const [error, setError] = useState<string>()
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError(undefined)
    try {
      setData(await api.jobs.rejected({ rule, page, pageSize: PAGE_SIZE }))
    } catch (err) {
      // Same shaping useApi does: ApiError carries the backend's own detail
      // string, anything else falls back to its message.
      setError(err instanceof ApiError ? err.detail : err instanceof Error ? err.message : "Unknown error")
    } finally {
      setLoading(false)
    }
  }, [rule, page])

  useEffect(() => {
    void load()
  }, [load])

  // `rules` comes from the full unfiltered set, so the chips stay stable
  // when a rule is selected rather than collapsing to just the active one.
  const rules = data?.rules ?? []
  const total = data?.total ?? 0
  const lastPage = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const from = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1
  const to = Math.min(page * PAGE_SIZE, total)

  const pick = (next?: string) => {
    setRule(next)
    setPage(1)
  }

  return (
    <div>
      <PageHeader
        title="Rejected postings"
        description="Why postings didn't make the list. One rule fires per posting - the first one that matches."
        actions={
          <button type="button" onClick={() => void load()} className="rounded-md border px-3 py-1.5 text-sm hover:bg-accent">
            Refresh
          </button>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-muted-foreground">Rule</span>
        <button
          type="button"
          onClick={() => pick(undefined)}
          className={cn(
            "rounded-full border px-3 py-1 text-xs transition-colors",
            rule === undefined
              ? "border-primary bg-primary text-primary-foreground"
              : "text-muted-foreground hover:bg-accent",
          )}
        >
          all
        </button>
        {rules.map((r) => (
          <button
            key={r}
            type="button"
            onClick={() => pick(r)}
            className={cn(
              "rounded-full border px-3 py-1 text-xs transition-colors",
              rule === r
                ? "border-primary bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-accent",
            )}
          >
            {r}
          </button>
        ))}
      </div>

      {error && <ErrorState error={error} onRetry={load} />}
      {loading && !error && <p className="text-sm text-muted-foreground">Loading…</p>}

      {!loading && !error && total === 0 && (
        <EmptyState
          title={rule ? `Nothing rejected by ${rule}` : "Nothing rejected yet"}
          body={
            rule
              ? "No posting in the archive was rejected by this rule. Try another, or select all."
              : "Run the pipeline from Mission Control — rejected postings are stored too, not discarded."
          }
        />
      )}

      {!loading && !error && total > 0 && (
        <>
          <p className="mb-3 text-xs text-muted-foreground">
            Showing <span className="font-medium tabular-nums text-foreground">{from}–{to}</span> of{" "}
            <span className="font-medium tabular-nums text-foreground">{total.toLocaleString()}</span>
            {rule ? <> rejected by <code className="rounded bg-muted px-1 py-0.5">{rule}</code></> : " rejected"}.
            Rejected postings are kept, not deleted — they are the RAG archive corpus.
          </p>

          <Card className="shadow-sm">
            <CardContent className="px-0 py-0">
              <table className="w-full text-sm">
                <thead className="border-b text-left text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 font-medium">Company</th>
                    <th className="px-4 py-2 font-medium">Title</th>
                    <th className="px-4 py-2 font-medium">Location</th>
                    <th className="px-4 py-2 font-medium">Rejected by</th>
                  </tr>
                </thead>
                <tbody>
                  {(data?.items ?? []).map((item, i) => (
                    <tr key={`${item.company}-${item.title}-${i}`} className="border-b last:border-0">
                      <td className="px-4 py-1.5 font-medium">{item.company}</td>
                      <td className="px-4 py-1.5">{item.title}</td>
                      <td className="px-4 py-1.5 text-muted-foreground">{item.location ?? "—"}</td>
                      <td className="px-4 py-1.5">
                        <Badge variant="secondary" className="text-[0.65rem] font-normal">
                          {item.reason}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>

          <div className="mt-3 flex items-center justify-between">
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              className="rounded-md border px-3 py-1.5 text-sm disabled:opacity-40 hover:enabled:bg-accent"
            >
              Previous
            </button>
            <span className="text-xs tabular-nums text-muted-foreground">
              page {page} of {lastPage.toLocaleString()}
            </span>
            <button
              type="button"
              disabled={page >= lastPage}
              onClick={() => setPage((p) => Math.min(lastPage, p + 1))}
              className="rounded-md border px-3 py-1.5 text-sm disabled:opacity-40 hover:enabled:bg-accent"
            >
              Next
            </button>
          </div>
        </>
      )}
    </div>
  )
}
