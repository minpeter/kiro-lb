# frontend/ — operations dashboard

Bun + Vite 7 + React 19 + Tailwind 4 SPA, ~2.6k lines across 31 source files.
Built output is committed into `../kiro/static` and served by FastAPI; there is
no separate dashboard server. `README.md` in this directory still says
`../app/static`, a `/backend-api` proxy, and test scripts that do not exist —
all wrong; trust `vite.config.ts` and `package.json`.

## WHERE TO LOOK

| Task | Location |
|---|---|
| Page composition | `src/features/dashboard/components/` (10 panels/cards) |
| Data fetching + polling | `src/features/dashboard/use-dashboard.ts`, `api.ts` |
| Auth state | `use-dashboard.ts`; `AUTH_REQUIRED` from `api.ts` means show `LoginCard` |
| Response shapes | `src/features/dashboard/types.ts` (mirror of `dashboard.py` JSON) |
| Shared primitives | `src/components/ui/` (radix/shadcn, 10 files) |
| Theme tokens | `src/index.css` (CSS variables; `main.tsx` sets the `.dark` class) |
| Build/output wiring | `vite.config.ts` (`outDir: "../kiro/static"`, `emptyOutDir: true`) |
| Backend API contract | `../kiro/dashboard.py` (`/api/dashboard/*`) |

## CONVENTIONS

- Package manager is Bun 1.4.0 (`packageManager` in `package.json`). Do not
  introduce npm or pnpm lockfiles.
- `bun run build` runs `tsc -b` first; type errors block the build by design.
  CI also runs `bun run lint` and `bun run typecheck` in the `quality` job.
- Fonts are vendored under `public/fonts` and mounted by the backend at `/fonts`
  (`../main.py:605`).
- `vite dev` proxies `/api`, `/v1`, and `/health` to `API_PROXY_TARGET`
  (default `http://localhost:8000`); the gateway must be running for dev mode.
- No auth token lives in the client. Session is the server-side `kiro_lb_session`
  cookie; every call is a same-origin `fetch`, and any non-2xx becomes
  `DashboardApiError`.
- All dashboard state lives in the single `useDashboard()` hook. Live polling runs
  once a second only while authenticated and live; request-log pages are guarded
  by a request-id so a slow page cannot overwrite a newer one
  (`use-dashboard.ts:53`).
- Device-login registration is single-shot, guarded by a ref against overlapping
  polls.
- `isUnroutable()` (`src/features/dashboard/routing-state.ts`) decides which
  accounts the rate chart hides. It is deliberately narrower than "renders as a
  destructive badge": `rate_limited` and `cooling_down` are red but clear on their
  own, and they are exactly what the rate chart is for. Only `suspended`,
  `auth_dead`, `quota_exhausted`, and `quota_depleted` hide. Hidden panels are
  disclosed by a toggle, never silently dropped.
- Any cell rendering upstream text must bound its own width. `UsageErrorCell`
  (`components/accounts-panel.tsx`) exists because `usage.error` was rendered bare
  into a `whitespace-nowrap` `TableCell`: an httpx 401 message is 188 characters
  across two lines, so the row grew past the viewport and pushed every later column
  off-screen. The backend now summarizes these, but the cell must not rely on that
  — `max-w-40` + `whitespace-normal break-words line-clamp-2` holds for any string,
  with the full text kept reachable via `title` rather than truncated away.
- `cn()` in `src/lib/utils.ts` (clsx + tailwind-merge) is how class names compose;
  shadcn config lives in `components.json` (new-york, CSS variables, Lucide).

## ANTI-PATTERNS

- Editing `../kiro/static/**` directly instead of rebuilding here.
- Adding a dev-only proxy assumption; the SPA is served same-origin in production.
- Committing a build without running `bun run typecheck`.
- Widening the rate chart's hide rule to every `destructive` badge state. It
  would hide rate-limited accounts from the rate chart, removing the evidence of
  the condition being diagnosed.
- Ad hoc tab buttons; extend the `Tabs` primitive and `useTabHash()` instead
  (an unknown hash falls back to `overview`).
- Adding a formatter config. There is no Prettier or Biome here; ESLint flat
  config (`eslint.config.js`) is the only gate.
