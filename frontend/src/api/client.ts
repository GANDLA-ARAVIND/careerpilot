/**
 * Typed fetch wrapper over the CareerPilot API.
 *
 * Types are NOT hand-written - src/api/schema.d.ts is generated from the
 * backend's own OpenAPI document (`npm run gen:api`), so the shapes here
 * cannot drift from the Pydantic response models. If the backend renames a
 * field, this file stops compiling, which is the point.
 */

import type { components } from "./schema"

export type JobSummary = components["schemas"]["JobSummary"]
export type JobDetail = components["schemas"]["JobDetail"]
export type StatsResponse = components["schemas"]["StatsResponse"]
export type RejectedPage = components["schemas"]["RejectedPage"]
export type StatusUpdateResponse = components["schemas"]["StatusUpdateResponse"]
export type RunStatus = components["schemas"]["RunStatus"]
export type RunEvent = components["schemas"]["RunEvent"]
export type RunStartResponse = components["schemas"]["RunStartResponse"]
export type PreferencesResponse = components["schemas"]["PreferencesResponse"]
export type ResumeResponse = components["schemas"]["ResumeResponse"]
export type CompaniesResponse = components["schemas"]["CompaniesResponse"]
export type SkillGapsResponse = components["schemas"]["SkillGapsResponse"]
export type AgentsResponse = components["schemas"]["AgentsResponse"]
export type AgentMetric = components["schemas"]["AgentMetric"]
export type RunHistoryResponse = components["schemas"]["RunHistoryResponse"]
export type RunHistoryEntry = components["schemas"]["RunHistoryEntry"]
export type ResumePreview = components["schemas"]["ResumePreview"]
export type ResumeConfirmResponse = components["schemas"]["ResumeConfirmResponse"]
export type PreferencesImpact = components["schemas"]["PreferencesImpact"]
export type ScoutResponse = components["schemas"]["ScoutResponse"]
export type AskResponse = components["schemas"]["AskResponse"]
export type CompanyStats = components["schemas"]["CompanyStats"]
export type EvaluationResponse = components["schemas"]["EvaluationResponse"]
export type ArchitectureResponse = components["schemas"]["ArchitectureResponse"]
export type RuntimeResponse = components["schemas"]["RuntimeResponse"]

/** The four statuses the backend accepts. Anything else is a 422. */
export const APPLICATION_STATUSES = ["new", "applied", "rejected", "interviewing"] as const
export type ApplicationStatus = (typeof APPLICATION_STATUSES)[number]

export class ApiError extends Error {
  // Declared as plain fields rather than constructor parameter properties:
  // this project's tsconfig sets `erasableSyntaxOnly`, which forbids the
  // shorthand because it emits real code rather than being erased.
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(`${status}: ${detail}`)
    this.name = "ApiError"
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // FormData must NOT get an explicit Content-Type: the browser has to set
  // it itself so it can include the multipart boundary. Setting
  // application/json here would make the resume upload unparseable
  // server-side.
  const isFormData = typeof FormData !== "undefined" && init?.body instanceof FormData
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body && !isFormData ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    // FastAPI puts the message in `detail`, which may itself be an array
    // for validation errors. Surface something readable either way rather
    // than "[object Object]".
    let detail = response.statusText
    try {
      const body = await response.json()
      detail =
        typeof body.detail === "string"
          ? body.detail
          : Array.isArray(body.detail)
            ? body.detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join("; ")
            : JSON.stringify(body)
    } catch {
      /* non-JSON error body - keep statusText */
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  jobs: {
    list: (params?: { status?: string[]; includeUnscored?: boolean }) => {
      const query = new URLSearchParams()
      params?.status?.forEach((s) => query.append("status", s))
      if (params?.includeUnscored === false) query.set("include_unscored", "false")
      const qs = query.toString()
      return request<JobSummary[]>(`/api/jobs${qs ? `?${qs}` : ""}`)
    },
    get: (contentHash: string) => request<JobDetail>(`/api/jobs/${contentHash}`),
    setStatus: (contentHash: string, status: ApplicationStatus) =>
      request<StatusUpdateResponse>(`/api/jobs/${contentHash}/status`, {
        method: "POST",
        body: JSON.stringify({ status }),
      }),
    rejected: (params?: { rule?: string; page?: number; pageSize?: number }) => {
      const query = new URLSearchParams()
      if (params?.rule) query.set("rule", params.rule)
      if (params?.page) query.set("page", String(params.page))
      if (params?.pageSize) query.set("page_size", String(params.pageSize))
      const qs = query.toString()
      return request<RejectedPage>(`/api/jobs/rejected${qs ? `?${qs}` : ""}`)
    },
  },
  stats: () => request<StatsResponse>("/api/stats"),
  run: {
    /**
     * threadId forces a fresh LangGraph thread. Left unset for a normal
     * run so the date-based thread keeps its crash-resume behaviour; set
     * only when re-running a day whose thread already completed, which
     * would otherwise be a silent no-op (see api/schemas/run.py).
     */
    start: (threadId?: string) =>
      request<RunStartResponse>("/api/run", {
        method: "POST",
        body: JSON.stringify({ thread_id: threadId ?? null }),
      }),
    status: () => request<RunStatus>("/api/run/status"),
  },
  resume: {
    get: () => request<ResumeResponse>("/api/resume"),
    /**
     * Preview only - saves nothing. Either a PDF or pasted text; the
     * backend extracts, runs the mangled-extraction tripwires, and reports
     * how many cached analyses a save would invalidate.
     */
    preview: (input: { file?: File; text?: string }) => {
      const form = new FormData()
      if (input.file) form.append("file", input.file)
      if (input.text) form.append("text", input.text)
      // No Content-Type header: the browser must set the multipart
      // boundary itself, and setting it manually breaks the parse.
      return request<ResumePreview>("/api/resume", { method: "POST", body: form })
    },
    confirm: (text: string) =>
      request<ResumeConfirmResponse>("/api/resume/confirm", {
        method: "POST",
        body: JSON.stringify({ text }),
      }),
  },
  preferences: {
    get: () => request<PreferencesResponse>("/api/preferences"),
    preview: (prefs: PreferencesBody) =>
      request<PreferencesImpact>("/api/preferences/preview", {
        method: "POST",
        body: JSON.stringify(prefs),
      }),
    save: (prefs: PreferencesBody) =>
      request<PreferencesImpact>("/api/preferences", { method: "PUT", body: JSON.stringify(prefs) }),
    reset: () => request<PreferencesResponse>("/api/preferences/reset", { method: "POST" }),
  },
  companies: {
    list: () => request<CompaniesResponse>("/api/companies"),
    scout: (name: string) =>
      request<ScoutResponse>("/api/companies/scout", { method: "POST", body: JSON.stringify({ name }) }),
    add: (entry: { name: string; ats: string; token: string; notes?: string | null }) =>
      request<CompanyStats>("/api/companies", { method: "POST", body: JSON.stringify(entry) }),
  },
  coach: {
    skillGaps: (threshold = 40, limit = 20) =>
      request<SkillGapsResponse>(`/api/coach/skill-gaps?threshold=${threshold}&limit=${limit}`),
    ask: (question: string, k = 8) =>
      request<AskResponse>("/api/coach/ask", { method: "POST", body: JSON.stringify({ question, k }) }),
  },
  meta: {
    agents: () => request<AgentsResponse>("/api/meta/agents"),
    runs: (limit = 20) => request<RunHistoryResponse>(`/api/meta/runs?limit=${limit}`),
    evaluation: () => request<EvaluationResponse>("/api/meta/evaluation"),
    architecture: () => request<ArchitectureResponse>("/api/meta/architecture"),
    runtime: () => request<RuntimeResponse>("/api/meta/runtime"),
  },
}

export interface PreferencesBody {
  title_allowlist: string[]
  seniority_keywords: string[]
  non_engineering_keywords: string[]
  india_location_keywords: string[]
}
