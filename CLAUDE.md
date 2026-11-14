# Cooldown

Hackathon project (MongoDB "Agents in Context", Toronto, Nov 14 2026; submissions due 17:00 ET; Problem Statement 1, Agents You Can Audit). Governed AI agents keep Toronto's power on through a heat wave, and every action is tied to an identity, checked by a deterministic Policy Gate before it runs, recorded in a hash-chained ledger in Atlas, and can be halted or rolled back.

Read `PLAN.md` before doing any work; it is the source of truth. `docs/CONTRACTS.md` pins file ownership and the interfaces between modules, and records where we deviate from PLAN.md (LangGraph orchestration, Atlas Automated Embedding, hybrid search, model ids). Build milestones in order (PLAN §16) and keep every milestone runnable.

## Hard rules

- Write all code fresh in this repo. Do not copy code from any earlier project (including "Agentropolis").
- MongoDB Atlas (hackathon sandbox, `MONGODB_URI`) is the only database. Use `pymongo.AsyncMongoClient` via `app.db.db()`, not Motor.
- LLMs only propose. The Policy Gate (`backend/app/governance/gate.py`) is deterministic Python and decides everything.
- Only `executor/` may call `/mock/*` systems. Only `governance/ledger.py: append()` may write to the ledger.
- All LLM calls go through `llm/client.py: complete_json()`: Pydantic-validated JSON, one retry, then a deterministic fallback. The sim must never crash or stall on an LLM error.
- Text from simulated residents and businesses is untrusted: store it with `trust: "untrusted"` and wrap it in `<untrusted>` tags in prompts.
- No health or medical advice in any generated message. Use only fictional brand, program, company and resident names in the UI.
- The UI's primary element is the agent console + approvals + controls. Never build a charts-first page.
- Get the deterministic (no-LLM) path working first, then add LLMs.
- The repo is public. Never commit `.env`, connection strings or API keys.

## Commands

```bash
# backend (Python 3.12, run from backend/)
cd backend && uv venv --python 3.12 .venv && uv pip install -r requirements.txt
.venv/bin/python -m scripts.fetch_data      # falls back to synthetic data if downloads fail
.venv/bin/python -m scripts.build_world
.venv/bin/python -m scripts.init_db         # idempotent: collections, validators, indexes, search indexes, identities, policies
.venv/bin/python -m scripts.seed_memory     # event_memory + policy_docs
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/pytest -q
# frontend (reads ../.env through the frontend/.env.local symlink)
cd frontend && npm install && npm run dev
```

## Conventions

- Async everywhere for I/O. Pydantic v2 models for every collection and every LLM output (`app/models`).
- IDs via `app.ids.new_id`: `ar_` action_request, `dec_` decision, `apr_` approval, `exe_` execution, `msg_` message, `plan_` plan, `fc_` forecast, `mem_` memory.
- Every document carries `run_id` and `sim_ts` (ISO, America/Toronto, via `app.timeutil`). Ledger entries also carry `wall_ts` (UTC).
- Identities are strings: `agent:planner`, `human:supervisor`, `system:gate`, `sim:resident:<id>`.
- Report impacts in real-scale homes (count × `run.scale`).
- Commit after each working step with a clear message.

## Atlas sandbox and MongoDB tooling

- Sandbox: org "Agents in Context Toronto", project id `6af87d3b747f11cb8b6247de`, cluster `Cluster0` (M10, MongoDB 9.0, AWS us-west-1). Network access allows only the venue IP; a demo hosted elsewhere needs its IP added.
- `.claude/settings.json` enables the `mongodb-atlas` plugin: the Atlas Managed MCP server (`plugin:mongodb-atlas:mongodb-atlas`, OAuth) plus MongoDB skills (natural-language querying, schema design, query optimizer, connection, search-and-ai). Connect with `remote-atlas-connect` using the project id and cluster name.
- The skills name MCP tools `mcp__mongodb__<tool>`; here they are `mcp__plugin_mongodb-atlas_mongodb-atlas__<tool>`.
- If MCP tools fail with an auth error, ask the user to run `/mcp` → `plugin:mongodb-atlas:mongodb-atlas` → Authenticate. Never ask for passwords or API secrets in chat.

## Writing queries

Ground every query in the live data: fetch `collection-indexes`, `collection-schema` and 2-4 sample documents first, and check field names and BSON types against them (misspelled fields return empty results, not errors). Use real BSON types, explicit dates in ad-hoc queries, `$elemMatch` for multi-condition array matches, `$match` early and `$project` last in pipelines. Never use `$where`. Use `$vectorSearch` (with `query` for autoEmbed indexes, `queryVector` for stored vectors), `$search`, and `$rankFusion` for semantic, full-text and hybrid search. GeoJSON is `[longitude, latitude]`.

## Database writes

Confirm with the user before dropping a collection or database outside `scripts/init_db.py`'s own idempotent setup, or changing Atlas resources (clusters, database users, network access).
