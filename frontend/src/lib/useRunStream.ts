/**
 * Subscribes to GET /api/run/stream (SSE) and keeps a deduplicated,
 * ordered view of the current run.
 *
 * Three things this has to get right, all of which mirror guarantees the
 * backend already makes (see api/routers/run.py):
 *
 * 1. Mid-run connect. The server replays every event of the current run
 *    before switching to live ones, so opening the page at job 30 of 41
 *    shows the whole timeline rather than an empty panel.
 *
 * 2. Reconnect without duplicates. EventSource reconnects on its own after
 *    a dropped connection, and the server replays from the start again -
 *    so events are keyed by `seq` and merged, never appended blindly.
 *    Without that, a flaky connection would show each stage twice.
 *
 * 3. Close on `done`. The server ends the stream with a `done` event and
 *    returns. EventSource would treat that clean close as a failure and
 *    reconnect forever, so the connection is closed explicitly instead.
 */

import { useCallback, useEffect, useRef, useState } from "react"

import type { RunEvent, RunStatus } from "@/api/client"
import { api } from "@/api/client"

export type ConnectionState = "connecting" | "open" | "reconnecting" | "closed"

export interface RunStreamState {
  events: RunEvent[]
  status: RunStatus | undefined
  connection: ConnectionState
  /** Server-side run state: idle | running | completed | failed. */
  runState: string
  refreshStatus: () => void
}

export function useRunStream(): RunStreamState {
  const [events, setEvents] = useState<RunEvent[]>([])
  const [status, setStatus] = useState<RunStatus>()
  const [connection, setConnection] = useState<ConnectionState>("connecting")
  const [statusNonce, setStatusNonce] = useState(0)

  // seq -> event. A Map keyed by the server's sequence number is what
  // makes replay-after-reconnect idempotent.
  const bySeq = useRef<Map<number, RunEvent>>(new Map())

  const refreshStatus = useCallback(() => setStatusNonce((n) => n + 1), [])

  // Run status carries run_id / started_at / finished_at, which the
  // progress events themselves don't. Fetched on mount, and again whenever
  // the stream reports the run finished.
  useEffect(() => {
    let cancelled = false
    api.run
      .status()
      .then((result) => {
        if (!cancelled) setStatus(result)
      })
      .catch(() => {
        /* the stream's own error state already reports connectivity */
      })
    return () => {
      cancelled = true
    }
  }, [statusNonce])

  useEffect(() => {
    const source = new EventSource("/api/run/stream")
    let closed = false

    const mergeEvent = (raw: string) => {
      try {
        const event = JSON.parse(raw) as RunEvent
        bySeq.current.set(event.seq, event)
        setEvents(Array.from(bySeq.current.values()).sort((a, b) => a.seq - b.seq))
      } catch {
        /* a malformed frame shouldn't tear down the whole timeline */
      }
    }

    source.onopen = () => {
      if (!closed) setConnection("open")
    }

    source.addEventListener("progress", (event) => {
      mergeEvent((event as MessageEvent<string>).data)
    })

    source.addEventListener("done", () => {
      // Explicit close: the server is finished, and EventSource would
      // otherwise read the closed stream as an error and reconnect.
      closed = true
      source.close()
      setConnection("closed")
      refreshStatus()
    })

    source.addEventListener("heartbeat", () => {
      // Nothing to record - its only job is keeping an idle connection
      // alive through proxies while the Analyst waits on its rate limiter.
      if (!closed) setConnection("open")
    })

    source.onerror = () => {
      // EventSource retries by itself; this only reflects that in the UI.
      // Not treated as fatal - a backend restart mid-run should recover.
      if (!closed) setConnection("reconnecting")
    }

    return () => {
      closed = true
      source.close()
    }
  }, [refreshStatus])

  return {
    events,
    status,
    connection,
    runState: status?.status ?? "idle",
    refreshStatus,
  }
}
