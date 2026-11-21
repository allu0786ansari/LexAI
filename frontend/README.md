# LexAI frontend

React 19 + TypeScript + Vite + Tailwind. Talks only to the API v1 (`/api/v1/...`).

```bash
npm install
npm run dev          # http://localhost:5173; /api is proxied to http://localhost:8000
```

Point the dev proxy at another API with `LEXAI_API_URL=http://host:8000 npm run dev`,
or build for a separate API host with `VITE_API_BASE=https://api.example.org npm run build`.

| Command | What it does |
| --- | --- |
| `npm run build` | type check, then production build into `dist/` |
| `npm run typecheck` / `npm run lint` | `tsc --noEmit` / oxlint |
| `npm test` | Vitest: SSE parser, chat reducer (incl. "no save per token"), citation links |
| `npm run e2e` | Playwright against the built app with the API mocked (desktop + phone) |
| `npm run gen:api` | regenerate `src/api/schema.d.ts` from `src/api/openapi.json` |

## API types

`src/api/openapi.json` is written by the backend (`python scripts/export_openapi.py` in
`backend/`); `npm run gen:api` turns it into TypeScript. CI regenerates both and fails if
either differs from the committed files, so the client cannot drift from the API.

## Layout

| Path | |
| --- | --- |
| `src/api/` | typed client, SSE parser, generated schema |
| `src/state/chats.ts` | conversations as a reducer; saved to `localStorage` only when an answer ends |
| `src/lib/citations.ts` | `[n]` markers → buttons; only http(s) links survive |
| `src/components/` | sidebar/drawer, message, composer, filters, source panel, verification badge |
| `e2e/` | Playwright specs and the mock API |

Answers are rendered as Markdown with raw HTML ignored and links restricted to http(s).
Fonts are bundled (`@fontsource`): the page makes no third-party requests.
