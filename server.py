"""ERPNext CRM MCP server.

Exposes sales/CRM insights from an ERPNext site (leads, opportunities, customers, follow-ups,
bookings) as MCP tools, for use from Claude (e.g. as a claude.ai custom connector).

Configuration (environment variables):
  ERP_URL           e.g. https://eclerxdemo.m.frappe.cloud
  ERP_API_KEY       API key of the ERPNext user the server acts as
  ERP_API_SECRET    API secret of that user
  ERP_ALLOW_WRITES  "true" to enable the tools that create follow-ups and notes (default: off)

Run locally:  fastmcp run server.py:mcp --transport http --port 8000
"""

import json
import os
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

ERP_URL = os.environ.get("ERP_URL", "").rstrip("/")
ALLOW_WRITES = os.environ.get("ERP_ALLOW_WRITES", "").lower() in ("1", "true", "yes")

# Doctypes the generic search/get tools may read
READABLE_DOCTYPES = {
    "Lead", "Opportunity", "Customer", "Contact", "Address", "Quotation", "Sales Order", "Sales Invoice",
    "ToDo", "Event", "Comment", "Sales Person", "Territory", "Customer Group", "Industry Type",
    "Item", "Competitor", "Opportunity Lost Reason", "Sales Stage", "Prospect", "Campaign",
}
MAX_ROWS = 5000

mcp = FastMCP(
    "ERPNext CRM",
    instructions=(
        "Tools for sales leadership insights from ERPNext CRM: pipeline, win/loss, lead funnel, "
        "follow-ups, stalled deals, account health and bookings. Amounts are in the company currency. "
        "Opportunity status 'Converted' means won. Use search_records for anything the summary tools "
        "don't cover, and include record names so the user can find them in ERPNext."
    ),
)


# --------------------------------------------------------------------------------------
# ERPNext REST helpers
# --------------------------------------------------------------------------------------

def _client() -> httpx.AsyncClient:
    key, secret = os.environ.get("ERP_API_KEY"), os.environ.get("ERP_API_SECRET")
    if not (ERP_URL and key and secret):
        raise ToolError("Server is not configured: set ERP_URL, ERP_API_KEY and ERP_API_SECRET.")
    return httpx.AsyncClient(base_url=ERP_URL, timeout=60,
                             headers={"Authorization": f"token {key}:{secret}", "Accept": "application/json"})


def _error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"ERPNext returned HTTP {resp.status_code}"
    msgs = []
    for m in json.loads(body.get("_server_messages") or "[]"):
        try:
            msgs.append(json.loads(m).get("message", m))
        except (ValueError, AttributeError):
            msgs.append(str(m))
    return f"ERPNext returned HTTP {resp.status_code}: " + ("; ".join(msgs) or body.get("exception", ""))[:400]


async def _request(method: str, path: str, **kw):
    async with _client() as c:
        resp = await c.request(method, path, **kw)
    if not resp.is_success:
        raise ToolError(_error(resp))
    return resp.json()


async def get_list(doctype: str, fields: list[str], filters: list | dict | None = None,
                   order_by: str | None = None, limit: int = MAX_ROWS) -> list[dict]:
    params = {"fields": json.dumps(fields), "limit_page_length": limit}
    if filters:
        params["filters"] = json.dumps(filters)
    if order_by:
        params["order_by"] = order_by
    return (await _request("GET", f"/api/resource/{doctype}", params=params))["data"]


_meta_cache: dict[str, dict] = {}


async def field_names(doctype: str) -> set[str]:
    if doctype not in _meta_cache:
        docs = (await _request("GET", "/api/method/frappe.desk.form.load.getdoctype",
                               params={"doctype": doctype}))["docs"]
        _meta_cache[doctype] = {f["fieldname"] for f in docs[0]["fields"] if f.get("fieldname")}
    return _meta_cache[doctype]


async def source_field(doctype: str) -> str | None:
    names = await field_names(doctype)
    return next((f for f in ("utm_source", "source") if f in names), None)


async def user_names() -> dict[str, str]:
    rows = await get_list("User", ["name", "full_name"])
    return {r["name"]: r["full_name"] or r["name"] for r in rows}


def _round(x: float) -> float:
    return round(x or 0, 2)


def _today() -> date:
    return date.today()


# --------------------------------------------------------------------------------------
# Insight tools
# --------------------------------------------------------------------------------------

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}

OppDimension = Literal["opportunity_owner", "sales_stage", "territory", "industry", "opportunity_type",
                       "customer_group", "source"]


async def _opps(extra_fields=()):
    src = await source_field("Opportunity")
    fields = ["name", "party_name", "customer_name", "status", "sales_stage", "opportunity_amount",
              "probability", "expected_closing", "transaction_date", "territory", "industry",
              "opportunity_type", "customer_group", "opportunity_owner", *extra_fields]
    if src:
        fields.append(src)
    rows = await get_list("Opportunity", fields)
    for r in rows:
        r["source"] = r.get(src) if src else None
    return rows


def _label(row, dim, users):
    v = row.get(dim)
    if dim == "opportunity_owner":
        return users.get(v, v) or "Unassigned"
    return v or "(not set)"


@mcp.tool(annotations=READ_ONLY)
async def crm_overview() -> dict:
    """Headline sales KPIs: open pipeline, weighted pipeline, bookings and win rate (last 12 months),
    lead counts by status, overdue follow-ups and stalled deals."""
    today = _today()
    year_ago = (today - timedelta(days=365)).isoformat()
    opps = await _opps()
    leads = await get_list("Lead", ["status"])
    todos = await get_list("ToDo", ["date"], filters=[["status", "=", "Open"]])
    orders = await get_list("Sales Order", ["base_net_total", "transaction_date"],
                            filters=[["docstatus", "=", 1], ["transaction_date", ">=", year_ago]])
    open_ = [o for o in opps if o["status"] in ("Open", "Quotation", "Replied")]
    won = [o for o in opps if o["status"] == "Converted"]
    lost = [o for o in opps if o["status"] == "Lost"]
    return {
        "as_of": today.isoformat(),
        "open_opportunities": len(open_),
        "open_pipeline_value": _round(sum(o["opportunity_amount"] or 0 for o in open_)),
        "weighted_pipeline_value": _round(sum((o["opportunity_amount"] or 0) * (o["probability"] or 0) / 100
                                              for o in open_)),
        "stalled_deals_past_expected_close": sum(1 for o in open_ if o["expected_closing"]
                                                 and o["expected_closing"] < today.isoformat()),
        "won_deals": len(won), "lost_deals": len(lost),
        "win_rate_pct": _round(100 * len(won) / max(1, len(won) + len(lost))),
        "bookings_last_12_months": _round(sum(o["base_net_total"] or 0 for o in orders)),
        "sales_orders_last_12_months": len(orders),
        "leads_by_status": dict(Counter(lead["status"] for lead in leads).most_common()),
        "open_follow_ups": len(todos),
        "overdue_follow_ups": sum(1 for t in todos if t["date"] and t["date"] < today.isoformat()),
    }


@mcp.tool(annotations=READ_ONLY)
async def pipeline(
    group_by: Annotated[OppDimension, Field(description="Dimension to break the open pipeline down by")]
    = "sales_stage",
) -> list[dict]:
    """Open pipeline (open opportunities) grouped by rep, stage, territory, industry, type, segment or
    source: deal count, total value and probability-weighted value."""
    users = await user_names()
    groups = defaultdict(lambda: {"deals": 0, "value": 0.0, "weighted_value": 0.0})
    for o in await _opps():
        if o["status"] not in ("Open", "Quotation", "Replied"):
            continue
        g = groups[_label(o, group_by, users)]
        g["deals"] += 1
        g["value"] += o["opportunity_amount"] or 0
        g["weighted_value"] += (o["opportunity_amount"] or 0) * (o["probability"] or 0) / 100
    return sorted(({group_by: k, **{m: _round(v) if isinstance(v, float) else v for m, v in g.items()}}
                   for k, g in groups.items()), key=lambda r: -r["value"])


@mcp.tool(annotations=READ_ONLY)
async def win_loss(
    group_by: Annotated[OppDimension, Field(description="Dimension to compare win rates across")]
    = "opportunity_owner",
    since: Annotated[str | None, Field(description="Only opportunities created on/after this date (YYYY-MM-DD)")]
    = None,
) -> dict:
    """Win/loss analysis of closed opportunities: win rate and won value per group, plus the top lost
    reasons and competitors we lost to."""
    users = await user_names()
    opps = [o for o in await _opps() if o["status"] in ("Converted", "Lost")
            and (not since or (o["transaction_date"] or "") >= since)]
    groups = defaultdict(lambda: {"won": 0, "lost": 0, "won_value": 0.0, "lost_value": 0.0})
    for o in opps:
        g = groups[_label(o, group_by, users)]
        k = "won" if o["status"] == "Converted" else "lost"
        g[k] += 1
        g[f"{k}_value"] += o["opportunity_amount"] or 0
    rows = [{group_by: k, **g, "won_value": _round(g["won_value"]), "lost_value": _round(g["lost_value"]),
             "win_rate_pct": _round(100 * g["won"] / max(1, g["won"] + g["lost"]))} for k, g in groups.items()]

    lost_names = {o["name"] for o in opps if o["status"] == "Lost"}
    reasons, competitors = Counter(), Counter()
    seen = set()
    for row in await get_list("Opportunity", ["name", "order_lost_reason", "competitors.competitor as competitor"],
                              filters=[["status", "=", "Lost"]]):
        if row["name"] not in lost_names:
            continue
        if row["name"] not in seen and row.get("order_lost_reason"):
            reasons[row["order_lost_reason"]] += 1
        seen.add(row["name"])
        if row.get("competitor"):
            competitors[row["competitor"]] += 1
    return {"by_group": sorted(rows, key=lambda r: -r["win_rate_pct"]),
            "top_lost_reasons": dict(reasons.most_common(10)),
            "competitors_lost_to": dict(competitors.most_common(10))}


@mcp.tool(annotations=READ_ONLY)
async def lead_funnel(
    group_by: Annotated[Literal["source", "territory", "industry", "lead_owner", "market_segment"],
                        Field(description="Dimension to break the funnel down by")] = "source",
) -> list[dict]:
    """Lead funnel: lead counts by status for each group, with the share of leads that reached an
    opportunity or converted to a customer."""
    src = await source_field("Lead")
    fields = ["status", "territory", "industry", "lead_owner", "market_segment"] + ([src] if src else [])
    users = await user_names() if group_by == "lead_owner" else {}
    groups = defaultdict(Counter)
    for lead in await get_list("Lead", fields):
        key = lead.get(src) if group_by == "source" and src else lead.get(group_by)
        groups[users.get(key, key) or "(not set)"][lead["status"]] += 1
    out = []
    for k, c in groups.items():
        total = sum(c.values())
        advanced = sum(c[s] for s in ("Opportunity", "Quotation", "Converted", "Lost Quotation"))
        out.append({group_by: k, "leads": total, "by_status": dict(c),
                    "reached_opportunity_pct": _round(100 * advanced / total),
                    "converted_pct": _round(100 * c["Converted"] / total)})
    return sorted(out, key=lambda r: -r["leads"])


@mcp.tool(annotations=READ_ONLY)
async def follow_ups(
    which: Annotated[Literal["overdue", "upcoming", "all_open"], Field(description="Which open follow-ups")]
    = "overdue",
    rep_email: Annotated[str | None, Field(description="Only follow-ups assigned to this user (email)")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict:
    """Open follow-up tasks (ToDos) with due date, owner and the lead/opportunity/customer they relate
    to, plus a count per rep. Overdue = due date before today."""
    today = _today().isoformat()
    filters = [["status", "=", "Open"]]
    if which == "overdue":
        filters.append(["date", "<", today])
    elif which == "upcoming":
        filters.append(["date", ">=", today])
    if rep_email:
        filters.append(["allocated_to", "=", rep_email])
    rows = await get_list("ToDo", ["name", "date", "priority", "allocated_to", "reference_type",
                                   "reference_name", "description"], filters=filters, order_by="date asc")
    users = await user_names()
    per_rep = Counter(users.get(r["allocated_to"], r["allocated_to"]) for r in rows)
    items = [{"todo": r["name"], "due": r["date"], "days_overdue": (_today() - date.fromisoformat(r["date"])).days
              if r["date"] and r["date"] < today else 0, "priority": r["priority"],
              "owner": users.get(r["allocated_to"], r["allocated_to"]), "related_to": r["reference_type"],
              "record": r["reference_name"], "task": _strip_html(r["description"])} for r in rows[:limit]]
    return {"total": len(rows), "per_rep": dict(per_rep.most_common()), "items": items}


@mcp.tool(annotations=READ_ONLY)
async def stalled_deals(limit: Annotated[int, Field(ge=1, le=200)] = 25) -> list[dict]:
    """Open opportunities whose expected closing date has passed, largest first."""
    today = _today()
    users = await user_names()
    rows = [o for o in await _opps() if o["status"] in ("Open", "Quotation", "Replied")
            and o["expected_closing"] and o["expected_closing"] < today.isoformat()]
    rows.sort(key=lambda o: -(o["opportunity_amount"] or 0))
    return [{"opportunity": o["name"], "account": o["customer_name"] or o["party_name"],
             "value": _round(o["opportunity_amount"]), "stage": o["sales_stage"], "status": o["status"],
             "expected_closing": o["expected_closing"],
             "days_past_due": (today - date.fromisoformat(o["expected_closing"])).days,
             "owner": users.get(o["opportunity_owner"], o["opportunity_owner"])} for o in rows[:limit]]


@mcp.tool(annotations=READ_ONLY)
async def account_health(
    inactive_days: Annotated[int, Field(ge=1, le=730, description="Flag accounts untouched for this many days")]
    = 90,
) -> dict:
    """Customer engagement: last recorded touch per customer (meetings, notes, tasks, opportunities, orders),
    open pipeline and bookings, and which accounts have had no contact for `inactive_days`."""
    today = _today()
    customers = await get_list("Customer", ["name", "customer_group", "territory", "industry"])
    last = defaultdict(str)

    def touch(cust, d):
        if cust and d and str(d)[:10] > last[cust]:
            last[cust] = str(d)[:10]

    for c in await get_list("Comment", ["reference_name", "creation", "content"],
                            filters=[["reference_doctype", "=", "Customer"], ["comment_type", "=", "Comment"]]):
        content = c.get("content") or ""
        # notes may carry their real date as a "[DD Mon YYYY]" prefix
        touch(c["reference_name"], _bracket_date(content) or c["creation"])
    for t in await get_list("ToDo", ["reference_name", "date", "status"],
                            filters=[["reference_type", "=", "Customer"], ["status", "=", "Closed"]]):
        touch(t["reference_name"], t["date"])
    events = await get_list("Event", ["starts_on", "event_participants.reference_docname as party",
                                      "event_participants.reference_doctype as party_type"],
                            filters=[["Event Participants", "reference_doctype", "=", "Customer"]])
    for e in events:
        if e.get("party_type") == "Customer" and e["starts_on"][:10] <= today.isoformat():
            touch(e["party"], e["starts_on"])
    pipeline_by, bookings_by = defaultdict(float), defaultdict(float)
    for o in await _opps():
        if o["party_name"]:
            touch(o["party_name"], o["transaction_date"])
            if o["status"] in ("Open", "Quotation", "Replied"):
                pipeline_by[o["party_name"]] += o["opportunity_amount"] or 0
    for so in await get_list("Sales Order", ["customer", "transaction_date", "base_net_total"],
                             filters=[["docstatus", "=", 1]]):
        touch(so["customer"], so["transaction_date"])
        bookings_by[so["customer"]] += so["base_net_total"] or 0

    rows = []
    for c in customers:
        lt = last.get(c["name"]) or None
        days = (today - date.fromisoformat(lt)).days if lt else None
        rows.append({"customer": c["name"], "group": c["customer_group"], "territory": c["territory"],
                     "industry": c["industry"], "last_touch": lt, "days_since_touch": days,
                     "open_pipeline": _round(pipeline_by[c["name"]]), "bookings": _round(bookings_by[c["name"]])})
    inactive = sorted((r for r in rows if r["days_since_touch"] is None or r["days_since_touch"] >= inactive_days),
                      key=lambda r: -(r["bookings"] + r["open_pipeline"]))
    return {"customers": len(rows), "inactive_count": len(inactive), "inactive_accounts": inactive[:50]}


@mcp.tool(annotations=READ_ONLY)
async def bookings(
    group_by: Annotated[Literal["month", "customer", "sales_person", "territory", "customer_group"],
                        Field(description="How to group submitted sales orders")] = "month",
    since: Annotated[str | None, Field(description="From date (YYYY-MM-DD); default 12 months ago")] = None,
) -> list[dict]:
    """Bookings (submitted sales orders) grouped by month, customer, sales person, territory or segment."""
    since = since or (_today() - timedelta(days=365)).isoformat()
    orders = await get_list("Sales Order", ["name", "customer", "transaction_date", "base_net_total", "territory",
                                            "customer_group"],
                            filters=[["docstatus", "=", 1], ["transaction_date", ">=", since]])
    reps = {}
    if group_by == "sales_person":
        for row in await get_list("Sales Order", ["name", "sales_team.sales_person as sp"],
                                  filters=[["docstatus", "=", 1], ["transaction_date", ">=", since]]):
            reps[row["name"]] = row.get("sp") or "(no sales person)"
    groups = defaultdict(lambda: {"orders": 0, "value": 0.0})
    for o in orders:
        key = {"month": (o["transaction_date"] or "")[:7], "customer": o["customer"],
               "sales_person": reps.get(o["name"], "(no sales person)"), "territory": o["territory"],
               "customer_group": o["customer_group"]}[group_by]
        groups[key]["orders"] += 1
        groups[key]["value"] += o["base_net_total"] or 0
    rows = [{group_by: k, "orders": g["orders"], "value": _round(g["value"])} for k, g in groups.items()]
    return sorted(rows, key=lambda r: r[group_by] if group_by == "month" else -r["value"])


# --------------------------------------------------------------------------------------
# Generic read tools
# --------------------------------------------------------------------------------------

def _check_doctype(doctype: str):
    if doctype not in READABLE_DOCTYPES:
        raise ToolError(f"Doctype '{doctype}' is not available. Allowed: {', '.join(sorted(READABLE_DOCTYPES))}")


@mcp.tool(annotations=READ_ONLY)
async def search_records(
    doctype: Annotated[str, Field(description="ERPNext doctype, e.g. Lead, Opportunity, Customer, ToDo")],
    filters: Annotated[list[list] | None, Field(
        description='Frappe filters, e.g. [["status","=","Open"],["territory","=","India"]]')] = None,
    fields: Annotated[list[str] | None, Field(description="Fields to return; default name + key fields")] = None,
    order_by: Annotated[str | None, Field(description='e.g. "modified desc"')] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> list[dict]:
    """List ERPNext records of a CRM/sales doctype with filters. Use get_record for one full document."""
    _check_doctype(doctype)
    return await get_list(doctype, fields or ["*"], filters=filters, order_by=order_by, limit=limit)


@mcp.tool(annotations=READ_ONLY)
async def get_record(
    doctype: Annotated[str, Field(description="ERPNext doctype, e.g. Opportunity")],
    name: Annotated[str, Field(description="Document name/ID, e.g. CRM-OPP-2026-00012")],
    include_activity: Annotated[bool, Field(description="Also return notes and follow-ups on the record")] = True,
) -> dict:
    """Fetch one full ERPNext document (with child tables), plus its notes and follow-up tasks."""
    _check_doctype(doctype)
    doc = (await _request("GET", f"/api/resource/{doctype}/{quote(name, safe='')}"))["data"]
    out = {"document": doc}
    if include_activity:
        out["notes"] = [{"date": c["creation"][:10], "by": c["comment_by"] or c["owner"],
                         "note": _strip_html(c["content"])}
                        for c in await get_list("Comment", ["creation", "comment_by", "owner", "content"],
                                                filters=[["reference_doctype", "=", doctype],
                                                         ["reference_name", "=", name],
                                                         ["comment_type", "=", "Comment"]],
                                                order_by="creation desc", limit=50)]
        out["follow_ups"] = await get_list("ToDo", ["name", "status", "date", "allocated_to", "description"],
                                           filters=[["reference_type", "=", doctype],
                                                    ["reference_name", "=", name]],
                                           order_by="date desc", limit=50)
    return out


# --------------------------------------------------------------------------------------
# Write tools (only registered when ERP_ALLOW_WRITES=true)
# --------------------------------------------------------------------------------------

if ALLOW_WRITES:
    WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}

    @mcp.tool(annotations=WRITE)
    async def create_follow_up(
        reference_doctype: Annotated[Literal["Lead", "Opportunity", "Customer"], Field()],
        reference_name: Annotated[str, Field(description="Record name, e.g. CRM-LEAD-2026-00101")],
        task: Annotated[str, Field(description="What needs to be done")],
        due_date: Annotated[str, Field(description="YYYY-MM-DD")],
        assign_to: Annotated[str, Field(description="Email of the ERPNext user who owns the task")],
        priority: Literal["Low", "Medium", "High"] = "Medium",
    ) -> dict:
        """Create a follow-up task (ToDo) on a lead, opportunity or customer, assigned to a user."""
        doc = {"doctype": "ToDo", "status": "Open", "reference_type": reference_doctype,
               "reference_name": reference_name, "description": task, "date": due_date,
               "allocated_to": assign_to, "priority": priority}
        data = (await _request("POST", "/api/resource/ToDo", json=doc))["data"]
        return {"created": data["name"], "url": f"{ERP_URL}/desk/todo/{data['name']}"}

    @mcp.tool(annotations=WRITE)
    async def add_note(
        reference_doctype: Annotated[Literal["Lead", "Opportunity", "Customer"], Field()],
        reference_name: Annotated[str, Field(description="Record name")],
        note: Annotated[str, Field(description="Note text, e.g. a call summary")],
    ) -> dict:
        """Add a note (comment) to the timeline of a lead, opportunity or customer."""
        doc = {"doctype": "Comment", "comment_type": "Comment", "reference_doctype": reference_doctype,
               "reference_name": reference_name, "content": note}
        data = (await _request("POST", "/api/resource/Comment", json=doc))["data"]
        return {"created": data["name"]}


# --------------------------------------------------------------------------------------

def _strip_html(s: str | None) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _bracket_date(text: str) -> str | None:
    import re
    from datetime import datetime
    m = re.match(r"\s*(?:<[^>]+>)*\s*\[(\d{2} \w{3} \d{4})\]", text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d %b %Y").date().isoformat()
    except ValueError:
        return None


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
