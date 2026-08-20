import { useCallback, useRef, useState } from "react"
import { AlertTriangle, CheckCircle2, FileUp, Loader2 } from "lucide-react"
import { toast } from "sonner"

import type { ResumePreview } from "@/api/client"
import { ApiError, api } from "@/api/client"
import { ErrorState, PageHeader } from "@/components/PageHeader"
import { Card, CardContent } from "@/components/ui/card"
import { useApi } from "@/lib/useApi"

function ExtractionPanel({
  extraction,
  emptyNote,
}: {
  extraction: { skills: string; projects: string; extracted: boolean }
  emptyNote: string
}) {
  return (
    <div className="space-y-2">
      {extraction.extracted ? (
        <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
          <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-emerald-600" />
          Recognized “Technical Skills” / “Projects” headers — only these sections are sent to the Analyst.
        </p>
      ) : (
        <p className="flex items-start gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-2 text-xs dark:border-amber-800 dark:bg-amber-950/40">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-amber-600" />
          <span>
            No recognized section headers found — the <strong>entire</strong> resume is sent to the Analyst
            instead of a trimmed section. {emptyNote}
          </span>
        </p>
      )}
      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <p className="mb-1 text-[0.65rem] font-semibold uppercase tracking-wide text-muted-foreground">
            Skills
          </p>
          <pre className="max-h-52 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-2.5 text-xs">
            {extraction.skills || "(nothing extracted)"}
          </pre>
        </div>
        <div>
          <p className="mb-1 text-[0.65rem] font-semibold uppercase tracking-wide text-muted-foreground">
            Projects
          </p>
          <pre className="max-h-52 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-2.5 text-xs">
            {extraction.projects || "(nothing extracted)"}
          </pre>
        </div>
      </div>
    </div>
  )
}

export function ResumePage() {
  const current = useApi(() => api.resume.get(), [])
  const [preview, setPreview] = useState<ResumePreview>()
  const [pasted, setPasted] = useState("")
  const [busy, setBusy] = useState(false)
  const [saving, setSaving] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  const runPreview = useCallback(async (input: { file?: File; text?: string }) => {
    setBusy(true)
    try {
      setPreview(await api.resume.preview(input))
    } catch (err) {
      toast.error("Couldn't read that", { description: err instanceof ApiError ? err.detail : String(err) })
    } finally {
      setBusy(false)
    }
  }, [])

  const handleConfirm = useCallback(async () => {
    if (!preview) return
    setSaving(true)
    try {
      const result = await api.resume.confirm(preview.text)
      toast.success("Resume saved", {
        description: `${result.invalidated_cached_analyses} cached analysis(es) are now stale — re-run the pipeline to refresh them.`,
      })
      setPreview(undefined)
      setPasted("")
      if (fileInput.current) fileInput.current.value = ""
      current.reload()
    } catch (err) {
      toast.error("Couldn't save", { description: err instanceof ApiError ? err.detail : String(err) })
    } finally {
      setSaving(false)
    }
  }, [preview, current])

  return (
    <div>
      <PageHeader
        title="Resume"
        description="What the Analyst actually reads. Nothing is saved until you confirm."
      />

      {current.error && <ErrorState error={current.error} onRetry={current.reload} />}

      {!current.error && (
        <div className="space-y-5">
          <Card className="shadow-sm">
            <CardContent className="px-5 py-4">
              <div className="mb-3 flex items-baseline justify-between gap-3">
                <h2 className="text-sm font-semibold">Currently active</h2>
                {current.data?.exists && (
                  <span className="text-xs text-muted-foreground">
                    {current.data.length.toLocaleString()} characters
                  </span>
                )}
              </div>

              {current.loading && <p className="text-sm text-muted-foreground">Loading…</p>}

              {!current.loading && !current.data?.exists && (
                <p className="text-sm text-muted-foreground">
                  No resume saved yet. Upload a PDF or paste text below.
                </p>
              )}

              {current.data?.exists && (
                <div className="space-y-3">
                  <ExtractionPanel
                    extraction={current.data.extraction}
                    emptyNote="That still works, it's just less focused."
                  />
                  <details>
                    <summary className="cursor-pointer text-xs font-medium text-primary hover:underline">
                      Show full saved text
                    </summary>
                    <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-3 text-xs">
                      {current.data.text}
                    </pre>
                  </details>
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="shadow-sm">
            <CardContent className="px-5 py-4">
              <h2 className="mb-3 text-sm font-semibold">Replace it</h2>

              <div className="flex flex-wrap items-center gap-3">
                <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border px-3 py-2 text-sm hover:bg-accent">
                  <FileUp className="size-4" />
                  Choose PDF
                  <input
                    ref={fileInput}
                    type="file"
                    accept="application/pdf"
                    className="hidden"
                    onChange={(e) => {
                      const file = e.target.files?.[0]
                      if (file) void runPreview({ file })
                    }}
                  />
                </label>
                <span className="text-xs text-muted-foreground">or paste below</span>
              </div>

              <textarea
                value={pasted}
                onChange={(e) => setPasted(e.target.value)}
                placeholder="Paste resume text…"
                rows={5}
                className="mt-3 w-full rounded-md border bg-background p-2.5 text-sm"
              />
              <button
                type="button"
                disabled={!pasted.trim() || busy}
                onClick={() => void runPreview({ text: pasted })}
                className="mt-2 rounded-md border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
              >
                {busy ? <Loader2 className="size-4 animate-spin" /> : "Preview pasted text"}
              </button>
            </CardContent>
          </Card>

          {preview && (
            <Card className="border-primary/40 shadow-sm">
              <CardContent className="px-5 py-4">
                <h2 className="mb-1 text-sm font-semibold">Preview — not saved yet</h2>
                <p className="mb-3 text-xs text-muted-foreground">
                  {preview.length.toLocaleString()} characters extracted
                  {preview.previous_length != null && ` (current resume is ${preview.previous_length.toLocaleString()})`}
                </p>

                {/* Mangled-PDF tripwires from the backend. Shown before the
                    save button, because a character count alone won't reveal
                    interleaved columns or glued-together words. */}
                {(preview.warnings ?? []).length > 0 ? (
                  <div className="mb-3 space-y-1.5">
                    {(preview.warnings ?? []).map((w) => (
                      <p
                        key={w}
                        className="flex items-start gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-2 text-xs dark:border-amber-800 dark:bg-amber-950/40"
                      >
                        <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-amber-600" />
                        {w}
                      </p>
                    ))}
                  </div>
                ) : (
                  <p className="mb-3 flex items-start gap-1.5 text-xs text-muted-foreground">
                    <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-emerald-600" />
                    No obvious extraction problems detected — still worth reading the text below before saving.
                  </p>
                )}

                <ExtractionPanel
                  extraction={preview.extraction}
                  emptyNote="Worth checking the headers in your PDF before saving."
                />

                <details className="mt-3">
                  <summary className="cursor-pointer text-xs font-medium text-primary hover:underline">
                    Show full extracted text
                  </summary>
                  <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-3 text-xs">
                    {preview.text}
                  </pre>
                </details>

                <div className="mt-4 rounded-md border border-amber-300 bg-amber-50 px-3 py-2.5 dark:border-amber-800 dark:bg-amber-950/40">
                  <p className="text-xs">
                    Saving invalidates{" "}
                    <strong>{preview.invalidates_cached_analyses} cached analysis(es)</strong>. The resume text
                    is part of every cached verdict's identity, so those entries simply stop matching — nothing
                    needs clearing, but the next run spends real LLM quota re-scoring them.
                  </p>
                </div>

                <div className="mt-3 flex gap-2">
                  <button
                    type="button"
                    onClick={handleConfirm}
                    disabled={saving}
                    className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
                  >
                    {saving && <Loader2 className="size-4 animate-spin" />}
                    Confirm and save
                  </button>
                  <button
                    type="button"
                    onClick={() => setPreview(undefined)}
                    className="rounded-md border px-4 py-2 text-sm hover:bg-accent"
                  >
                    Discard
                  </button>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </div>
  )
}
