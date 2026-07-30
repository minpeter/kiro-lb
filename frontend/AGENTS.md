# frontend/ — operations dashboard

Bun + Vite 7 + React 19 + Tailwind 4 SPA, ~2.6k lines across 31 source files.
Built output is committed into `../kiro/static` and served by FastAPI; there is
no separate dashboard server. `README.md` in this directory still says
`../app/static` and lists a `/backend-api` proxy — both are wrong; trust
`vite.config.ts`.

## WHERE TO LOOK

| Task | Location |
|---|---|
| Page composition | `src/features/dashboard/components/` (10 panels/cards) |
| Data fetching + polling | `src/features/dashboard/use-dashboard.ts`, `api.ts` |
| Auth state | `use-dashboard.ts`; `AUTH_REQUIRED` from `api.ts` means show `LoginCard` |
| Response shapes | `src/features/dashboard/types.ts` (mirror of `dashboard.py` JSON) |
| Shared primitives | `src/components/ui/` (radix-based, 10 files) |
| Build/output wiring | `vite.config.ts` (`outDir: "../kiro/static"`) |
| Backend API contract | `../kiro/dashboard.py` (`/api/dashboard/*`) |

## CONVENTIONS

- Package manager is Bun 1.3.7 (`packageManager` in `package.json`). Do not
  introduce npm or pnpm lockfiles.
- `bun run build` runs `tsc -b` first; type errors block the build by design.
- Fonts are vendored under `public/fonts` and mounted by the backend at `/fonts`
  (`../main.py:635`).
- `vite dev` proxies `/api`, `/v1`, and `/health` to `API_PROXY_TARGET`
  (default `http://localhost:8000`); the gateway must be running for dev mode.
- No auth token lives in the client. Session is a server-side cookie; every call
  is a same-origin `fetch`, and any non-2xx becomes `DashboardApiError`.
- All dashboard state lives in the single `useDashboard()` hook. Live polling runs
  once a second only while authenticated and live; request-log pages are guarded
  by a request-id so a slow page cannot overwrite a newer one.
- Device-login registration is single-shot, guarded by a ref against overlapping
  polls.

## ANTI-PATTERNS

- Editing `../kiro/static/**` directly instead of rebuilding here.
- Adding a dev-only proxy assumption; the SPA is served same-origin in production.
- Committing a build without running `bun run typecheck`.
- Ad hoc tab buttons; extend the `Tabs` primitive and `useTabHash()` instead
  (an unknown hash falls back to `overview`).
