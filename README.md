# ERPNext CRM MCP server

An MCP server that gives Claude sales insights from ERPNext: pipeline, win/loss, lead funnel, follow-ups, stalled deals, account health and bookings. It is built with [FastMCP](https://gofastmcp.com) for hosting on Prefect Horizon and use as a claude.ai connector.

## Tools

| Tool | What it answers |
|---|---|
| `crm_overview` | Headline KPIs: open/weighted pipeline, win rate, bookings, overdue follow-ups, stalled deals |
| `pipeline` | Open pipeline by rep, stage, territory, industry, type, segment or source |
| `win_loss` | Win rate and won/lost value per group, top lost reasons, competitors lost to |
| `lead_funnel` | Lead status mix and conversion by source, territory, industry or owner |
| `follow_ups` | Overdue / upcoming follow-up tasks, per rep |
| `stalled_deals` | Open deals past their expected close date |
| `account_health` | Last touch per customer; accounts with no contact in N days |
| `bookings` | Submitted sales orders by month, customer, sales person, territory or segment |
| `search_records`, `get_record` | Generic read access to CRM/sales doctypes |
| `create_follow_up`, `add_note` | Write tools; only available when `ERP_ALLOW_WRITES=true` |

## Configuration

| Variable | Value |
|---|---|
| `ERP_URL` | `https://eclerxdemo.m.frappe.cloud` |
| `ERP_API_KEY` / `ERP_API_SECRET` | API key pair of the ERPNext user the server acts as |
| `ERP_ALLOW_WRITES` | `true` to enable the write tools (default off) |

Every Claude user of the connector acts as this one ERPNext user. Use a dedicated user that holds only the roles it needs (e.g. Sales Manager, read-only).

## Run locally

```bash
pip install -r requirements.txt
ERP_URL=... ERP_API_KEY=... ERP_API_SECRET=... python server.py   # http://localhost:8000/mcp
```

## Deploy to Prefect Horizon

1. Push this folder to a GitHub repository (private is fine).
2. In Horizon (workspace `mcp-demo-v1`), create a new server from that repository.
   - Entrypoint: `server.py:mcp`
   - Environment variables: `ERP_URL`, `ERP_API_KEY`, `ERP_API_SECRET` (mark the key and secret as secret)
   - Keep authentication **enabled**: without it, anyone with the URL can read your CRM.
3. Deploy. Horizon shows the server URL, ending in `/mcp`.

## Connect from claude.ai

Settings → Connectors → **Add custom connector** → paste the Horizon server URL → Add. Then sign in when prompted, which is Horizon's auth. Enable the connector in a chat via the tools menu and ask e.g. "Which reps have the most overdue follow-ups?"
