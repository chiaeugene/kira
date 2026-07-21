# frontend-future

Assets staged for the **Phase-1 product front-end** (React + shadcn + Tailwind +
TypeScript) — the "Kira Cloud" web app that replaces the Streamlit pilot console.

- `text-scramble.tsx` — animated hero headline component (needs `npm i motion`).
  Copy into `/components/ui/` once the shadcn project exists.

The current pilot is Streamlit; it ports the same scramble effect in
`kira/ui.py` (`_headline_iframe`), so nothing here is wired into the running app
yet. Preserved so the effect can be reused verbatim when we build the real
web front-end.
