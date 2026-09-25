# Frontend

React 19 single-page app for the FPL / Dream11 game. It is built with Vite and
styled with Tailwind, with `@dnd-kit` for drag-and-drop lineup editing and
`react-router-dom` for routing.

## Relation to the backend

The frontend only talks to the FastAPI app in `backend/Context_assembler/main.py`.
In development, Vite proxies every `/api/*` request to `http://127.0.0.1:8000`
and strips the `/api` prefix (see `vite.config.js`). Start the backend first,
using the setup in the [root README](../README.md), or API calls will fail.
The backend's `ALLOWED_ORIGINS` must include the dev server origin,
`http://localhost:5173`.

## Commands

Run from this directory:

```
npm install        # once
npm run dev        # dev server on http://localhost:5173
npm run build      # production build into dist/
npm run preview    # serve the production build locally
npm run lint       # oxlint
npm test           # vitest (run once)
```

## Layout

- `src/api/`: one module per backend area (chat, gwSelection, transfers, ...)
- `src/pages/`, `src/components/`, `src/layout/`: screens and UI pieces
- `src/hooks/`, `src/auth/`, `src/config/`, `src/data/`: shared logic and helpers

UI mockups live in [`../docs/stitch_mockups/`](../docs/stitch_mockups/).
