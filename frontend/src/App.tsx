import { Navigate, Route, Routes } from "react-router-dom"

import { AppShell } from "@/components/AppShell"
import { RequireRecruiterMode } from "@/components/RequireRecruiterMode"
import { AgentMetricsPage } from "@/pages/AgentMetricsPage"
import { ApplicationsPage } from "@/pages/ApplicationsPage"
import { ArchitecturePage } from "@/pages/ArchitecturePage"
import { CoachPage } from "@/pages/CoachPage"
import { EvaluationPage } from "@/pages/EvaluationPage"
import { HomePage } from "@/pages/HomePage"
import { JobsPage } from "@/pages/JobsPage"
import { MissionControlPage } from "@/pages/MissionControlPage"
import { ResumePage } from "@/pages/ResumePage"
import { SettingsPage } from "@/pages/SettingsPage"

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<HomePage />} />
        <Route path="jobs" element={<JobsPage />} />

        <Route path="mission-control" element={<MissionControlPage />} />
        <Route path="resume" element={<ResumePage />} />
        <Route path="applications" element={<ApplicationsPage />} />
        <Route path="coach" element={<CoachPage />} />
        <Route path="settings" element={<SettingsPage />} />

        {/* Recruiter Mode only. Guarded rather than merely hidden from the
            nav - a bookmarked URL shouldn't bypass the toggle. */}
        <Route element={<RequireRecruiterMode />}>
          <Route path="architecture" element={<ArchitecturePage />} />
          <Route path="evaluation" element={<EvaluationPage />} />
          <Route path="agents" element={<AgentMetricsPage />} />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
