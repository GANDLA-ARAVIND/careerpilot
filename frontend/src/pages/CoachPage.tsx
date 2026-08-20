import { useCallback, useState } from "react"
import { Loader2, Search } from "lucide-react"
import { toast } from "sonner"

import type { AskResponse } from "@/api/client"
import { ApiError, api } from "@/api/client"
import { EmptyState, ErrorState, PageHeader } from "@/components/PageHeader"
import { Card, CardContent } from "@/components/ui/card"
import { useApi } from "@/lib/useApi"

const THRESHOLDS = [30, 40, 50, 60]

/**
 * Suggested questions, not a chat history.
 *
 * Coach answers one retrieval-grounded question at a time over the JD
 * archive - it isn't a conversational assistant, and presenting it as a
 * chat would imply memory and follow-up it doesn't have.
 */
const EXAMPLE_QUESTIONS = [
  "Which skills appear most often in roles I scored below 60 on?",
  "What do backend roles in India ask for that my resume doesn't mention?",
  "Which cloud platforms show up most across these postings?",
]

export function CoachPage() {
  const [threshold, setThreshold] = useState(40)
  const gaps = useApi(() => api.coach.skillGaps(threshold), [threshold])

  const [question, setQuestion] = useState("")
  const [answer, setAnswer] = useState<AskResponse>()
  const [asking, setAsking] = useState(false)

  const ask = useCallback(async (q: string) => {
    if (!q.trim()) return
    setAsking(true)
    setAnswer(undefined)
    try {
      setAnswer(await api.coach.ask(q))
    } catch (err) {
      toast.error("Couldn't answer that", {
        description: err instanceof ApiError ? err.detail : String(err),
      })
    } finally {
      setAsking(false)
    }
  }, [])

  const maxCount = gaps.data?.skills?.[0]?.count ?? 1

  return (
    <div>
      <PageHeader
        title="Career Coach"
        description="Patterns across every posting the pipeline has analyzed."
      />

      <div className="grid gap-5 lg:grid-cols-2">
        <Card className="shadow-sm">
          <CardContent className="px-5 py-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-semibold">Most common missing skills</h2>
              <div className="flex items-center gap-1">
                <span className="text-xs text-muted-foreground">below</span>
                {THRESHOLDS.map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setThreshold(t)}
                    className={`rounded-md border px-2 py-0.5 text-xs ${
                      t === threshold ? "border-primary bg-primary text-primary-foreground" : "hover:bg-accent"
                    }`}
                  >
                    {t}
                  </button>
                ))}
              </div>
            </div>

            {gaps.error && <ErrorState error={gaps.error} onRetry={gaps.reload} />}
            {gaps.loading && !gaps.error && <p className="text-sm text-muted-foreground">Loading…</p>}

            {gaps.data && (
              <>
                <p className="mb-3 text-xs text-muted-foreground">
                  Across {gaps.data.job_count} job(s) scored below {gaps.data.threshold} on the stage-1 pass.
                  Stage-1 only, deliberately — mixing in stage-2 scores would aggregate rows judged by two
                  different models.
                </p>
                {(gaps.data.skills ?? []).length === 0 ? (
                  <EmptyState
                    title="Nothing below this threshold"
                    body="Try raising it, or confirm the screening pass has actually run."
                  />
                ) : (
                  <ul className="space-y-1.5">
                    {(gaps.data.skills ?? []).map((gap) => (
                      <li key={gap.skill} className="flex items-center gap-2">
                        <span className="w-8 shrink-0 text-right text-xs font-semibold tabular-nums">
                          {gap.count}
                        </span>
                        <span className="h-4 shrink-0 rounded-sm bg-primary/25" style={{ width: `${(gap.count / maxCount) * 90}px` }} />
                        <span className="min-w-0 truncate text-sm" title={gap.skill}>
                          {gap.skill}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </CardContent>
        </Card>

        <Card className="shadow-sm">
          <CardContent className="px-5 py-4">
            <h2 className="text-sm font-semibold">Ask about the market</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              One question at a time, answered only from postings retrieved out of the archive. Not a chat —
              there's no memory between questions.
            </p>

            <div className="mt-3 flex gap-2">
              <input
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void ask(question)}
                placeholder="e.g. which skills do I keep missing?"
                className="min-w-0 flex-1 rounded-md border bg-background px-3 py-2 text-sm"
              />
              <button
                type="button"
                onClick={() => void ask(question)}
                disabled={asking || !question.trim()}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
              >
                {asking ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />}
                Ask
              </button>
            </div>

            <div className="mt-2 flex flex-wrap gap-1.5">
              {EXAMPLE_QUESTIONS.map((q) => (
                <button
                  key={q}
                  type="button"
                  onClick={() => {
                    setQuestion(q)
                    void ask(q)
                  }}
                  className="rounded-full border px-2.5 py-1 text-[0.7rem] text-muted-foreground hover:bg-accent"
                >
                  {q}
                </button>
              ))}
            </div>

            {answer && (
              <div className="mt-4 border-t pt-3">
                <p className="text-sm leading-relaxed">{answer.answer}</p>

                {/* Retrieval provenance. The honest bit: if retrieval was a
                    no-op the "answer" was formed over the whole pool, not a
                    focused subset, and that changes how much to trust it. */}
                <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[0.68rem] text-muted-foreground">
                  <span>
                    retrieved {answer.retrieved_count} of {answer.pool_size} in scope
                  </span>
                  <span>population {answer.population_size}</span>
                  <span>k = {answer.k}</span>
                </div>
                {answer.retrieval_was_noop && (
                  <p className="mt-2 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-[0.68rem] dark:border-amber-800 dark:bg-amber-950/40">
                    Retrieval returned everything in scope rather than narrowing — the pool was smaller than k,
                    so this answer isn't based on a selective search.
                  </p>
                )}

                {(answer.retrieved ?? []).length > 0 && (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-xs font-medium text-primary hover:underline">
                      Show the {(answer.retrieved ?? []).length} retrieved excerpt(s)
                    </summary>
                    <ul className="mt-2 space-y-2">
                      {(answer.retrieved ?? []).map((chunk, i) => (
                        <li key={i} className="rounded-md border bg-muted/30 p-2.5">
                          <p className="mb-1 text-[0.65rem] tabular-nums text-muted-foreground">
                            score {chunk.score.toFixed(3)}
                          </p>
                          <p className="line-clamp-4 text-xs">{chunk.text}</p>
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
