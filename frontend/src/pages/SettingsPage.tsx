import { useCallback, useEffect, useState } from "react"
import { AlertTriangle, Loader2, Plus, RotateCcw, Search, X } from "lucide-react"
import { toast } from "sonner"

import type { PreferencesBody, PreferencesImpact, ScoutResponse } from "@/api/client"
import { ApiError, api } from "@/api/client"
import { ErrorState, PageHeader } from "@/components/PageHeader"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { useApi } from "@/lib/useApi"
import { cn } from "@/lib/utils"

const LIST_FIELDS: Array<{ key: keyof PreferencesBody; label: string; help: string }> = [
  {
    key: "title_allowlist",
    label: "Title allowlist",
    help: "A title must contain at least one of these to survive.",
  },
  {
    key: "seniority_keywords",
    label: "Seniority keywords (reject)",
    help: "A title containing any of these is rejected as too senior.",
  },
  {
    key: "non_engineering_keywords",
    label: "Non-engineering keywords (reject)",
    help: "A title containing any of these is rejected as not an engineering role.",
  },
  {
    key: "india_location_keywords",
    label: "India location keywords",
    help: "A location must contain at least one of these to survive.",
  },
]

function KeywordEditor({
  label,
  help,
  values,
  onChange,
}: {
  label: string
  help: string
  values: string[]
  onChange: (next: string[]) => void
}) {
  const [draft, setDraft] = useState("")

  const add = () => {
    const value = draft.trim()
    if (!value) return
    // Case-insensitive dedupe: the backend lowercases before matching, so
    // "QA" and "qa" would be the same rule with two entries.
    if (values.some((v) => v.toLowerCase() === value.toLowerCase())) {
      setDraft("")
      return
    }
    onChange([...values, value])
    setDraft("")
  }

  return (
    <div className="rounded-lg border p-3.5">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">{label}</h3>
        <span className="text-xs text-muted-foreground">{values.length}</span>
      </div>
      <p className="mt-0.5 text-xs text-muted-foreground">{help}</p>

      {values.length === 0 && (
        <p className="mt-2 flex items-start gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs dark:border-amber-800 dark:bg-amber-950/40">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-amber-600" />
          Empty — the backend falls back to its built-in default rather than matching nothing.
        </p>
      )}

      <div className="mt-2 flex flex-wrap gap-1.5">
        {values.map((value) => (
          <span
            key={value}
            className="inline-flex items-center gap-1 rounded-full border bg-muted/40 py-0.5 pl-2.5 pr-1 text-xs"
          >
            {value}
            <button
              type="button"
              onClick={() => onChange(values.filter((v) => v !== value))}
              className="rounded-full p-0.5 hover:bg-destructive/15 hover:text-destructive"
              aria-label={`Remove ${value}`}
            >
              <X className="size-3" />
            </button>
          </span>
        ))}
      </div>

      <div className="mt-2 flex gap-1.5">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault()
              add()
            }
          }}
          placeholder="Add keyword…"
          className="min-w-0 flex-1 rounded-md border bg-background px-2.5 py-1.5 text-xs"
        />
        <button type="button" onClick={add} className="rounded-md border px-2 hover:bg-accent">
          <Plus className="size-3.5" />
        </button>
      </div>
    </div>
  )
}

function RolesSection() {
  const { data, error, loading, reload } = useApi(() => api.preferences.get(), [])
  const [draft, setDraft] = useState<PreferencesBody>()
  const [impact, setImpact] = useState<PreferencesImpact>()
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (data) {
      setDraft({
        title_allowlist: data.title_allowlist,
        seniority_keywords: data.seniority_keywords,
        non_engineering_keywords: data.non_engineering_keywords,
        india_location_keywords: data.india_location_keywords,
      })
      setImpact(undefined)
    }
  }, [data])

  const dirty =
    !!draft &&
    !!data &&
    LIST_FIELDS.some(({ key }) => JSON.stringify(draft[key]) !== JSON.stringify(data[key]))

  const preview = useCallback(async () => {
    if (!draft) return
    setBusy(true)
    try {
      setImpact(await api.preferences.preview(draft))
    } catch (err) {
      toast.error("Preview failed", { description: err instanceof ApiError ? err.detail : String(err) })
    } finally {
      setBusy(false)
    }
  }, [draft])

  const save = useCallback(async () => {
    if (!draft) return
    setBusy(true)
    try {
      const result = await api.preferences.save(draft)
      toast.success("Preferences saved", {
        description: `${result.new_survivors} job(s) now survive the filters (${result.delta >= 0 ? "+" : ""}${result.delta}). ${result.unanalyzed_after} await analysis.`,
      })
      reload()
    } catch (err) {
      toast.error("Save failed", { description: err instanceof ApiError ? err.detail : String(err) })
    } finally {
      setBusy(false)
    }
  }, [draft, reload])

  const reset = useCallback(async () => {
    setBusy(true)
    try {
      await api.preferences.reset()
      toast.success("Reset to built-in defaults")
      reload()
    } catch (err) {
      toast.error("Reset failed", { description: err instanceof ApiError ? err.detail : String(err) })
    } finally {
      setBusy(false)
    }
  }, [reload])

  if (error) return <ErrorState error={error} onRetry={reload} />
  if (loading || !draft) return <p className="text-sm text-muted-foreground">Loading…</p>

  return (
    <div>
      {data?.warnings?.map((w) => (
        <p
          key={w}
          className="mb-3 flex items-start gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs dark:border-amber-800 dark:bg-amber-950/40"
        >
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-amber-600" />
          {w}
        </p>
      ))}
      {data?.is_default && (
        <p className="mb-3 text-xs text-muted-foreground">Currently using the built-in defaults.</p>
      )}

      <div className="grid gap-3 md:grid-cols-2">
        {LIST_FIELDS.map(({ key, label, help }) => (
          <KeywordEditor
            key={key}
            label={label}
            help={help}
            values={draft[key]}
            onChange={(next) => {
              setDraft({ ...draft, [key]: next })
              setImpact(undefined)
            }}
          />
        ))}
      </div>

      {impact && (
        <div className="mt-3 rounded-md border border-primary/40 bg-primary/5 px-3 py-2.5 text-sm">
          <strong>{impact.current_survivors}</strong> jobs survive today →{" "}
          <strong>{impact.new_survivors}</strong> with these edits (
          {impact.delta >= 0 ? "+" : ""}
          {impact.delta}). {impact.unanalyzed_after} would need analysing.
        </div>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={preview}
          disabled={busy || !dirty}
          className="inline-flex items-center gap-1.5 rounded-md border px-3.5 py-2 text-sm hover:bg-accent disabled:opacity-50"
        >
          {busy && <Loader2 className="size-4 animate-spin" />}
          Preview impact
        </button>
        <button
          type="button"
          onClick={save}
          disabled={busy || !dirty}
          className="rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
        >
          Save
        </button>
        <button
          type="button"
          onClick={reset}
          disabled={busy}
          className="ml-auto inline-flex items-center gap-1.5 rounded-md border px-3.5 py-2 text-sm hover:bg-accent disabled:opacity-50"
        >
          <RotateCcw className="size-3.5" />
          Reset to defaults
        </button>
      </div>
      {!dirty && <p className="mt-2 text-xs text-muted-foreground">No unsaved changes.</p>}
    </div>
  )
}

function CompaniesSection() {
  const { data, error, loading, reload } = useApi(() => api.companies.list(), [])
  const [name, setName] = useState("")
  const [scouting, setScouting] = useState(false)
  const [result, setResult] = useState<ScoutResponse>()
  const [adding, setAdding] = useState(false)

  const scout = useCallback(async () => {
    if (!name.trim()) return
    setScouting(true)
    setResult(undefined)
    try {
      setResult(await api.companies.scout(name.trim()))
    } catch (err) {
      toast.error("Scout failed", { description: err instanceof ApiError ? err.detail : String(err) })
    } finally {
      setScouting(false)
    }
  }, [name])

  const add = useCallback(async () => {
    if (!result?.success || !result.ats || !result.token) return
    setAdding(true)
    try {
      await api.companies.add({
        name: result.company_name,
        ats: result.ats,
        token: result.token,
        notes: "found by Scout",
      })
      toast.success(`${result.company_name} added to companies.yaml`)
      setResult(undefined)
      setName("")
      reload()
    } catch (err) {
      toast.error("Couldn't add", { description: err instanceof ApiError ? err.detail : String(err) })
    } finally {
      setAdding(false)
    }
  }, [result, reload])

  if (error) return <ErrorState error={error} onRetry={reload} />

  const zero = new Set(data?.zero_survivor_companies ?? [])

  return (
    <div>
      <div className="rounded-lg border p-3.5">
        <h3 className="text-sm font-medium">Add a company</h3>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Scout works out which ATS a company uses and finds its board token by testing candidates against the
          real APIs. It spends LLM quota only if mechanical guesses fail.
        </p>
        <div className="mt-2.5 flex gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void scout()}
            placeholder="Company name…"
            className="min-w-0 flex-1 rounded-md border bg-background px-3 py-2 text-sm"
          />
          <button
            type="button"
            onClick={scout}
            disabled={scouting || !name.trim()}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
          >
            {scouting ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />}
            Scout
          </button>
        </div>

        {scouting && (
          <p className="mt-2 text-xs text-muted-foreground">
            Testing candidate tokens against Greenhouse, Lever and Ashby — this can take a minute.
          </p>
        )}

        {result && (
          <div
            className={cn(
              "mt-3 rounded-md border px-3 py-2.5",
              result.success
                ? "border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/40"
                : "border-muted bg-muted/30",
            )}
          >
            <p className="text-sm">{result.conclusion}</p>
            <p className="mt-1 text-[0.68rem] text-muted-foreground">
              {result.requests_used} request(s) used
              {(result.empty_boards ?? []).length > 0 && ` · ${(result.empty_boards ?? []).length} empty board(s) found`}
            </p>
            {result.success && (
              <button
                type="button"
                onClick={add}
                disabled={adding}
                className="mt-2 inline-flex items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-50"
              >
                {adding && <Loader2 className="size-3 animate-spin" />}
                Add {result.company_name} ({result.ats}/{result.token})
              </button>
            )}
          </div>
        )}
      </div>

      <div className="mt-4">
        <div className="mb-2 flex items-baseline justify-between">
          <h3 className="text-sm font-medium">Roster</h3>
          <span className="text-xs text-muted-foreground">{data?.companies.length ?? 0} companies</span>
        </div>

        {loading && <p className="text-sm text-muted-foreground">Loading…</p>}

        {zero.size > 0 && (
          <p className="mb-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs dark:border-amber-800 dark:bg-amber-950/40">
            {zero.size} company(ies) fetched jobs but produced zero survivors — candidates for removal.
          </p>
        )}

        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Company</th>
                <th className="px-3 py-2 font-medium">ATS</th>
                <th className="px-3 py-2 font-medium">Token</th>
                <th className="px-3 py-2 text-right font-medium">Fetched</th>
                <th className="px-3 py-2 text-right font-medium">Survivors</th>
              </tr>
            </thead>
            <tbody>
              {(data?.companies ?? [])
                .slice()
                .sort((a, b) => a.survivors - b.survivors || a.name.localeCompare(b.name))
                .map((c) => (
                  <tr key={`${c.name}-${c.token}`} className="border-t">
                    <td className="px-3 py-1.5">
                      {c.name}
                      {zero.has(c.name) && (
                        <Badge variant="secondary" className="ml-2 text-[0.6rem]">
                          0 survivors
                        </Badge>
                      )}
                    </td>
                    <td className="px-3 py-1.5 text-muted-foreground">{c.ats}</td>
                    <td className="px-3 py-1.5 font-mono text-xs text-muted-foreground">{c.token}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{c.jobs_fetched}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{c.survivors}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

export function SettingsPage() {
  const [tab, setTab] = useState<"roles" | "companies">("roles")

  return (
    <div>
      <PageHeader title="Settings" description="Which roles count, and which companies get checked." />

      <div className="mb-4 flex gap-1 border-b">
        {(["roles", "companies"] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={cn(
              "-mb-px border-b-2 px-3 py-2 text-sm capitalize",
              tab === t
                ? "border-primary font-medium"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t}
          </button>
        ))}
      </div>

      <Card className="shadow-sm">
        <CardContent className="px-5 py-4">
          {tab === "roles" ? <RolesSection /> : <CompaniesSection />}
        </CardContent>
      </Card>
    </div>
  )
}
