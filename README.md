# Weather Risk Agent

Answers weather-disruption questions for 10 fixed US logistics hubs. Python computes every score. A language model can only parse intent and draft wording, and only when `OPENAI_API_KEY` is set.

## Run

```bash
cd weather-risk-agent
cp .env.example .env
# optional: add OPENAI_API_KEY
make setup
make seed
make dev
```

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
- `GET /api/compare?hub_ids=miami,houston&hazards=hurricane,flood`
- `POST /api/admin/refresh-scores`

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
- No deployment and no score-history chart in this version.
