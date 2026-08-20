/**
 * User Mode vs Recruiter Mode.
 *
 * User Mode is the default and the real product: the thing opened at 8am to
 * find work. Recruiter Mode adds pages that explain how the system is built
 * and how well it measurably performs - it reveals sections, it never
 * changes what the underlying data says.
 *
 * Persisted to localStorage so a reload doesn't drop you back to User Mode
 * mid-demo.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react"
import type { ReactNode } from "react"

export type AppMode = "user" | "recruiter"

const STORAGE_KEY = "careerpilot:mode"

interface ModeContextValue {
  mode: AppMode
  isRecruiter: boolean
  setMode: (mode: AppMode) => void
  toggle: () => void
}

const ModeContext = createContext<ModeContextValue | undefined>(undefined)

function readStoredMode(): AppMode {
  if (typeof window === "undefined") return "user"
  return window.localStorage.getItem(STORAGE_KEY) === "recruiter" ? "recruiter" : "user"
}

export function ModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<AppMode>(readStoredMode)

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, mode)
  }, [mode])

  const setMode = useCallback((next: AppMode) => setModeState(next), [])
  const toggle = useCallback(() => setModeState((m) => (m === "user" ? "recruiter" : "user")), [])

  const value = useMemo(
    () => ({ mode, isRecruiter: mode === "recruiter", setMode, toggle }),
    [mode, setMode, toggle],
  )

  return <ModeContext.Provider value={value}>{children}</ModeContext.Provider>
}

export function useMode(): ModeContextValue {
  const context = useContext(ModeContext)
  if (!context) throw new Error("useMode must be used inside a ModeProvider")
  return context
}
