# frontend/ — operations dashboard

Bun + Vite + React 19 + Tailwind 4 SPA, ~2.3k lines across 29 source files.
Built output is committed into `../kiro/static` and served by FastAPI; there is
no separate dashboard server.

## WHERE TO LOOK

| Task | Location |
|---|---|
| Page composition | `src/features/dashboard/components/` (11 panels/cards) |
| Data fetching + polling | `src/features/dashboard/use-dashboard.ts`, `api.ts` |
| Response shapes | `src/features/dashboard/types.ts` (mirror of `dashboard.py` JSON) |
| Shared primitives | `src/components/ui/` (radix-based) |
| Build/output wiring | `vite.config.ts` (`outDir: "../kiro/static"`) |
| Backend API contract | `../kiro/dashboard.py` (`/api/dashboard/*`) |

## CONVENTIONS

- Package manager is Bun 1.3.7 (`packageManager` in `package.json`). Do not
  introduce npm or pnpm lockfiles.
- `bun run build` runs `tsc -b` first; type errors block the build by design.
- Fonts are vendored under `public/fonts` and mounted by the backend at `/fonts`
  (`../main.py:653`).
- `vite dev` proxies `/api`, `/v1`, and `/health` to `API_PROXY_TARGET`
  (default `http://localhost:8000`); the gateway must be running for dev mode.

## ANTI-PATTERNS

- Editing `../kiro/static/**` directly instead of rebuilding here.
- Adding a dev-only proxy assumption; the SPA is served same-origin in production.
- Committing a build without running `bun run typecheck`.
