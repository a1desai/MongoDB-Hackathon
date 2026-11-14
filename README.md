# MongoDB-Hackathon

**Agents you can audit.** An AI agent that takes real actions across systems, using MongoDB Atlas as its identity, policy and audit layer. Every action is tied to an identity, checked against policy before it runs, and recorded so a reviewer can reconstruct the run afterward.

Built at MongoDB's Agents in Context hackathon, Toronto, November 14, 2026.

## Data model

| Collection | Holds |
| --- | --- |
| `identities` | Humans, agents and services. Every agent names an accountable human (`ownerId`). |
| `policies` | Versioned allow, deny and require-approval rules, by identity or role and by action. |
| `runs` | One agent session. Setting `status: "stop_requested"` stops the run. |
| `audit_log` | Append-only events ordered by `(runId, seq)`: proposed actions, policy decisions, results. |

Validators and indexes are defined in [`src/audit_agent/schema.py`](src/audit_agent/schema.py).

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env    # then fill in MONGODB_URI (and VOYAGE_API_KEY if you have one)
uv run python scripts/setup_db.py
uv run python scripts/smoke_test.py
```

`MONGODB_URI` comes from Atlas: **Cluster0 → Connect → Drivers**, using a database user from **Database Access**. Your IP must be on the project's **Network Access** list.

## AI assistant setup (Claude Code)

This repo enables the official [MongoDB Atlas plugin](https://github.com/mongodb/agent-skills/tree/main/plugins/mongodb-atlas) for Claude Code. It bundles the [MongoDB MCP Server](https://www.mongodb.com/docs/mcp-server/get-started/) (Atlas Managed, sign in with OAuth) and the [MongoDB agent skills](https://www.mongodb.com/docs/agent-skills/).

### Prerequisites

- A MongoDB Atlas account with a role that can reach cluster data, such as Project Owner or Project Data Access Read/Write.
- AI client access turned on for the Atlas organization. An Organization Owner enables it in **Organization Settings → App Connections** and picks **Read and write**. See [Manage AI Client Access](https://www.mongodb.com/docs/mcp-server/remote-mcp/manage-ai-client-access/).

### First run

1. Open Claude Code in this repo and trust the folder. If you aren't prompted to install the plugin, run `/plugin install mongodb-atlas@claude-plugins-official`.
2. Run `/reload-plugins`.
3. Run `/mcp`, select `plugin:mongodb-atlas:mongodb-atlas`, choose **Authenticate**, and approve the request in your browser.

The sign-in expires after 7 days of inactivity or 30 days after you first grant it. Repeat step 3 when that happens.

### Asking for queries

The `mongodb-natural-language-querying` skill reads the collection's schema, indexes and sample documents through the MCP server before it writes a query. You get better queries when your request includes:

- **Where**: the database and collection.
- **What**: the filter conditions, sort order and limit.
- **Output shape**: which fields to return, and any grouping or computed values.
- **Format**: mongosh, or the driver and language the code will run in.
- **Dates**: absolute dates ("since 2026-11-01") rather than "last week".

Example:

> In `agent_audit.audit_log`, find every `policy.decision` event with `decision.effect: "deny"` since 2026-11-14T12:00Z, newest first. Return only `runId`, `actorId`, `ts` and `decision.reason`, as a PyMongo query.

Background: MongoDB's [Natural Language to MongoDB Queries](https://www.mongodb.com/docs/manual/natural-language-to-mongodb/) guide.
