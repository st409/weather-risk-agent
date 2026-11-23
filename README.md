# Weather Risk Agent

Answers weather-disruption questions for 10 fixed US logistics hubs. Python computes every score. A language model can only parse intent and draft wording, and only when `OPENAI_API_KEY` is set.

Live:

- Chat UI: https://weather-risk-agent-dsev.onrender.com/
- API: https://weather-risk-api.onrender.com (health check at `/api/health`)

Both run on Render's free plan. The first request after 15 idle minutes waits about a minute while the instance wakes.

Design notes are in [DESIGN.md](DESIGN.md). The AI-assisted build is indexed in [AI_SESSION.md](AI_SESSION.md).

About 5,200 lines of application code, plus about 3,500 lines of tests and evals, and about 1,400 lines of frontend. Counts skip blank lines.

## Features

Voice: mic input and optional read-aloud via the browser Web Speech API (Chrome/Edge/Safari).

## Run

Prerequisites: Python 3.10 or newer, and Node.js with npm (for the frontend).

```bash
cd weather-risk-agent
cp .env.example .env
# optional: add OPENAI_API_KEY
make setup
make seed
make dev
```

- `make setup` creates `backend/.venv`, installs the backend, and runs `npm install` in `frontend/`.
- `make seed` loads the 10 hubs (and their FEMA baselines) into the SQLite database at `backend/data/weather_risk.db`.
- `make dev` starts the API on port 8000 and the UI on port 5173 together. Ctrl+C stops both.

Open http://127.0.0.1:5173

On Windows, if `make` is not installed:

```bash
python scripts/tasks.py setup
python scripts/tasks.py seed
python scripts/tasks.py dev
```

`python` must be 3.10 or newer. The setup script will look for 3.10+ via the `py` launcher if the default interpreter is older.

## Environment

| Variable | Required | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | No | Drafts prose. Without it, the reply is still built from the scores. |
| `OPENAI_MODEL` | No | Default `gpt-4o-mini`. |
| `WEATHER_DATA_MODE` | No | `live` (default) or `fixture` for tests. |
| `NWS_USER_AGENT` | No | Required by the National Weather Service. A default is set. |
| `DATABASE_PATH` | No | SQLite file. Default `backend/data/weather_risk.db`. |
| `ALLOWED_ORIGINS` | No | Extra browser origins for CORS, comma-separated. Local dev is always allowed. |
| `ADMIN_TOKEN` | No | Secret for `POST /api/admin/refresh-scores`, sent as `X-Admin-Token`. Empty means that route returns 404. |
| `CHAT_RATE_LIMIT_PER_MIN` | No | Per-IP cap on `POST /api/chat`. Default 30. Set to 0 to turn it off. |
| `VITE_API_URL` | No | Frontend only. API origin in production. Empty in local dev, so Vite proxies `/api`. |

Copy `.env.example` to `.env`. Do not commit `.env`.

## Example questions

- Which hubs in the Midwest are most exposed to winter disruption?
- Compare Miami and Houston in terms of hurricane and flood exposure.
- What percentage of days in Denver last year had snowfall?
- Why is the Dallas hub's weather disruption risk high?
- What about floods only?

The last one reuses the hubs from the previous turn.

## Other endpoints

The UI uses `POST /api/chat`. These do not go through the model:

- `GET /api/hubs`
- `GET /api/hubs/dallas/risk`
- `GET /api/compare?hub_ids=miami,houston&hazards=hurricane,flood` ranks those hubs on the mean of the named hazards.
- `POST /api/admin/refresh-scores` with header `X-Admin-Token`. Chat messages are limited to 500 characters.

## Tests and evals

```bash
make test
make eval
make score-once
```

`make test` and `make eval` use fixture weather and do not call a model. `make score-once` uses live Open-Meteo and NWS unless `WEATHER_DATA_MODE=fixture`.

## Limitations

- Ten hubs only. Unknown cities get one clarifying question.
- Scores are 0–100 indexes, not probabilities and not a forecast for a shipment.
- Historical days come from the Open-Meteo archive. A snow day is daily snowfall of at least 1 cm.
- NWS alerts are whatever is active now. A zero is a quiet moment, not a quiet year.
- FEMA baselines are a county snapshot retrieved 2026-11-23. Large counties often score near 100 because the index includes expected loss.
- Hurricane surge modifier is on only for Miami, Houston, and New York/Newark.
- Wildfire is not a scored hazard.
- No score-history chart in this version.

## Deploy

The chat UI is live at https://weather-risk-agent-dsev.onrender.com/ and the API at https://weather-risk-api.onrender.com, both on Render. The page can also be hosted on Vercel and pointed at the API. No custom domain is required. The free Render service sleeps after 15 minutes without a request, and the next request takes about a minute to wake it.

### Render

1. In Render, create a Blueprint from this repo, or a Python web service with root directory `backend`.
2. Build command: `pip install -r requirements.txt`
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Health check path: `/api/health`
5. Set environment variables:
   - `OPENAI_API_KEY`: your key, if you want the model to draft prose. Leave it empty and answers still come from the scores.
   - `ALLOWED_ORIGINS`: the UI origin, for example `https://weather-risk-agent-dsev.onrender.com` (or the Vercel URL if the page is hosted there). No trailing slash.

`render.yaml` records the same service settings. The committed `backend/data/weather_risk.db` is what the free server starts with. Chat rows written after deploy can disappear when the free instance sleeps or restarts.

### Vercel

1. Import the GitHub repo. Set the root directory to `frontend`. Framework preset: Vite.
2. Set `VITE_API_URL` to the API origin, `https://weather-risk-api.onrender.com`. No trailing slash.
3. On the API service, set `ALLOWED_ORIGINS` to the UI origin (the `*.vercel.app` URL).
4. Deploy. The site is served at a `*.vercel.app` address.

Locally, leave `VITE_API_URL` unset. The dev server still proxies `/api` to `http://127.0.0.1:8000`.
