/**
 * Minimal data-fetching hook.
 *
 * Deliberately not TanStack Query: this app has a handful of endpoints, no
 * pagination-heavy lists, no optimistic-update requirements, and one user.
 * A 40-line hook that reports loading/error/data honestly is the boring
 * solution, and adding a cache layer would mean reasoning about staleness
 * across a Run that rewrites the whole dataset. Revisit if the app grows a
 * real need, not before.
 */

import { useCallback, useEffect, useRef, useState } from "react"

import { ApiError } from "@/api/client"

export interface UseApiResult<T> {
  data: T | undefined
  error: string | undefined
  loading: boolean
  reload: () => void
}

export function useApi<T>(fetcher: () => Promise<T>, deps: unknown[] = []): UseApiResult<T> {
  const [data, setData] = useState<T>()
  const [error, setError] = useState<string>()
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)

  // Kept in a ref so changing the closure identity (which happens on every
  // render for an inline arrow) doesn't retrigger the effect - `deps` is
  // the explicit, intentional trigger.
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(undefined)

    fetcherRef
      .current()
      .then((result) => {
        if (cancelled) return
        setData(result)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(
          err instanceof ApiError
            ? err.detail
            : err instanceof Error
              ? err.message
              : "Unknown error",
        )
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  const reload = useCallback(() => setNonce((n) => n + 1), [])

  return { data, error, loading, reload }
}
