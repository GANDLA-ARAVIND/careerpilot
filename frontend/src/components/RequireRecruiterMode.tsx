import { Navigate, Outlet, useLocation } from "react-router-dom"

import { useMode } from "@/lib/mode"

/**
 * Route guard for the Recruiter Mode pages.
 *
 * Hiding them from the nav isn't enough on its own - a bookmarked or
 * shared /evaluation URL would otherwise render regardless of the toggle,
 * which makes the mode look decorative rather than real.
 */
export function RequireRecruiterMode() {
  const { isRecruiter } = useMode()
  const location = useLocation()

  if (!isRecruiter) {
    return <Navigate to="/" replace state={{ blockedFrom: location.pathname }} />
  }
  return <Outlet />
}
