import { NavLink, Outlet } from "react-router-dom"
import {
  Activity,
  Boxes,
  ClipboardList,
  Filter,
  FileText,
  FlaskConical,
  GaugeCircle,
  Home,
  MessageSquareText,
  Network,
  Settings,
  Sparkles,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"
import { useMode } from "@/lib/mode"

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
}

const USER_NAV: NavItem[] = [
  { to: "/", label: "Home", icon: Home },
  { to: "/jobs", label: "Jobs", icon: Boxes },
  { to: "/rejected", label: "Rejected", icon: Filter },
  { to: "/mission-control", label: "Mission Control", icon: Activity },
  { to: "/resume", label: "Resume", icon: FileText },
  { to: "/applications", label: "Applications", icon: ClipboardList },
  { to: "/coach", label: "Career Coach", icon: MessageSquareText },
  { to: "/settings", label: "Settings", icon: Settings },
]

const RECRUITER_NAV: NavItem[] = [
  { to: "/architecture", label: "Architecture", icon: Network },
  { to: "/evaluation", label: "Evaluation", icon: FlaskConical },
  { to: "/agents", label: "Agent Metrics", icon: GaugeCircle },
]

function NavSection({ items, label }: { items: NavItem[]; label?: string }) {
  return (
    <div className="space-y-1">
      {label && (
        <p className="px-3 pb-1 pt-4 text-[0.68rem] font-semibold uppercase tracking-wider text-muted-foreground">
          {label}
        </p>
      )}
      {items.map(({ to, label: itemLabel, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          end={to === "/"}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors",
              "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
              isActive && "bg-accent font-medium text-accent-foreground",
            )
          }
        >
          <Icon className="size-4 shrink-0" />
          {itemLabel}
        </NavLink>
      ))}
    </div>
  )
}

export function AppShell() {
  const { isRecruiter, toggle } = useMode()

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col border-r bg-sidebar">
        <div className="flex items-center gap-2 px-5 py-5">
          <Sparkles className="size-5 text-primary" />
          <div className="leading-tight">
            <p className="text-sm font-semibold tracking-tight">CareerPilot</p>
            {/* Second half of the repo-wide tagline: "CareerPilot — Multi-agent
                AI job discovery pipeline". Wraps to two lines in the 240px
                sidebar by design; leading-snug keeps that tight. */}
            <p className="text-[0.68rem] leading-snug text-muted-foreground">
              Multi-agent AI job discovery pipeline
            </p>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 pb-4">
          <NavSection items={USER_NAV} />
          {isRecruiter && <NavSection items={RECRUITER_NAV} label="Recruiter Mode" />}
        </nav>

        <div className="border-t px-3 py-3">
          <label
            className="flex cursor-pointer items-center justify-between gap-2 rounded-md px-2 py-1.5 hover:bg-accent"
            htmlFor="recruiter-mode"
          >
            <span className="flex flex-col">
              <span className="text-sm font-medium">Recruiter Mode</span>
              <span className="text-[0.68rem] text-muted-foreground">
                {isRecruiter ? "Technical detail shown" : "Show how it's built"}
              </span>
            </span>
            <Switch id="recruiter-mode" checked={isRecruiter} onCheckedChange={toggle} />
          </label>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {isRecruiter && (
          <div className="border-b bg-primary/5 px-8 py-2">
            <Badge variant="secondary" className="gap-1.5 font-normal">
              <GaugeCircle className="size-3" />
              Recruiter Mode — architecture, measured evaluation, and per-agent metrics are visible
            </Badge>
          </div>
        )}
        <main className="min-w-0 flex-1 px-8 py-7">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
