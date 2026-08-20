import { ArrowDown, Bot, Cpu, Database } from "lucide-react"

import { api } from "@/api/client"
import { ErrorState, PageHeader } from "@/components/PageHeader"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { useApi } from "@/lib/useApi"
import { cn } from "@/lib/utils"

export function ArchitecturePage() {
  const { data, error, loading, reload } = useApi(() => api.meta.architecture(), [])

  return (
    <div>
      <PageHeader
        title="Architecture"
        description="Rendered from /api/meta/architecture — the backend describes its own pipeline."
      />

      {error && <ErrorState error={error} onRetry={reload} />}
      {loading && !error && <p className="text-sm text-muted-foreground">Loading…</p>}

      {data && (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
          <Card className="shadow-sm">
            <CardContent className="px-5 py-4">
              <h2 className="mb-3 text-sm font-semibold">Pipeline</h2>
              <ol className="space-y-0">
                {data.stages.map((stage, i) => (
                  <li key={stage.key}>
                    <div
                      className={cn(
                        "rounded-lg border p-3.5",
                        stage.uses_llm
                          ? "border-primary/40 bg-primary/5"
                          : "bg-card",
                      )}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-semibold tabular-nums">
                          {i + 1}
                        </span>
                        <h3 className="text-sm font-medium">{stage.name}</h3>
                        {stage.uses_llm ? (
                          <Badge className="gap-1 text-[0.6rem]">
                            <Cpu className="size-2.5" />
                            LLM
                          </Badge>
                        ) : (
                          <Badge variant="secondary" className="text-[0.6rem]">
                            no LLM
                          </Badge>
                        )}
                        <code className="ml-auto shrink-0 rounded bg-muted px-1.5 py-0.5 text-[0.65rem] text-muted-foreground">
                          {stage.module}
                        </code>
                      </div>
                      <p className="mt-1.5 text-xs text-muted-foreground">{stage.description}</p>
                    </div>
                    {i < data.stages.length - 1 && (
                      <div className="flex items-center gap-2 py-1.5 pl-3">
                        <ArrowDown className="size-3.5 text-muted-foreground" />
                        <span className="text-[0.68rem] text-muted-foreground">
                          {data.edges.find((e) => e.source === stage.key)?.label ?? ""}
                        </span>
                      </div>
                    )}
                  </li>
                ))}
              </ol>
            </CardContent>
          </Card>

          <div className="space-y-4">
            <Card className="shadow-sm">
              <CardContent className="px-4 py-3.5">
                <h2 className="mb-2 flex items-center gap-1.5 text-sm font-semibold">
                  <Bot className="size-4" />
                  Agents
                </h2>
                <ul className="space-y-3">
                  {data.agents.map((agent) => (
                    <li key={agent.name}>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-sm font-medium">{agent.name}</span>
                        {agent.runs_in_nightly_pipeline ? (
                          <Badge variant="secondary" className="text-[0.6rem]">
                            nightly
                          </Badge>
                        ) : (
                          <Badge variant="outline" className="text-[0.6rem]">
                            on demand
                          </Badge>
                        )}
                      </div>
                      <p className="mt-0.5 text-xs font-medium text-primary">Owns: {agent.decision}</p>
                      <p className="mt-0.5 text-xs text-muted-foreground">{agent.description}</p>
                      <code className="mt-1 inline-block rounded bg-muted px-1.5 py-0.5 text-[0.65rem] text-muted-foreground">
                        {agent.module}
                      </code>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>

            <Card className="shadow-sm">
              <CardContent className="px-4 py-3.5">
                <h2 className="mb-2 flex items-center gap-1.5 text-sm font-semibold">
                  <Database className="size-4" />
                  Design principles
                </h2>
                <ul className="space-y-2">
                  {data.principles.map((p) => (
                    <li key={p} className="text-xs leading-relaxed text-muted-foreground">
                      {p}
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          </div>
        </div>
      )}
    </div>
  )
}
