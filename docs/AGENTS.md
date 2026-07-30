# docs/ — authoritative references

Design decisions and translated user docs. Prefer citing these over restating
them elsewhere.

## MAP

| File | Authority |
|---|---|
| `adr/0001-real-upstream-reasoning-only.md` | Why reasoning is never synthesized |
| `router-design.md` | Routing invariants, failover ordering, upstream constraints |
| `en/ARCHITECTURE.md` | Adapter-pattern system overview |
| `ru/ARCHITECTURE.md` | Russian architecture doc (kept in sync) |
| `<lang>/README.md` | Translated user READMEs (ru, zh, es, id, pt, ja, ko) |

## CONVENTIONS

- ADRs are append-only; supersede with a new numbered ADR rather than rewriting.
- A behavior change that contradicts `router-design.md` or the ADR must update
  that document in the same change.
- Root `README.md` is the English source of truth; translations follow it.
