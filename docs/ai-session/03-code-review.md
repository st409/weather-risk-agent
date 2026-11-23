# Comprehensive code review
_Exported on 11/23/2026 at 20:13:25 GMT+2 from Cursor (3.31.15)_

---

**User**

/code-review 

Perform a comprehensive code review of this project.

Do not modify any files yet.

Review the entire codebase, focusing on:

1. Architecture and code organization.
2. Backend logic and API endpoints.
3. SQLite database design, queries, and data persistence.
4. Weather data collection and processing.
5. Risk scoring calculations and their correctness.
6. AI agent implementation, reasoning, and use of retrieved data.
7. Error handling and edge cases.
8. Security vulnerabilities and data protection.
9. Performance and scalability.

For every issue you identify:
- Specify the exact file and line number.
- Explain the problem in simple terms.
- Explain why it matters.
- Assign severity: Critical, High, Medium, or Low.
- Suggest a concrete fix.

Distinguish between actual bugs and optional improvements.

Do not assume functionality that is not implemented in the code.
Do not modify files. First provide a prioritized review report.
בנוסף תבדוק שהקוד הוא מינמלי ואין קוד מיותר

---

**Cursor**

אתחיל בסקירת מבנה הפרויקט.

Oops, that picked up the venv. Let me get line counts of project files only.

Quoting issues in PowerShell; I'll use temp script files instead.

# Code review: Weather Risk Agent

I changed no project files. The 72 backend tests pass. I checked the suspicious behaviour with throwaway scripts against a temp DB in fixture mode, and I mark those findings "verified". "Bug" means the behaviour is wrong today or will be soon. "Improvement" means optional.

Two things stand out. `composer.py` is 2,941 lines, about half of the 5,550 lines of app code, and several hundred of them are dead or never run in production. Year labels are also hard-coded and will start returning wrong answers on 1 Jan 2027.

No Critical issues. Four High, a set of Medium bugs, then Low.

---

## High

### H1. Hard-coded "2025" and "2026-11-23" while the analysis window is dynamic (Bug)
- **Where:**
  - `composer.py:444, 573, 1920, 1966`: `year = "2025" if window_start.startswith("2025") or "2025" in label …`
  - `composer.py:994` (`WINDOW_NOTICE`), `:1019` (`"2025 weather"`), `:1350, 1362` (methodology text)
  - `composer.py:2609, 2836`: year whitelist `2020 <= value <= 2027`
  - `llm.py:33, 40` (prompt text)
- **Problem:** `windows.py` computes the year from `date.today()`, but the answer text is fixed to 2025.
  - **Wrong today (verified):** the trailing-365 window is 2025-11-17 to 2026-11-16, but the answer says "had 212 freezing days **in 2025**".
  - **Wrong from 1 Jan 2027 (verified by faking the date):** the window becomes 2026, but the Sources line still says "Open-Meteo 2025 weather" and the methodology text says "calendar year 2025".
  - **Draft rejection from 2028:** the model's valid year numbers start getting rejected.
- **Why it matters:** the answers will state the wrong period in about five weeks.
- **Fix:** build year or window wording from the `HubReport.window_label` and `window_start` fields. Build the notice and methodology text from `resolve_window`. Delete the four copies of the `year =` expression. Drop the year whitelist or compute it from the window.

### H2. Unauthenticated admin endpoint (Security)
- **Where:** `api/hubs.py:82-96`, `POST /api/admin/refresh-scores`.
- **Problem:** anyone can call it. It bypasses the caches (`force=True`) and refetches 10 hubs from Open-Meteo and NWS. It also writes 10 DB rows per call.
- **Why it matters:** the service is public on Render. This is trivial abuse, and it can get the server's IP rate-limited or banned upstream.
- **Fix:** require a secret header from an env var, or remove it. `python -m app.cli score-once` already does the same job.

### H3. `/api/chat` has no input limit and no rate limit (Security / cost)
- **Where:** `agent/schemas.py:74-76` (`message: str`), `api/chat.py`, `agent/llm.py:127`.
- **Problem:** the message length is unlimited, and there is no auth or throttling.
  - **Measured:** a 200k-character message took 5.4 s of CPU.
  - **Cost:** with a key set, each call sends the text to OpenAI, up to two calls per turn.
- **Why it matters:** this is a cheap way to burn CPU or OpenAI spend.
- **Fix:** use `message: str = Field(max_length=500)`, add per-IP rate limiting, and set `max_tokens` on the OpenAI calls.

### H4. A real database with user chat history is committed to git (Data protection)
- **Where:** `.gitignore:12` (`!backend/data/weather_risk.db`), `README.md` ("The committed backend/data/weather_risk.db…").
- **Problem:** the DB is tracked and holds 129 sessions and 408 chat messages. It also holds about 1.7 MB of stored answer payloads and 6,850 cached weather rows. The app rewrites it on every run, so the file shows as modified in `git status`.
- **Why it matters:** user questions leave your machine and live in history. The tracked file also causes merge noise.
- **Fix:** ignore all `*.db`. The app already seeds on startup (`seed_reference_data`). If those chats are sensitive, remove the file from history.

---

## Medium

### Bugs

**M1. `/api/compare` ignores `hazards` (verified).**
- **Where:** `api/hubs.py:63-72`.
- **Problem:** the parameter is validated and echoed back, but never passed to scoring. `hazards=heat` and `hazards=flood` return identical scores.
- **Why it matters:** the response claims a focus it did not apply.
- **Fix:** apply it, or remove the parameter.

**M2. Any non-ASCII character is treated as "unsupported language" (verified).**
- **Where:** `session_context.py:570-571` (`[^\x00-\x7F]`).
- **Problem:** "What’s the flood risk in Miami?", "above 95°F" and "Miami — Houston" all get "I currently support English only."
- **Fix:** normalise punctuation first, or only reject non-Latin scripts, for example Hebrew or Arabic ranges.
- **Side effect:** the Hebrew regex branches are unreachable (see the minimality section).

**M3. Fabricated data is presented as fact when the live API fails.**
- **Where:** `risk/weather.py:90-99`, `risk/fixtures.py`.
- **Problem:** if Open-Meteo fails for one hub, hand-written synthetic climatology is mixed into a ranking with real data. The prose says "Denver had 43 snow days". The only disclosure is a Sources/uncertainty note, and it is not per hub. These results are also saved to `risk_scores`; 20 of the 67 stored snapshots are fallback.
- **Fix:** mark that hub "unavailable" and exclude it from the ranking (or return 503). Do not persist fallback results.

**M4. A partial NWS outage skews rankings, and failures are not cached.**
- **Where:** `risk/scoring.py:120-142` and `risk/weather.py:149`.
- **Problem:** when the alert input is missing it is dropped and the weights are rescaled. A hub whose NWS call failed gets a higher score than peers with a real 0 alert value. For example, the winter score is divided by 0.9.
- **Cache gap:** a failed alert call is not cached, so every request retries it.
- **Fix:** add a short negative cache. Handle "unavailable" the same way for every hub, for example as 0 plus a flag.

**M5. Startup runs a legacy migration with network calls.**
- **Where:** `main.py:22`, `risk/engine.py:48-60`, `db/repository.py:238-255` (`"above 0 cm"`).
- **Problem:**
  - Startup re-scores every "stale" window sequentially. That makes live HTTP calls before the app can serve, which can fail Render's health check.
  - If Open-Meteo fails, the code deletes the real snapshot and saves a fixture-based one (`engine.py:57-58`).
  - The migration is for an old snow rule and is no longer needed.
- **Fix:** delete it. If you keep it, make it offline.

**M6. Unbounded DB growth, no indexes, and writes on reads.**
- **Where:** `agent/orchestrator.py:121-122`, `api/hubs.py:54`, `db/models.py:70-100`, `db/repository.py:360-370`.
- **Problem:**
  - A score snapshot row (about 7 KB) is written for every hub on every chat turn.
  - `GET /hubs/{id}/risk` also writes.
  - There is no retention policy and no index on `risk_scores` or `chat_messages`.
  - `chat_messages` stores the full evidence payload and is never read.
- **Fix:** upsert only the latest score per hub and window. Add an index on `risk_scores(hub_id, window_start, window_end)`. Drop `payload_json` from `chat_messages`, or drop the table.

**M7. The `last_winter` window is half-implemented.**
- **Where:** `risk/windows.py:33-37`, `agent/session_context.py:25`, `composer.py:1019, 658`, `risk/scoring.py:64-71, 274`.
- **Problem (verified):**
  - The answer says "45 of 90 … Sources: Open-Meteo 2025 weather" and "(x% of the year)".
  - `_confidence` needs 330 or more valid days, so a 90-day window is always "low".
  - The "Only N days had a snowfall value" warning (< 300) always fires.
  - DESIGN.md says only two windows are supported.
- **Fix:** remove it, or implement it fully.

**M8. Alerts questions are mishandled.**
- **Where:** `session_context.py:238` (`elif alerts and not hub_ids …`) and `orchestrator.py:197`.
- **Problem:**
  - "Tell me the alerts in Chicago" returns a score answer, not alerts (verified).
  - The alerts intent scores all 10 hubs, which fetches a full year of weather for each hub, just to show alerts.
- **Fix:** allow hub ids, and load alerts only.

**M9. Airport-code aliases match ordinary words (verified).**
- **Where:** `risk/hubs.py:59-68` (`den`, `la`, `mia`, `ord`, `atl`).
- **Problem:** "Is the weather in the den nice? la la la" gets answered as Denver vs Los Angeles.
- **Fix:** drop `la` and `den`, and require upper-case codes for the rest.

**M10. The LLM-output guard is weaker than it looks.**
- **Where:** `composer.py:2791-2795`, `:2609`, `:2836`.
- **Problem:**
  - `_draft_rejection` returns `_fact_problem(...)` whenever a skeleton exists, and the skeleton is always set in production. Lines 2796-2840 never run there: the evidence-number check, internal-label check, overclaim check, "mainly" clause check and driver traps.
  - `_fact_numbers` ignores {0,1,2,3,4,5,10,32,40,95,100,365}, so a wrong small count passes.
- **Fix:** keep one validation path and delete the other.

**M11. Possible multi-minute request latency.**
- **Where:** `risk/weather.py:29-33, 259-275`, `agent/llm.py:60-69, 127`.
- **Problem:**
  - Each upstream call allows a 30 s timeout and 2 tries, with no backoff, and it also retries 4xx errors.
  - The OpenAI client allows 45 s with one retry, and there are two schema attempts per call.
  - There is no single-flight protection, so concurrent identical cold requests all hit upstream.
- **Fix:** retry only 5xx and timeouts with backoff, use shorter timeouts, add a per-key lock, and warm the cache in the background.

**M12. A new SQLite connection per call, behind a global lock.**
- **Where:** `db/repository.py:17, 24-45`.
- **Problem:** one ranking chat turn opens 15 connections (measured). Each runs `PRAGMA journal_mode=WAL`. A global `threading.Lock` serialises reads too. `foreign_keys=ON` does nothing because no FKs are declared.
- **Fix:** set WAL once in `init_db`, reuse a connection per request or thread, and drop the global lock for reads.

### Architecture

**M13. `composer.py` is a God module.**
- **Where:** `composer.py`, 165 private functions and 79 regex calls.
- **Problem:** it mixes wording, follow-ups and about 437 lines of regex that repair model output (`_prepare_draft` runs about 10 rewrite steps). Git history shows a new rule patched in after each failure. Most of the final text is the skeleton anyway.
- **Fix (Improvement):**
  - Split it into `skeleton.py`, `followups.py` and `draft_guard.py`.
  - Either let the model write one sentence and validate it, or drop the LLM from the answer path.

**M14. Tangled layering and duplicated sources of truth.**
- **Where:** `db/repository.py:11-13` imports from `risk`; `risk/weather.py:11` imports `repository`; `composer.py:11` imports the private `_unsupported_window`.
- **Problem:** `risk` and `db` depend on each other. `load_daily` has a side effect that deletes scores (`weather.py:89`). Hubs and baselines live in three places: code, DB tables and the CSV.

---

## Low

| # | Where | Issue | Fix |
|---|---|---|---|
| L1 | `composer.py:78, 1354, 1370`, `hazards.py:32, 39` | A hard-coded `2026-11-23` is reported as the retrieval date, and "Weather and FEMA inputs were retrieved 2026-11-23" is false for live weather. | Use real timestamps. |
| L2 | `scoring.py:125` | `_combine` returns score 0.0 when there is no usable input, so "no data" looks like "no risk". | Return `None` and show n/a. |
| L3 | `hazards.py:21-26`, `scoring.py:276-281` | A blank FEMA hurricane cell becomes a real 0 and is not dropped. This contradicts the scoring module's own docstring. | Treat it as not applicable. |
| L4 | `types.py:155` | `focus_score` is an unweighted mean, but `overall` uses fixed weights, so the two are not comparable. | Document it, or use the overall weights. |
| L5 | `weather.py:232, 244-255` | `"test" in event` is a substring match. "Extreme Cold" and "Wind Advisory" are not classified, and Watch and Warning weigh the same. | Match exact event names. |
| L6 | `api/hubs.py:56-60` | `/hubs/{id}/risk` is case-sensitive but `/compare` lowercases ids. `/compare` also accepts 50 duplicate ids. | Normalise ids, de-duplicate and cap the count. |
| L7 | `agent/orchestrator.py:70-76`, `agent/schemas.py` | `rejection_reason` and the "language model request failed (…)" note are returned to the client. | Log only, or show a generic note. |
| L8 | `frontend/src/App.tsx:110, 114`, `api.ts:100-111` | `sessionStorage.setItem` is unguarded and stores full payloads. `fetch` has no timeout. Errors show raw JSON from `response.text()`. | Wrap in try/catch, add an `AbortController`, show `detail`. |
| L9 | `pyproject.toml`, `requirements.txt` | No lockfile and unpinned dependencies. The two files duplicate each other, and `httpx` is listed twice. | Pin or lock; use one source. |
| L10 | `main.py:39` | The root endpoint has a hard-coded UI URL. `/docs` and the admin route are public. | Remove or gate. |
| L11 | `api/hubs.py:41`, `db/repository.py:61-88` | `business_importance_weight` is stored and exposed but never used. | Remove it. |
| L12 | negation in `session_context._negated_hazards` | "Rank ignoring winter, flood, hurricane, heat and severe storms" produced a ranking over all hazards (verified). There is no check that at least one hazard remains. | Clarify when nothing is left. |
| L13 | `tests/test_scoring.py:184` | The assertion `miami.flood == 99.71 or … or >= 99` is vacuous. | Assert the real value. |
| L14 | tests | Missing coverage for:<br>- NWS event classification<br>- live-mode failure fallback<br>- cache TTL<br>- the `/compare` hazards parameter<br>- non-ASCII input<br>- the admin endpoint<br>- the LLM path with a mocked OpenAI | Add tests. |

---

## Minimality and unnecessary code

### Dead in `composer.py`: delete (about 290 lines)
I confirmed with a reachability scan from the app entry points that nothing calls these. Tests use some of them (`_draft_is_usable`, `_driver_phrase`), so update those tests.

| Function | Line |
|---|---|
| `_behind_clause` | 397 |
| `_lead_fact` | 414 |
| `_count_fact` | 429 |
| `_climate_gap` | 891 |
| `_compose` | 966 |
| `_hub_count` | 1134 |
| `_rank_verdict` | 1185 |
| `_best_separator` | 1275 |
| `_driver_value` | 1487 |
| `_baseline_sentence` | 1495 |
| `_separator_sentence` | 1515 |
| `_hub_line` | 1538 |
| `_reason_driver` | 1555 |
| `_driver_words` | 1587 |
| `_why_sentence` | 2088 |
| `_driver_phrase` | 2134 |
| `_assumption_sentence` | 2161 |
| `_definition` | 2172 |
| `_uncertainty_sentence` | 2189 |
| `_compact_sources` | 2203 |
| `_score_tokens` | 2279 |
| `_numbers_in` | 2289 |
| `_ranking_score_numbers` | 2293 |
| `_draft_is_usable` | 2779 |
| `_source_sentence` | 2903 |
| `NRI_NOTE` (constant) | 17 |

### Unreachable in production (see M10): about 107 lines
- `_internal_label_problem`, `_answer_shape_problem`, `_mainly_clause_problem`, `_collect_numbers`, `_driver_score_traps`, `_required_driver_phrase`, `_driver_point_phrase`.
- The matching evidence keys `required_driver_phrase` and `driver_score_traps` (`composer.py:~154-155`).

### Unused by the UI (decide whether the API keeps them)
- The frontend renders only `answer`, `assumptions`, `uncertainties`, `sources` and the driver and score tables. It never uses `scores`, `key_findings`, `follow_up_questions` or `evidence.metric`.
- The follow-up generator (`_followups_for`, `_filter_followups`, `_repeats_question`, `_mentions_unrelated_hub`, `_peer`) is about 81 lines. DESIGN.md claims follow-ups render, but they don't.
- `getHealth` and the `Health` type in `api.ts` are unused.

### Backend and DB leftovers
- **Unread functions:** `latest_score` (`repository.py:336`, used only by tests) and `replace_window_note` (`:218`) are never called.
- **Legacy migration:** `stale_score_windows` (`repository.py:238`) and `recompute_stale_scores` (`engine.py:48-60`) are the one-off migration from M5.
- **Redundant init:** `init_db()` is called twice at startup (`main.py:20` and `repository.py:54`). `PRAGMA foreign_keys` is a no-op.
- **Tables and columns never read:**
  - `hazard_baselines` is written (`repository.py:92-132`) but the scorer reads the CSV.
  - The five per-hazard score columns in `risk_scores` (`models.py:78-82`) are never queried.
  - `chat_sessions` and `chat_messages` are written but never read.
  - The `hubs` table only mirrors `HUBS`.

### Model schema and prompt
- **Never consumed:** `IntentResult.hub_ids`, `region`, `needs_clarification` and `clarifying_question` (`schemas.py:30-36, 85-103`). `_merge` uses only `intent`, `hazards`, `metric` and `time_window`.
- **Always empty:** `AnswerDraft.follow_up_questions` (`schemas.py:59-60`). The prompt even says to return an empty list.
- **Unused return value:** `schema_retries` returns an attempt count that nothing uses (`llm.py:60-69, 157`).

### Duplication
- `OVERALL_WEIGHTS` is defined twice (`composer.py:33`, `scoring.py:37`).
- `METRIC_HAZARD` and `_METRIC_HAZARD` are the same dict (`composer.py:94`, `session_context.py:40`).
- The hazard list is written out in at least five places: `api/hubs.py:68`, `session_context.py` (`HAZARDS`), `scoring.py:45`, `composer.py` (`HAZARD_LABEL`) and the tuple at `composer.py:756`.
- `("los angeles", "losangeles")` appears twice in `ALIASES` (`hubs.py:45, 48`).
- `orchestrator.py:124-125` copies `fallback` and `rejection_reason`, which `answer.model_dump()` already contains.
- `_pack` is only an alias of `_base` (`composer.py:706`).
- `_name_list` has identical branches (`composer.py:2910`).
- `_one_decimal` adds `+ 0.0` for no reason (`composer.py:160`).
- `_ranking_row` repeats the same bucket lookup (`composer.py:855`).
- `_short_separator` and `_best_separator` are near-duplicates.
- `score_selection` returns a `_span` string that no caller uses (`engine.py:63-73`).
- `parse_message` runs twice per turn (`orchestrator.py:41, 131`).
- `_to_f`, `_to_inches` and `_to_mph` convert units the request already pins (`weather.py:289-325`). Keep `_to_cm`, because snowfall comes back in inches.

### Dead Hebrew branches
- `session_context.py:27-31, 206, 751, 753`, plus the Hebrew example in the prompt (`llm.py:28`) and in tests.
- M2 rejects non-ASCII input before any of these run.

### Scripts and repo clutter
- `scripts/dev.py:47`: `signal.CTRL_BREAK_EVENT if False else signal.SIGTERM` is a constant-false branch.
- `cli.py seed` duplicates the startup seeding. `score-once` duplicates the admin endpoint.
- `qa-r5/` has 35 untracked QA JSON files, and `data/_probe*.db` sit at the repo root.
- **Stale docs:**
  - DESIGN.md says "no hosted deployment" and describes an answer JSON that no longer exists. The real schema is `answer` plus `follow_up_questions`.
  - README says "about 4,900 lines"; the real count is about 5,550.
  - `AI_SESSION.md` shows prompts that differ from `llm.py`.

---

## What is solid
- All six formulas match DESIGN.md, and every weight set sums to 1. Tests lock this.
- The model cannot inject scores. `AnswerDraft` forbids extra fields, and model-supplied `hub_ids` and `region` are ignored.
- All SQL is parameterised, so there is no SQL injection.
- Session IDs are validated. React escapes all output, so there is no XSS. `.env` is not tracked.
- If a model draft fails validation, the deterministic answer is returned.

## Suggested order
1. H1 (year labels), then H2 and H3 (admin endpoint, input limits and rate limits), then H4 (the committed DB).
2. M1, M2, M3, M5 and M8.
3. Delete the dead code listed above, then split `composer.py`.

בקצרה, על הדרישה לקוד מינימלי: יש כ-400 שורות קוד מת או שלא רץ בפרודקשן ב-`composer.py`, וטבלאות ועמודות ב-DB שנכתבות ולא נקראות. שדות שהמודל מחזיר ואף אחד לא משתמש בהם. כפילויות של קבועים. וענפי עברית שלא ניתן להגיע אליהם. הפירוט המלא בסעיף "Minimality".

---

**User**

<system_notification>
The following task has finished. If you were already aware, ignore this notification and do not restate prior responses.

<task>
kind: shell
status: error
task_id: 432310
title: Get-ChildItem -Recurse -File -Include *.py,*.ts,*.tsx,*.json,*.toml,*.md,Makefile,*.example,.gitignore,*.csv -Path . | Where-Object { $_.FullName -notmatch 'node_modules|__pycache__|\.pytest_cache|qa-r5|dist\\' } | ForEach-Object { "{0,6} {1}" -f (Get-Content $_.FullName | Measure-Object -Line).Lines, $_.FullName.Replace((Get-Location).Path,'') }; Get-ChildItem -Directory | Select Name
tool_call_id: toolu_01CGr4dS5uGZpaNzdjrQZcpH
detail: exit_code=4294967295
output_path: C:\Users\PC\.cursor\projects\c-Users-PC-weather-risk-agent\terminals\432310.txt
</task>
</system_notification>
<user_query>Briefly inform the user about the task result and perform any follow-up actions (if needed).</user_query>

---

**Cursor**

That background task was my first file-listing command. It accidentally scanned `backend\.venv`, so I stopped it. It ended with an error exit code because I killed it, not because anything failed.

It has no effect on the review. I re-ran the listing excluding the virtualenv, and the review is based on that. I made no changes to your project files, so there is nothing to follow up on.

---

**User**

Apply the code-review fixes below (round 1 of 2). Don't change scoring formulas/weights, answer wording rules, or .env. Don't split composer.py. Keep pytest and evals green; the 4 assignment questions must answer the same.

High:
- H1: remove hard-coded "2025"/"2026-11-23". Derive year/window wording, Sources label, window notice, methodology text and year whitelist from the resolved window. Trailing-365 answers say "in the last 365 days", not "in 2025". Use real retrieval timestamps (L1).
- H2: protect POST /api/admin/refresh-scores with header X-Admin-Token == env ADMIN_TOKEN; if unset, return 404. Add empty ADMIN_TOKEN to .env.example.
- H3: ChatRequest.message Field(min_length=1, max_length=500); set max_tokens on OpenAI calls. Add a simple per-IP rate limit on /api/chat (e.g. 30/min, in-memory).
- H4: stop tracking chat data: clear chat_sessions, chat_messages, session_context rows from backend/data/weather_risk.db and VACUUM (keep hubs, FEMA, observations, alerts, scores). Remove untracked junk (qa-r5/, data/_probe*.db) and ignore them.

Medium:
- M1: /api/compare must apply `hazards`, or drop the param.
- M2: don't treat non-ASCII as non-English; normalize curly quotes/dashes/° first, reject only non-Latin scripts (Hebrew/Arabic etc).
- M3: if live weather fails for a hub, mark it unavailable and exclude from ranking (say so in the answer); never persist fixture-based scores in live mode.
- M5: remove the startup legacy snow migration (recompute_stale_scores / stale_score_windows).
- M8: "alerts in Chicago" returns Chicago alerts; alerts intent loads alerts only, no full-year weather.
- M9: drop "la"/"den" aliases; other airport codes only when upper-case.
- L7: don't return rejection_reason or raw LLM error text to the client (log only).

Cleanup: delete the dead functions listed in the review (composer.py table + unreachable validator branch per M10, keeping one validation path), duplicate constants (OVERALL_WEIGHTS, METRIC_HAZARD, duplicate "los angeles" alias), unused repository functions, dead Hebrew branches, the constant-false branch in scripts/dev.py. Fix the vacuous test (L13). Update README line count and stale DESIGN.md lines.

Add tests for: H2, H3, M1, M2, M8, M9.

---

**Cursor**

Now apply the repository edits.

Now llm.py.

Now the top-of-file edits (apply_model_prose, evidence_block, constants, clarification).

Now windows.py helpers.

Simplify that.

---

**User**

תנשיך

---

**Cursor**

ממשיך מאיפה שעצרתי: נרמול אנגלית, הסרת ענפי העברית, וניקוי הכינויים. קודם בודק מה כבר נכנס לקוד.

הקוד של החלון והאבטחה כבר בפנים. עכשיו סוגר את שפה, כינויים, התראות, ואת הקריאות שנשברו.

סבב 1 הושלם. משקלי הניקוד, נוסח התשובות ו-`.env` לא שונו, ו-`composer.py` נשאר קובץ אחד.

pytest: **79 עברו**. evals: **69 עברו**. ארבע שאלות המטלה מחזירות את אותו טקסט כמו לפני השינוי.

מה שנכנס:

- שנה וחלון נגזרים מהחלון שחושב. חלון של 365 יום נכתב "in the last 365 days". חותמות הזמן מגיעות מהמשיכה עצמה ומקובץ ה-FEMA.
- `POST /api/admin/refresh-scores` דורש `X-Admin-Token` ששווה ל-`ADMIN_TOKEN`. בלי הסוד הנתיב מחזיר 404. `ADMIN_TOKEN` הריק נוסף ל-`.env.example`.
- הודעת צ'אט מוגבלת ל-1–500 תווים, לקריאות OpenAI יש תקרת טוקנים, ו-`/api/chat` מוגבל ל-30 בקשות לדקה לכתובת IP (0 מכבה את המגבלה). ב-`render.yaml` נוסף `--proxy-headers` כדי שהמגבלה תראה את כתובת הלקוח מאחורי הפרוקסי.
- `/api/compare` מדרג לפי ממוצע ה-`hazards` שנשלחו.
- מרכאות מסולסלות, מקפים וסימן מעלה נשארים אנגלית. עברית, ערבית ושפות לא-לטיניות נדחות. כינויי `la` ו-`den` הוסרו, וקודי שדה עובדים רק באותיות גדולות.
- כשל במזג אוויר חי מוציא את המוקד מהדירוג ואומר זאת. ציונים שמבוססים על נתוני דמה לא נשמרים במצב חי.
- מיגרציית השלג בהפעלה הוסרה. "alerts in Chicago" מחזיר התראות של שיקגו בלי לטעון שנת מזג אוויר ובלי קריאה למודל.
- סיבת דחיית טיוטה ושגיאת המודל נרשמות בלוג בלבד.
- נמחקו פונקציות מתות, קבועים כפולים וענפי עברית. אין עוד קוד לא נגיש ב-`backend/app`.

`qa-r5/` וקבצי `data/_probe*.db` נמחקו. `qa-final/`, `qa-r4/` ו-`qa-rerun/` נכנסים ל-`.gitignore` ולא נמחקו. ב-`weather_risk.db` טבלאות הצ'אט כבר היו ריקות; הרצתי `VACUUM`. המוקדים, FEMA, התצפיות, ההתראות וצילומי הציונים נשארו. שורות צ'אט חדשות נכתבות שוב בזמן שהאפליקציה רצה, כי השיחה עדיין צריכה את ההקשר של הפנייה הקודמת.

