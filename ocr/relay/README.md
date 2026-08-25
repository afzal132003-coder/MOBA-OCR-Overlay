# Cloud Relay — Deploy Guide

This is the piece that lets the OCR engine, admin dashboard, and overlay
pages all reach the same live match state from different PCs/networks —
not just `localhost`. It's a small always-on WebSocket relay.

**It does not go on Vercel.** Vercel (and most static hosts) can't run a
long-lived WebSocket server. This needs a platform that runs a persistent
process. [Railway](https://railway.app) is the easiest fit and has a free
starter tier — these steps use it, but Render or Fly.io work the same way.

## 1. Deploy `server.py`

1. Push this repo to GitHub if it isn't already (`git push`).
2. On [railway.app](https://railway.app), sign in, **New Project → Deploy
   from GitHub repo**, pick this repo.
3. Railway will try to build the whole repo — tell it to only look at this
   folder: in the new service's **Settings → Source**, set **Root
   Directory** to `ocr/relay`.
4. It'll detect `requirements.txt` and `Procfile` automatically and run
   `python server.py`.
5. In **Settings → Networking**, click **Generate Domain** — this gives you
   a public URL like `your-relay-name.up.railway.app`. Your relay's
   WebSocket URL is `wss://your-relay-name.up.railway.app` (note **wss**,
   not **ws** — required since it's crossing the public internet, and
   required for pages served over **https** to be allowed to connect at
   all).

## 2. Set your three access tokens

In the Railway service's **Variables** tab, add:

| Variable | Value | Who gets it |
|---|---|---|
| `OCR_TOKEN` | any long random string | whoever's running `ocr_engine.py` that day |
| `ADMIN_TOKEN` | a different long random string | anyone doing edits in `dashboard.html` |
| `VIEWER_TOKEN` | a different long random string | whoever's setting up the OBS/vMix browser sources |

Generate them any way you like — e.g. in a terminal:
`python -c "import secrets; print(secrets.token_urlsafe(24))"`

Treat these like passwords. Anyone with `ADMIN_TOKEN` can edit everything;
anyone with `OCR_TOKEN` can push live stats; `VIEWER_TOKEN` is read-only.
Rotate one by changing its value here and updating whoever uses it — no
redeploy needed, it's read from the environment on each connection.

## 3. Point the OCR engine at it

On whichever PC is running OCR that day, edit `ocr/config.json`:

```json
"relay": {
  "enabled": true,
  "url": "wss://your-relay-name.up.railway.app",
  "token": "the OCR_TOKEN value from step 2"
}
```

Restart `ocr_engine.py` (or run `start.bat` again). It keeps running its
normal local server too (`ws://localhost:8765` still works if you're
testing on that same PC) — the relay connection is purely additive. Look
for `Connected to cloud relay at wss://...` in its console output to
confirm.

## 4. Point the dashboard at it

Anyone doing admin edits opens:

```
https://<wherever dashboard.html is hosted>/dashboard.html?relay=wss://your-relay-name.up.railway.app
```

The first time, it'll prompt for the admin token — paste the `ADMIN_TOKEN`
value. It's remembered in that browser after that (open it plain, without
the `?relay=...` part, on future visits). To change it later, or switch a
browser between local and relay mode, use the **change** link next to the
connection status at the top of the dashboard.

## 5. Point the three overlay pages at it (vMix/OBS)

On the production PC, add each as a Browser Source with the token baked
right into the URL (no prompt needed — nobody's there to answer it):

```
https://<wherever overlay pages are hosted>/overlay/overlay.html?relay=wss://your-relay-name.up.railway.app&token=<VIEWER_TOKEN>
https://<wherever overlay pages are hosted>/overlay/prematch.html?relay=wss://your-relay-name.up.railway.app&token=<VIEWER_TOKEN>
https://<wherever overlay pages are hosted>/overlay/postmatch.html?relay=wss://your-relay-name.up.railway.app&token=<VIEWER_TOKEN>
```

Set once per source; it's remembered in that browser source's local
storage afterward too, but there's no harm leaving the full URL in place.

## Hosting the static pages themselves

`dashboard.html` and the three overlay pages are plain static files — any
static host works (Vercel, Netlify, GitHub Pages). If you already have a
Vercel project connected to this GitHub repo, it's almost certainly
already serving these — check your Vercel dashboard for the deployment
URL, then use that as the `<wherever ... is hosted>` base above.

## Notes

- **Free-tier sleep**: some Railway/Render free tiers spin a service down
  after a period of no traffic, then take a few seconds to wake back up
  on the next connection. If that first-connection delay matters for a
  live show, use a paid always-on tier, or a platform without that
  behavior (Fly.io's free allowance doesn't sleep the same way).
- **The relay holds no permanent history** — only the single most recent
  state snapshot, kept in memory so a client joining mid-match isn't
  blank. If the relay process restarts, that snapshot is gone until the
  OCR engine's next update — but `ocr_engine.py` still persists everything
  to its own local `state.json` regardless, so nothing is actually lost.
- **This relay doesn't understand your data** — it just checks the token
  and forwards messages. All the actual logic (merging state, OCR,
  calibration, turtle timing) is still entirely in `ocr_engine.py`, same
  as before.
