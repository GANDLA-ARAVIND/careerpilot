import { AlertTriangle, Info } from "lucide-react"

import type { EvaluationResponse } from "@/api/client"
import { api } from "@/api/client"
import { EmptyState, ErrorState, PageHeader } from "@/components/PageHeader"
import { Card, CardContent } from "@/components/ui/card"
import { formatDateTime } from "@/lib/format"
import { useApi } from "@/lib/useApi"
import { cn } from "@/lib/utils"

const RANKINGS = ["Embedding", "Stage-1"] as const
const RANDOM = "Random (expected)"

/**
 * How far above the random baseline a ranking actually is.
 *
 * This is the number the page exists to communicate honestly. At the time
 * of writing stage-1 MRR is 0.132 against a random expectation of 0.121 -
 * a margin of about 0.011, which is not a meaningful improvement on a
 * sample this size. The page says so in words rather than leaving a reader
 * to notice that two numbers on a table are nearly equal.
 */
function marginVerdict(value: number, baseline: number): { label: string; tone: "flat" | "slight" | "clear" } {
  const ratio = baseline > 0 ? value / baseline : 1
  if (ratio < 1.05) return { label: "at chance", tone: "flat" }
  if (ratio < 1.25) return { label: "marginal", tone: "slight" }
  return { label: "above chance", tone: "clear" }
}

function MetricRow({
  label,
  values,
}: {
  label: string
  values: Record<string, number> | undefined
}) {
  if (!values) return null
  const baseline = values[RANDOM] ?? 0
  return (
    <tr className="border-t">
      <td className="px-3 py-2 font-medium">{label}</td>
      {RANKINGS.map((name) => {
        const value = values[name]
        const verdict = marginVerdict(value ?? 0, baseline)
        return (
          <td key={name} className="px-3 py-2 text-right">
            <span className="tabular-nums">{value?.toFixed(3) ?? "—"}</span>
            <span
              className={cn(
                "ml-2 rounded-full px-1.5 py-0.5 text-[0.6rem] font-medium uppercase",
                verdict.tone === "flat" && "bg-muted text-muted-foreground",
                verdict.tone === "slight" && "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
                verdict.tone === "clear" && "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
              )}
            >
              {verdict.label}
            </span>
          </td>
        )
      })}
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">{baseline.toFixed(3)}</td>
    </tr>
  )
}

function Headline({ data }: { data: EvaluationResponse }) {
  const stage1Mrr = data.mrr?.["Stage-1"]
  const baseline = data.mrr?.[RANDOM]
  if (stage1Mrr == null || baseline == null) return null
  const verdict = marginVerdict(stage1Mrr, baseline)

  return (
    <div
      className={cn(
        "mb-5 rounded-lg border px-4 py-3.5",
        verdict.tone === "clear"
          ? "border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/40"
          : "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/40",
      )}
    >
      <div className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600" />
        <div>
          <p className="text-sm font-semibold">
            {verdict.tone === "flat"
              ? "The LLM ranking is currently barely above chance."
              : verdict.tone === "slight"
                ? "The LLM ranking is only marginally above chance."
                : "The LLM ranking beats the random baseline."}
          </p>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            Stage-1 MRR is <strong>{stage1Mrr.toFixed(3)}</strong> against a random expectation of{" "}
            <strong>{baseline.toFixed(3)}</strong> — a margin of {(stage1Mrr - baseline).toFixed(3)} on{" "}
            <strong>n = {data.n}</strong> ranked jobs. That is not a result worth claiming as evidence the
            ranking works. It is reported here as-is rather than dressed up, because the point of measuring was
            to find out, and this is what the measurement says.
          </p>
        </div>
      </div>
    </div>
  )
}

export function EvaluationPage() {
  const { data, error, loading, reload } = useApi(() => api.meta.evaluation(), [])

  return (
    <div>
      <PageHeader
        title="Evaluation"
        description="Ranking quality measured against a hand-labeled set."
      />

      {error && <ErrorState error={error} onRetry={reload} />}
      {loading && !error && <p className="text-sm text-muted-foreground">Loading…</p>}

      {data && !data.available && (
        <EmptyState
          title="No evaluation snapshot yet"
          body="Generate one by running evaluate_stage1.py — the API serves a precomputed snapshot rather than re-embedding every job per request."
        />
      )}

      {data?.available && (
        <>
          <Headline data={data} />

          <Card className="shadow-sm">
            <CardContent className="px-5 py-4">
              <h2 className="mb-3 text-sm font-semibold">Metrics</h2>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-left text-xs text-muted-foreground">
                    <tr>
                      <th className="px-3 py-1.5 font-medium">Metric</th>
                      {RANKINGS.map((r) => (
                        <th key={r} className="px-3 py-1.5 text-right font-medium">
                          {r}
                        </th>
                      ))}
                      <th className="px-3 py-1.5 text-right font-medium">{RANDOM}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <MetricRow label="MRR (good)" values={data.mrr} />
                    <MetricRow label="Recall@10" values={data.recall_at_10} />
                    <MetricRow label="Recall@20" values={data.recall_at_20} />
                  </tbody>
                </table>
              </div>
              <p className="mt-3 text-xs text-muted-foreground">
                Higher is better. “Random (expected)” is the analytically expected score for shuffling the same
                jobs — not a sampled run, so it has no noise of its own.
              </p>
            </CardContent>
          </Card>

          <div className="mt-4 grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
            <Card className="shadow-sm">
              <CardContent className="px-4 py-3.5">
                <h2 className="mb-2 text-sm font-semibold">Sample</h2>
                <dl className="space-y-1.5 text-xs">
                  <div className="flex justify-between gap-2 border-b border-dashed py-1">
                    <dt className="text-muted-foreground">Ranked jobs (n)</dt>
                    <dd className="font-medium tabular-nums">{data.n}</dd>
                  </div>
                  <div className="flex justify-between gap-2 border-b border-dashed py-1">
                    <dt className="text-muted-foreground">Labeled total</dt>
                    <dd className="font-medium tabular-nums">{data.total_labels}</dd>
                  </div>
                  {Object.entries(data.label_counts ?? {}).map(([label, count]) => (
                    <div key={label} className="flex justify-between gap-2 border-b border-dashed py-1">
                      <dt className="text-muted-foreground">labeled “{label}”</dt>
                      <dd className="font-medium tabular-nums">
                        {count}
                        <span className="ml-1 font-normal text-muted-foreground">
                          ({data.overlap_counts?.[label] ?? 0} in overlap)
                        </span>
                      </dd>
                    </div>
                  ))}
                </dl>

                {data.is_minority_of_label_set && (
                  <p className="mt-2.5 flex items-start gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-2 text-[0.68rem] leading-relaxed dark:border-amber-800 dark:bg-amber-950/40">
                    <Info className="mt-0.5 size-3.5 shrink-0 text-amber-600" />
                    Only {data.n} of {data.total_labels} labeled jobs have a stage-1 score, because stage 1 only
                    ever saw jobs that survived rule-filtering. These numbers describe how the rankers order the
                    jobs both methods actually saw — not the full labeled pool.
                  </p>
                )}

                {data.generated_at && (
                  <p className="mt-2 text-[0.65rem] text-muted-foreground">
                    Snapshot generated {formatDateTime(data.generated_at)}
                  </p>
                )}
              </CardContent>
            </Card>

            <Card className="shadow-sm">
              <CardContent className="px-4 py-3.5">
                <h2 className="mb-1 text-sm font-semibold">Where each “good” job landed</h2>
                <p className="mb-2 text-xs text-muted-foreground">
                  Position out of {data.n}. Lower is better — a perfect ranker puts every “good” job at the top.
                </p>
                <div className="max-h-96 overflow-auto rounded-md border">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-muted/60 text-left text-muted-foreground">
                      <tr>
                        <th className="px-2.5 py-1.5 font-medium">Job</th>
                        <th className="px-2.5 py-1.5 text-right font-medium">Embedding</th>
                        <th className="px-2.5 py-1.5 text-right font-medium">Stage-1</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(data.good_positions ?? []).map((row, i) => (
                        <tr key={i} className="border-t">
                          <td className="px-2.5 py-1 truncate" title={`${row.company} | ${row.title}`}>
                            <span className="font-medium">{row.company}</span>
                            <span className="text-muted-foreground"> · {row.title}</span>
                          </td>
                          <td className="px-2.5 py-1 text-right tabular-nums">{row.embedding_position}</td>
                          <td className="px-2.5 py-1 text-right tabular-nums">{row.stage1_position}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {data.top_stage1_fit_score != null && (
                  <p className="mt-2.5 text-xs text-muted-foreground">
                    Highest stage-1 score was <strong>{data.top_stage1_fit_score}</strong> —{" "}
                    {data.top_stage1_company} · {data.top_stage1_title}, which was hand-labeled{" "}
                    <strong>“{data.top_stage1_label}”</strong>.
                  </p>
                )}
              </CardContent>
            </Card>
          </div>

          {(data.caveats ?? []).length > 0 && (
            <Card className="mt-4 shadow-sm">
              <CardContent className="px-4 py-3.5">
                <h2 className="mb-2 text-sm font-semibold">Caveats</h2>
                <ul className="space-y-1.5">
                  {(data.caveats ?? []).map((c) => (
                    <li key={c} className="text-xs leading-relaxed text-muted-foreground">
                      — {c}
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  )
}
