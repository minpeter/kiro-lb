# frontend/ — operations dashboard

Bun + Vite + React 19 + Tailwind 4 SPA. Built output is committed into
`../kiro/static` and served by FastAPI; there is no separate dashboard server.

## WHERE TO LOOK

| Task | Location |
|---|---|
| Page composition | `src/features/dashboard/` |
| Shared primitives | `src/components/ui/` (radix-based) |
| Build/output wiring | `vite.config.ts` (`outDir: "../kiro/static"`) |
| Backend API contract | `../kiro/dashboard.py` (`/api/dashboard/*`) |

## CONVENTIONS

- Package manager is Bun 1.3.7 (`packageManager` in `package.json`). Do not
  introduce npm or pnpm lockfiles.
- `bun run build` runs `tsc -b` first; type errors block the build by design.
- Fonts are vendored under `public/fonts` and mounted by the backend at `/fonts`.

## ANTI-PATTERNS

- Editing `../kiro/static/**` directly instead of rebuilding here.
- Adding a dev-only proxy assumption; the SPA is served same-origin in production.
- Committing a build without running `bun run typecheck`.
