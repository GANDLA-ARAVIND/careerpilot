import type { ReactNode } from "react"

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <div className="mb-6 flex items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-xl border border-dashed px-6 py-14 text-center">
      <p className="font-medium">{title}</p>
      <p className="mx-auto mt-1 max-w-md text-sm text-muted-foreground">{body}</p>
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="rounded-xl border border-destructive/40 bg-destructive/5 px-6 py-8 text-center">
      <p className="font-medium text-destructive">Couldn't load this from the API</p>
      <p className="mx-auto mt-1 max-w-lg text-sm text-muted-foreground">{error}</p>
      <p className="mt-3 text-xs text-muted-foreground">
        Is the backend running? <code className="rounded bg-muted px-1.5 py-0.5">uvicorn api.main:app --port 8000</code>
      </p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-4 rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
          type="button"
        >
          Retry
        </button>
      )}
    </div>
  )
}
