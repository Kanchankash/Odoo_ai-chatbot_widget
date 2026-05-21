"""
Safe ORM query builder — dynamic model discovery.

Flow:
  1. Discover queryable models (admin allow-list > business prefix filter > all non-transient).
  2. Build a live field-schema catalog for each candidate model.
  3. Ask the reasoner LLM to build a structured query using the real field names.
  4. Validate every part of the spec before executing anything.

No LLM-generated SQL ever touches the database.
Odoo 19: uses Model._read_group() which returns list-of-tuples.
"""
import json
import logging
from datetime import date, timedelta
from typing import Any

from . import llm_client

_logger = logging.getLogger(__name__)

_MAX_LIMIT_CAP = 500
_MAX_MODELS_IN_CATALOG = 50   # cap prompt size
_MAX_FIELDS_PER_MODEL = 15    # key fields per model shown to LLM

# These models always appear first in the catalog regardless of alphabetical order.
# Prevents account.* (53 models) from flooding the 30-model cap before stock/sale/crm get in.
_PRIORITY_MODELS = [
    "sale.order",
    "sale.order.line",
    "account.move",
    "account.move.line",
    "stock.picking",
    "stock.move",
    "crm.lead",
    "hr.employee",
    "product.template",
    "product.product",
    "res.partner",
    "purchase.order",
    "purchase.order.line",
    "mrp.production",
    "stock.warehouse.orderpoint",
]

_SKIP_FIELD_PREFIXES = (
    "message_", "activity_", "write_", "create_", "__last_update",
    "access_", "website_", "mail_",
)
_SKIP_FIELD_TYPES = ("binary", "html", "serialized", "many2many_tags")

# Business/ERP model prefixes — only these are shown when no allow-list is configured.
# This prevents unrelated modules (library, school, etc.) from polluting the catalog.
_BUSINESS_MODEL_PREFIXES = (
    "sale.",         # Sales
    "purchase.",     # Purchase
    "account.",      # Accounting / Invoicing
    "stock.",        # Inventory
    "mrp.",          # Manufacturing
    "crm.",          # CRM / Leads
    "hr.",           # HR / Employees / Payroll
    "project.",      # Project Management
    "helpdesk.",     # Helpdesk
    "pos.",          # Point of Sale
    "product.",      # Products
    "res.partner",   # Contacts / Customers
    "res.company",   # Company
)

_SYSTEM_PROMPT_TEMPLATE = """\
You are a structured-query generator for Odoo 19.
Given a natural-language request, pick the best model from the catalog below and
respond with a single JSON object. Use ONLY fields that appear in the catalog.

Today's date: {today}
Last 90 days start: {date_90d}
Current year start: {year_start}

=== AVAILABLE MODELS ===
{model_catalog}
========================

Required JSON shape:
{{
  "model": "<model technical name from catalog>",
  "domain": [<Odoo domain tuples, e.g. ["state","=","sale"]>],
  "fields": ["<field_name>", ...],
  "groupby": ["<field_name>", ...],
  "measures": [{{"field": "<field>", "agg": "sum|avg|count|max|min"}}, ...],
  "order": "<field> desc|asc or empty string>",
  "limit": <integer 1-500>,
  "chart_type": "bar|line|pie|doughnut|none"
}}

CRITICAL RULES:
1. For "how many / count / total / revenue / grouped" questions → ALWAYS use measures.
   Use agg="count" for counts, agg="sum" for amounts. Use groupby to segment.
   Never leave groupby AND measures both empty for aggregate questions.
2. For "show me details / list / top N records" questions → empty groupby, empty measures.
   "top quotations / top orders / top invoices / highest" → fields must include: name, partner_id, amount_total, date_order. Order by amount_total desc.
   "latest / most recent / last / when was" → fields must include: name, partner_id, date_order. Order by date_order desc, limit=1.
   NEVER order by date for "top" questions — "top" always means highest value (amount_total desc).
   For sale.order and account.move flat queries: ALWAYS include partner_id and amount_total in fields.
   For crm.lead flat queries: ALWAYS include name, partner_id, expected_revenue, stage_id in fields.
   For hr.employee flat queries: ALWAYS include name, department_id, job_title in fields.
3. Use ONLY field names from the catalog for the chosen model.
4. MANDATORY domain filters (always include these):
     "confirmed sale orders" / "orders":   [["state","=","sale"]]
     "all sale orders" / "total orders" / "quotations and orders": [] (no filter, all states)
     "quotations" only:                    [["state","in",["draft","sent"]]]
     customer invoices (posted):           [["state","=","posted"],["move_type","=","out_invoice"]]
     draft invoices:                       [["state","=","draft"],["move_type","=","out_invoice"]]
     all invoices:                         [["move_type","=","out_invoice"]]
     vendor bills:                         [["state","=","posted"],["move_type","=","in_invoice"]]
     CRM leads (active only):              [["active","=",true]]
     HR employees:                         [["active","=",true]]
5. For "total revenue / total sales amount" on sale.order → use amount_total field with agg="sum", no groupby.
6. SPECIAL QUERY PATTERNS (use exactly as specified):
   "top categories / category revenue / revenue by category / treemap / category breakdown":
     → model: sale.order.line, domain: [["order_id.state","=","sale"]],
       groupby: ["product_id.categ_id"], measures: [{{"field":"price_subtotal","agg":"sum"}}], chart_type: "pie"
   "top products / best selling products / products by revenue":
     → model: sale.order.line, domain: [["order_id.state","=","sale"]],
       groupby: ["product_id"], measures: [{{"field":"product_uom_qty","agg":"sum"}},{{"field":"price_subtotal","agg":"sum"}}], chart_type: "bar"
   "revenue by customer / top customers by revenue":
     → model: sale.order, domain: [["state","=","sale"]],
       groupby: ["partner_id"], measures: [{{"field":"amount_total","agg":"sum"}}], chart_type: "bar"
   "sales by month / monthly sales / sales trend":
     → model: sale.order, domain: [["state","=","sale"]],
       groupby: ["date_order:month"], measures: [{{"field":"amount_total","agg":"sum"}}], chart_type: "line"
   "products needing reorder / low stock / reorder alerts / below reorder point / stockout risk":
     → model: stock.warehouse.orderpoint, domain: [],
       fields: ["product_id","qty_on_hand","product_min_qty","product_max_qty"],
       order: "qty_on_hand asc", chart_type: "none"
   "delivery orders / to deliver / pending deliveries / outgoing shipments":
     → model: stock.picking, domain: [["picking_type_id.code","=","outgoing"],["state","not in",["done","cancel"]]],
       fields: ["name","partner_id","state","scheduled_date"], order: "scheduled_date asc", chart_type: "none"
   "receipts / incoming shipments / purchase receipts / to receive":
     → model: stock.picking, domain: [["picking_type_id.code","=","incoming"],["state","not in",["done","cancel"]]],
       fields: ["name","partner_id","state","scheduled_date"], order: "scheduled_date asc", chart_type: "none"
   NEVER use stock.quant or stock.move for revenue or category questions.
   NEVER use sale.order for "delivery orders" — delivery orders are always stock.picking.
7. chart_type: "bar" for grouped comparisons, "pie" for share/proportion/treemap, "line" for trends, "none" if no chart.
8. DATE FILTERS — apply these when the user specifies or implies a time period:
   "last 90 days" / "recent" / "this quarter" / no period specified for invoices/revenue:
     → add ["invoice_date",">=","{date_90d}"] for account.move
     → add ["date_order",">=","{date_90d}"] for sale.order
   "this year" / "current year":
     → add ["invoice_date",">=","{year_start}"] for account.move
     → add ["date_order",">=","{year_start}"] for sale.order
   "all time" / "total ever" / "since beginning":
     → no date filter
   DEFAULT (no time period mentioned): apply last-90-days filter for invoice/revenue queries.
9. Respond with ONLY the JSON object — no markdown fences, no explanation."""


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

def _get_available_models(env) -> dict[str, str]:
    """
    Return {model_technical_name: human_description} for queryable models.

    Priority:
      1. Admin allow-list (ai_chatbot.allowed_model_names) — exact model names, CSV.
      2. Auto-discover only business/ERP models matching _BUSINESS_MODEL_PREFIXES.
         This prevents unrelated custom modules from appearing in the catalog.

    To expose a custom module's models, add them to the allow-list in Settings.
    """
    param = (
        env["ir.config_parameter"]
        .sudo()
        .get_param("ai_chatbot.allowed_model_names", "")
        .strip()
    )

    if param:
        # Admin-restricted explicit list — include exactly what they asked for
        names = [n.strip() for n in param.split(",") if n.strip()]
        ir_models = env["ir.model"].search([("model", "in", names)])
    else:
        # Auto-discover: only business/ERP models
        ir_models = env["ir.model"].search(
            [("transient", "=", False)],
            limit=_MAX_MODELS_IN_CATALOG * 4,
            order="model asc",
        )

    # Build lookup from ir.model search
    model_map = {m.model: (m.name or m.model) for m in ir_models if m.model in env}

    result = {}

    if not param:
        # Priority models always come first so they're never crowded out by account.*
        for name in _PRIORITY_MODELS:
            if name in model_map:
                result[name] = model_map[name]

    # Then fill remaining slots with other business models from the search
    for m in ir_models:
        if len(result) >= _MAX_MODELS_IN_CATALOG:
            break
        if m.model in result:
            continue
        if m.model not in env:
            continue
        if not param and not any(m.model.startswith(p) for p in _BUSINESS_MODEL_PREFIXES):
            continue
        result[m.model] = m.name or m.model

    return result


# ---------------------------------------------------------------------------
# Live field schema
# ---------------------------------------------------------------------------

def _get_model_fields(env, model_name: str) -> dict[str, dict]:
    """Return filtered fields_get() dict for a model."""
    try:
        return env[model_name].fields_get(attributes=["string", "type", "relation"])
    except Exception:
        return {}


def _build_model_catalog(env, available_models: dict[str, str]) -> str:
    """
    Build a compact text catalog of models + their key fields.
    Injected verbatim into the reasoner system prompt.
    """
    lines = []
    for model_name, description in list(available_models.items())[:_MAX_MODELS_IN_CATALOG]:
        fields = _get_model_fields(env, model_name)
        field_parts = []
        for fname, finfo in fields.items():
            if any(fname.startswith(p) for p in _SKIP_FIELD_PREFIXES):
                continue
            if finfo.get("type") in _SKIP_FIELD_TYPES:
                continue
            ftype = finfo.get("type", "?")
            fstr = finfo.get("string", fname)
            rel = f" → {finfo['relation']}" if finfo.get("relation") else ""
            field_parts.append(f"{fname}({ftype}{rel})[{fstr}]")
            if len(field_parts) >= _MAX_FIELDS_PER_MODEL:
                break

        lines.append(f"  {model_name}  ({description})")
        lines.append(f"    fields: {', '.join(field_parts)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_query_spec(env, user_message: str, intent: str) -> dict | None:
    """
    Ask the reasoner model to produce a validated structured query spec.

    The prompt is built dynamically from the live Odoo model registry so the
    LLM always sees current field names regardless of which modules are installed.

    Args:
        env: Odoo environment
        user_message: raw user message
        intent: 'data_query' or 'chart'

    Returns:
        validated spec dict or None on failure
    """
    available = _get_available_models(env)
    if not available:
        _logger.warning("No queryable models found. Add models to the allow-list or install modules.")
        return None

    catalog = _build_model_catalog(env, available)
    today = date.today()
    date_90d = (today - timedelta(days=89)).isoformat()   # Odoo "Last 90 Days" is exclusive of day-90
    year_start = today.replace(month=1, day=1).isoformat()
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        model_catalog=catalog,
        today=today.isoformat(),
        date_90d=date_90d,
        year_start=year_start,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        raw = llm_client.chat_completion_sync(
            env,
            messages,
            model_role="reasoner",
            max_tokens=600,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        spec = json.loads(raw)
        return _validate_spec(env, spec, set(available.keys()))
    except Exception as exc:
        _logger.warning("Query spec generation failed: %s", exc)
        return None


def _validate_spec(env, spec: dict, allowed_model_names: set[str]) -> dict | None:
    """Validate every field in the spec against the live registry. Returns None if invalid."""
    model_name = spec.get("model", "")
    if model_name not in allowed_model_names:
        _logger.warning("LLM requested disallowed/unknown model %r", model_name)
        return None

    if model_name not in env:
        _logger.warning("Model %r not in registry", model_name)
        return None

    all_fields = env[model_name].fields_get()

    # validate domain
    domain = spec.get("domain", [])
    clean_domain = []
    for item in domain:
        if isinstance(item, str) and item in ("&", "|", "!"):
            clean_domain.append(item)
            continue
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            _logger.debug("Skipping malformed domain item %r", item)
            continue
        field_path = str(item[0]).split(".")[0]  # support dotted paths like partner_id.country_id
        if field_path not in all_fields:
            _logger.warning("Domain field %r not on %s — dropping", item[0], model_name)
            continue
        clean_domain.append(list(item))

    # validate groupby
    groupby = []
    for gb in spec.get("groupby", []):
        field_name = gb.split(":")[0]
        if field_name not in all_fields:
            _logger.warning("Groupby field %r not on %s — skipping", field_name, model_name)
            continue
        groupby.append(gb)

    # validate measures
    measures = []
    for m in spec.get("measures", []):
        if not isinstance(m, dict):
            continue
        field_name = m.get("field", "")
        agg = m.get("agg", "sum")
        if field_name and field_name not in all_fields:
            _logger.warning("Measure field %r not on %s — skipping", field_name, model_name)
            continue
        if agg not in ("sum", "avg", "count", "max", "min"):
            agg = "sum"
        measures.append({"field": field_name, "agg": agg})

    # validate explicit fields list (for flat/detail queries)
    fields = []
    for f in spec.get("fields", []):
        fname = str(f).split(".")[0]
        if fname in all_fields:
            fields.append(fname)

    # validate order clause
    order = str(spec.get("order", "") or "").strip()
    if order:
        order_field = order.split()[0]
        if order_field not in all_fields:
            order = ""

    limit = min(int(spec.get("limit", 50)), _MAX_LIMIT_CAP)
    chart_type = spec.get("chart_type", "none")
    if chart_type not in ("bar", "line", "pie", "doughnut", "none"):
        chart_type = "none"

    return {
        "model": model_name,
        "domain": clean_domain,
        "fields": fields,
        "groupby": groupby,
        "measures": measures,
        "order": order,
        "limit": limit,
        "chart_type": chart_type,
    }


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

def execute_query(env, spec: dict) -> dict[str, Any]:
    """
    Execute a validated spec. Uses _read_group for aggregates, search_read for flat lists.
    """
    Model = env[spec["model"]]
    domain = spec["domain"]
    groupby = spec["groupby"]
    measures = spec["measures"]
    limit = spec["limit"]
    chart_type = spec["chart_type"]
    fields = spec.get("fields", [])
    order = spec.get("order", "")

    if measures:
        return _execute_grouped(Model, domain, groupby, measures, limit, chart_type, order)
    return _execute_flat(Model, domain, fields, order, limit, chart_type)


def _execute_grouped(
    Model, domain, groupby: list[str], measures: list[dict], limit: int, chart_type: str,
    order: str = "",
) -> dict:
    """
    Odoo 19 _read_group() — returns list of tuples.
    First N elements are groupby values (recordsets for relational fields).
    Remaining elements are aggregate values in order.
    """
    agg_specs = []
    for m in measures:
        field = m.get("field", "")
        agg = m.get("agg", "sum")
        if field:
            agg_specs.append(f"{field}:{agg}")

    if not agg_specs:
        agg_specs = ["id:count"]

    # Build _read_group order string from spec order (e.g. "amount_total desc" → "amount_total:sum desc")
    rg_order = None
    if order:
        parts = order.strip().split()
        order_field = parts[0]
        order_dir = parts[1].lower() if len(parts) > 1 else "asc"
        for m in measures:
            if m.get("field") == order_field:
                rg_order = f"{order_field}:{m['agg']} {order_dir}"
                break
        if not rg_order:
            rg_order = order  # groupby field — use as-is

    try:
        rows_raw = Model._read_group(
            domain=domain,
            groupby=groupby,
            aggregates=agg_specs,
            limit=limit,
            order=rg_order,
        )
    except Exception as exc:
        _logger.warning("_read_group with order failed (%s), retrying without order: %s", rg_order, exc)
        try:
            rows_raw = Model._read_group(
                domain=domain,
                groupby=groupby,
                aggregates=agg_specs,
                limit=limit,
                order=None,
            )
        except Exception as exc2:
            _logger.error("_read_group failed on %s: %s", Model._name, exc2)
            return {"columns": [], "rows": [], "chart_type": chart_type}

    groupby_labels = [gb.split(":")[0] for gb in groupby]
    measure_labels = [
        f"{m['field']}_{m['agg']}" if m.get("field") else "count"
        for m in measures
    ] if measures else ["count"]
    columns = groupby_labels + measure_labels

    rows = []
    for row_tuple in rows_raw:
        row = []
        for gb_val in row_tuple[: len(groupby)]:
            if hasattr(gb_val, "display_name"):
                row.append(gb_val.display_name if gb_val else "(none)")
            elif isinstance(gb_val, tuple):
                row.append(gb_val[1] if len(gb_val) > 1 else str(gb_val))
            else:
                row.append(gb_val)
        for agg_val in row_tuple[len(groupby):]:
            row.append(round(agg_val, 2) if isinstance(agg_val, float) else agg_val)
        rows.append(row)

    return {"columns": columns, "rows": rows, "chart_type": chart_type}


def _execute_flat(
    Model, domain, fields: list[str], order: str, limit: int, chart_type: str
) -> dict:
    """Flat search_read for detail/list queries."""
    fields_to_read = fields if fields else ["id", "display_name"]

    try:
        records = Model.search_read(
            domain=domain,
            fields=fields_to_read,
            limit=limit,
            order=order or None,
        )
    except Exception as exc:
        _logger.error("search_read failed on %s: %s", Model._name, exc)
        return {"columns": [], "rows": [], "chart_type": chart_type}

    rows = [[rec.get(f) for f in fields_to_read] for rec in records]
    return {"columns": fields_to_read, "rows": rows, "chart_type": chart_type}
