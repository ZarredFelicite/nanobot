# nanobot webui

SvelteKit frontend for browsing nanobot sessions and chatting through the existing OpenCode HTTP+SSE API.

## Run

1. Start nanobot with the OpenCode channel enabled.
2. From `webui/`, install dependencies with `npm install`.
3. For local frontend development, run `npm run dev`.
4. To serve the UI from the nanobot gateway, run `npm run build` and then start nanobot.

The dev server proxies API and SSE requests to `http://127.0.0.1:4096` by default.
Set `NANOBOT_BASE_URL` if your OpenCode channel listens elsewhere.

## Current scope

- session list with status badges
- live chat history via SSE
- message sending through the OpenCode API
- mobile-friendly single-page layout
- static build that can be served directly by nanobot's OpenCode gateway
