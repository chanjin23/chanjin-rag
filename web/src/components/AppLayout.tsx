import { NavLink, Outlet } from "react-router";
import { cn } from "@/lib/cn";

const NAV = [
  { to: "/", label: "채팅", end: true },
  { to: "/documents", label: "문서" },
  { to: "/sync", label: "동기화" },
  { to: "/eval", label: "평가" },
];

export function AppLayout() {
  return (
    <div className="flex h-dvh flex-col bg-slate-50 text-slate-900">
      <header className="flex items-center gap-6 border-b border-slate-200 bg-white px-4 py-3">
        <span className="font-semibold">chanjin-rag</span>
        <nav className="flex gap-1 text-sm">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  "rounded-md px-3 py-1.5",
                  isActive ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="min-h-0 flex-1">
        <Outlet />
      </main>
    </div>
  );
}
