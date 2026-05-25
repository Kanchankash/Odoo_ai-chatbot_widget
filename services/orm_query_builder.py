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
Read the COMPLETE user request from start to finish — every keyword matters.
Then respond with a single JSON object. Use ONLY fields that appear in the catalog.

Today's date: {today}
Last 90 days start: {date_90d}
Current year start: {year_start}

=== AVAILABLE MODELS ===
{model_catalog}
========================

Required JSON shape — include ALL fields, especially "reasoning":
{{
  "reasoning": "<step-by-step: 1) intent (list/aggregate/trend/rank)? 2) model from glossary? 3) ALL dimensions (time + entity)? 4) domain filters? 5) chart type?>",
  "model": "<model technical name from catalog>",
  "domain": [<Odoo domain tuples, e.g. ["state","=","sale"]>],
  "fields": ["<field_name>", ...],
  "groupby": ["<field_name>", ...],
  "measures": [{{"field": "<field>", "agg": "sum|avg|count|max|min"}}, ...],
  "order": "<field> desc|asc or empty string>",
  "limit": <integer 1-500>,
  "chart_type": "bar|line|pie|doughnut|none"
}}

=== TERM → MODEL GLOSSARY ===
Map user vocabulary to the correct model + mandatory domain filters:
  "customers" / "clients"      → res.partner,    domain must include: [["customer_rank",">",0]]
  "orders" / "sales"           → sale.order,     domain must include: [["state","in",["sale","done"]]]
  "quotations" / "drafts"      → sale.order,     domain must include: [["state","in",["draft","sent"]]]
  "invoices" / "revenue"       → account.move,   domain must include: [["move_type","=","out_invoice"],["state","=","posted"]]
  "all invoices" (any state)   → account.move,   domain must include: [["move_type","=","out_invoice"]]
  "bills" / "vendor invoices"  → account.move,   domain must include: [["move_type","=","in_invoice"],["state","=","posted"]]
  "opportunities" / "pipeline" → crm.lead,       domain must include: [["type","=","opportunity"],["active","=",true]]
  "leads" (unqualified)        → crm.lead,       domain must include: [["type","=","lead"],["active","=",true]]
  "lost deals" / "lost leads"  → crm.lead,       domain must include: [["active","=",false],["probability","=",0]]
  "won deals" / "closed won"   → crm.lead,       domain must include: [["active","=",false],["probability","=",100]]
  "delivery orders" / "shipments" → stock.picking, domain: [["picking_type_code","=","outgoing"]]
  "receipts" / "incoming"      → stock.picking,  domain: [["picking_type_code","=","incoming"]]
  "low stock" / "reorder"      → stock.warehouse.orderpoint, domain: []
  "employees" / "staff"        → hr.employee,    domain must include: [["active","=",true]]
  "products" (catalog)         → product.template
  "products" (revenue/qty)     → sale.order.line, domain: [["order_id.state","=","sale"]]

=== ODOO DOMAIN SYNTAX ===
Domains are lists of 3-element arrays: [["field", "operator", "value"], ...]
Valid operators: "=", "!=", ">", ">=", "<", "<=", "in", "not in", "like", "ilike", "=like"
Booleans: [["active","=",false]]  — JSON uses lowercase true/false
Many2one: [["partner_id","=",42]] or dotted path in DOMAIN only: [["order_id.state","=","sale"]]
Dates: [["date_order",">=","2025-01-01"]]  — always ISO format strings
AND is implicit (default). OR needs explicit: ["|", ["field","=",1], ["field","=",2]]
NEVER generate SQL (WHERE, JOIN, SELECT). ONLY Odoo domain tuple syntax.

=== READ_GROUP LIMITATIONS ===
When using groupby + measures (_read_group), these constraints apply:
- CANNOT group by a dotted/related path: NO groupby: ["partner_id.country_id"].
  Only use direct fields on the chosen model for groupby (e.g. "partner_id", "product_id").
- CAN filter by dotted path in domain: [["order_id.state","=","sale"]] is valid.
- CANNOT aggregate a field from a related model. Only aggregate direct fields.
- For cross-model reports: choose the model that OWNS the numeric field you need to sum.
  Example: revenue by product → use sale.order.line (owns price_subtotal), groupby product_id.
  Example: revenue by category → use sale.order.line, groupby product_id (not product_id.categ_id).

=== DIMENSION EXTRACTION (do this mentally before writing JSON) ===
Read the full request and identify ALL dimensions present:
  TIME dimension   → month/year/quarter/date mentioned? → groupby date_order:month or date_order:year
  ENTITY dimension → customer/partner/product/employee/department mentioned? → groupby partner_id/product_id/department_id
  MEASURE          → amount/revenue/count/quantity mentioned? → measures with agg=sum/count
  FILTER           → this year/last 90 days/confirmed/paid? → domain
  CHART            → trend over time → line; compare categories → bar; share/percent → pie

RULE: If the request mentions BOTH a time period (month/year) AND an entity (customer/product),
      put BOTH in groupby. Example: "monthly sales by customer" → groupby: ["date_order:month","partner_id"]

=== CRITICAL RULES ===
1. For "how many / count / total / revenue / grouped" questions → ALWAYS use measures.
   Use agg="count" for counts, agg="sum" for amounts. Use groupby to segment by ALL named dimensions.
   Never leave groupby AND measures both empty for aggregate questions.
2. For "show me details / list / top N records" → empty groupby, empty measures.
   "top / highest / best" → fields include name, partner_id, amount_total, date_order. Order by amount_total desc.
   "latest / most recent / last" → include name, partner_id, date_order. Order by date_order desc.
   For sale.order and account.move flat queries: ALWAYS include partner_id and amount_total in fields.
   For crm.lead flat queries: ALWAYS include name, partner_id, expected_revenue, stage_id in fields.
   For hr.employee flat queries: ALWAYS include name, department_id, job_title in fields.
3. Use ONLY field names from the catalog for the chosen model.
4. MANDATORY domain filters — use the glossary above. In addition:
     confirmed sale orders:            [["state","=","sale"]]
     all sale orders incl. quotations: [] (no filter)
     quotations only:                  [["state","in",["draft","sent"]]]
     customer invoices (posted):       [["state","=","posted"],["move_type","=","out_invoice"]]
     vendor bills (posted):            [["state","=","posted"],["move_type","=","in_invoice"]]
     HR employees:                     [["active","=",true]]
5. For "total revenue / total sales amount" → agg="sum" on amount_total, no groupby.
6. SPECIAL QUERY PATTERNS:
   "monthly sales by customer / sales per customer per month":
     → model: sale.order, domain: [["state","=","sale"]],
       groupby: ["date_order:month","partner_id"], measures: [{{"field":"amount_total","agg":"sum"}}], chart_type: "bar"
   "sales by month / monthly sales trend / monthly total sales" (no customer):
     → model: sale.order, domain: [["state","=","sale"]],
       groupby: ["date_order:month"], measures: [{{"field":"amount_total","agg":"sum"}}], chart_type: "line"
   "revenue by customer / top customers by revenue":
     → model: sale.order, domain: [["state","=","sale"]],
       groupby: ["partner_id"], measures: [{{"field":"amount_total","agg":"sum"}}], chart_type: "bar"
   "top categories / category revenue":
     → model: sale.order.line, domain: [["order_id.state","=","sale"]],
       groupby: ["product_id"], measures: [{{"field":"price_subtotal","agg":"sum"}}], chart_type: "pie"
   "top products / best selling products / products by revenue":
     → model: sale.order.line, domain: [["order_id.state","=","sale"]],
       groupby: ["product_id"], measures: [{{"field":"product_uom_qty","agg":"sum"}},{{"field":"price_subtotal","agg":"sum"}}], chart_type: "bar"
   "products needing reorder / low stock / reorder alerts":
     → model: stock.warehouse.orderpoint, domain: [],
       fields: ["product_id","qty_on_hand","product_min_qty","product_max_qty"],
       order: "qty_on_hand asc", chart_type: "none"
   "delivery orders / pending deliveries / outgoing shipments":
     → model: stock.picking, domain: [["picking_type_code","=","outgoing"],["state","not in",["done","cancel"]]],
       fields: ["name","partner_id","state","scheduled_date"], order: "scheduled_date asc", chart_type: "none"
   "receipts / incoming shipments / purchase receipts":
     → model: stock.picking, domain: [["picking_type_code","=","incoming"],["state","not in",["done","cancel"]]],
       fields: ["name","partner_id","state","scheduled_date"], order: "scheduled_date asc", chart_type: "none"
   NOTE on stock.picking: use "picking_type_code" (not "move_type") for direction.
   NEVER use sale.order for delivery orders — always stock.picking.
   "lost CRM opportunities / lost leads / lost deals":
     → model: crm.lead, domain: [["active","=",false],["probability","=",0]],
       fields: ["name","partner_id","expected_revenue","stage_id"], chart_type: "none"
   "won CRM opportunities / won deals / closed won":
     → model: crm.lead, domain: [["active","=",false],["probability","=",100]],
       fields: ["name","partner_id","expected_revenue","stage_id"], chart_type: "none"
   NOTE on crm.lead: use active=false for archived (lost/won) leads. NEVER use is_rotting for lost/won.
   is_rotting=true means stagnant/no-activity, NOT lost. Active pipeline: [["active","=",true]].
7. chart_type: "bar" for grouped comparisons, "pie" for share/proportion, "line" for time trends, "none" if no chart.
   When groupby has BOTH a time field AND a category field → use "bar" not "line".
   When the result will be a single number (no groupby) → chart_type: "none".
8. DATE FILTERS:
   "last 90 days" / "recent" → ["date_order",">=","{date_90d}"] or ["invoice_date",">=","{date_90d}"]
   "this year" / "current year" → ["date_order",">=","{year_start}"]
   "all time" / "since beginning" → no date filter
   DEFAULT (no period mentioned): apply last-90-days for invoice/revenue queries.
9. Respond with ONLY the JSON object — no markdown, no explanation."""


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
            max_tokens=700,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw_spec = json.loads(raw)
        # Extract and log CoT reasoning before validation (never stored, just logged)
        reasoning = raw_spec.pop("reasoning", None)
        if reasoning:
            _logger.info(
                "ORM reasoning for '%s...': %s",
                user_message[:40],
                reasoning[:300],
            )
        return _validate_spec(env, raw_spec, set(available.keys()))
    except Exception as exc:
        _logger.warning("Query spec generation failed: %s", exc)
        return None


_DOMAIN_CORRECTIONS: dict[str, dict[str, str]] = {
    # LLMs confuse stock.picking's "move_type" (Shipping Policy) with direction.
    # The correct stored field for outgoing/incoming/internal is picking_type_code.
    "stock.picking": {"move_type": "picking_type_code"},
}

# Domain fields to DROP entirely for specific models (wrong field, no valid replacement).
# The validator already drops unknown fields; this catches VALID but wrong ones.
_DOMAIN_DROPS: dict[str, set[str]] = {
    # LLMs use is_rotting (stagnant) instead of active=False for lost/won CRM leads.
    # Drop is_rotting entirely — the system prompt now provides the correct pattern.
    "crm.lead": {"is_rotting"},
}

# For flat (detail) queries with no groupby/measures, guarantee these fields are present.
# Fixes 8B model omitting requested fields from the fields array despite mentioning them in reasoning.
_DEFAULT_FLAT_FIELDS: dict[str, list[str]] = {
    "hr.employee":              ["name", "department_id", "job_title"],
    "crm.lead":                 ["name", "partner_id", "expected_revenue", "stage_id"],
    "sale.order":               ["name", "partner_id", "amount_total", "state", "date_order"],
    "account.move":             ["name", "partner_id", "amount_total", "state", "invoice_date"],
    "stock.picking":            ["name", "partner_id", "state", "scheduled_date"],
    "purchase.order":           ["name", "partner_id", "amount_total", "state", "date_order"],
    "stock.warehouse.orderpoint": ["product_id", "qty_on_hand", "product_min_qty", "product_max_qty"],
}


def _unquote_domain_token(v):
    """Strip double-JSON-encoding from a domain token.

    Small LLMs sometimes emit `"\"sale\""` (double-encoded) which makes
    `state = '"sale"'` match zero rows. Recurses into lists for `in`/`not in` values.
    """
    if isinstance(v, str) and len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    if isinstance(v, (list, tuple)):
        return [_unquote_domain_token(x) for x in v]
    return v


def _autofix_domain(domain) -> list:
    """Apply LLM-output auto-fixes to a raw domain before field validation.

    Handles the most common small-LLM mistakes without rejecting the whole spec:
      A. Whole domain as JSON string → parse it
      B. Flat unwrapped triple (["state","=","sale"] not [["state","=","sale"]]) → rewrap
      C. Dict-format item ({"field":..,"operator":..,"value":..}) → convert to triple
      D. 2-element item (["active", True]) → insert "=" operator
      E. Backtick-wrapped operator (`=` → =)
      F. Double-JSON-encoded string values ("\"sale\"" → "sale")
    """
    if domain is None:
        return []

    # A — whole domain sent as a JSON-encoded string
    if isinstance(domain, str):
        try:
            domain = json.loads(domain)
        except (ValueError, TypeError):
            return []

    if not isinstance(domain, (list, tuple)):
        return []

    # B — flat unwrapped triple: first element is a non-operator string, second is also a string
    if (
        len(domain) >= 3
        and isinstance(domain[0], str)
        and domain[0] not in ("&", "|", "!")
        and isinstance(domain[1], str)
    ):
        domain = [list(domain[0:3])] + list(domain[3:])

    result = []
    for item in domain:
        # Logical operators pass through
        if isinstance(item, str) and item in ("&", "|", "!"):
            result.append(item)
            continue

        # A — item itself is a JSON-encoded string
        if isinstance(item, str) and item.strip().startswith("["):
            try:
                item = json.loads(item)
            except (ValueError, TypeError):
                _logger.debug("Skipping unparseable domain item %r", item[:80])
                continue

        # C — dict-format triple: {"field": .., "operator": .., "value": ..}
        if isinstance(item, dict):
            if {"field", "operator", "value"} <= set(item):
                item = [item["field"], item["operator"], item["value"]]
            elif len(item) == 1:
                k, v = next(iter(item.items()))
                item = [k, "=", v]
            else:
                _logger.debug("Skipping unrecognised dict domain item %r", item)
                continue

        if not isinstance(item, (list, tuple)):
            _logger.debug("Skipping non-list domain item %r", item)
            continue

        # D — 2-element item: ["active", True] → ["active", "=", True]
        if len(item) == 2 and isinstance(item[0], str):
            item = [item[0], "=", item[1]]

        if len(item) != 3:
            _logger.debug("Skipping malformed domain item %r", item)
            continue

        field, op, value = item[0], item[1], item[2]

        # E — backtick-wrapped operator: `=` → =
        if isinstance(op, str) and len(op) >= 2 and op[0] == "`" and op[-1] == "`":
            op = op[1:-1]

        # F — double-JSON-encoded string values
        field = _unquote_domain_token(field)
        op = _unquote_domain_token(op) if isinstance(op, str) else op
        value = _unquote_domain_token(value)

        if not isinstance(field, str) or not field:
            _logger.debug("Skipping domain item with non-string field %r", field)
            continue

        result.append([field, op, value])

    return result


def _validate_spec(env, spec: dict, allowed_model_names: set[str]) -> dict | None:
    """Validate every field in the spec against the live registry. Returns None if invalid."""
    model_name = spec.get("model", "")
    if model_name not in allowed_model_names:
        _logger.warning("LLM requested disallowed/unknown model %r", model_name)
        return None

    if model_name not in env:
        _logger.warning("Model %r not in registry", model_name)
        return None

    all_fields = env[model_name].fields_get(attributes=["string", "type", "store", "relation"])
    _corrections = _DOMAIN_CORRECTIONS.get(model_name, {})
    _drops = _DOMAIN_DROPS.get(model_name, set())
    # Stored fields can be used in ORDER BY and search_read; computed/non-stored cannot
    _stored_fields = {f for f, info in all_fields.items() if info.get("store", True)}

    # validate domain — auto-fix first, then field-validate
    raw_domain = spec.get("domain", [])
    autofixed = _autofix_domain(raw_domain)
    clean_domain = []
    for item in autofixed:
        if isinstance(item, str) and item in ("&", "|", "!"):
            clean_domain.append(item)
            continue
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            _logger.debug("Skipping malformed domain item %r", item)
            continue
        field_name = str(item[0])
        # Apply model-specific corrections (e.g. move_type → picking_type_code on stock.picking)
        corrected = _corrections.get(field_name.split(".")[0])
        if corrected:
            _logger.debug("Correcting domain field %r → %r on %s", field_name, corrected, model_name)
            field_name = corrected
        field_path = field_name.split(".")[0]
        if field_path not in all_fields:
            _logger.warning("Domain field %r not on %s — dropping", field_name, model_name)
            continue
        if field_path in _drops:
            _logger.debug("Dropping blacklisted domain field %r on %s", field_path, model_name)
            continue
        clean_domain.append([field_name, item[1], item[2]])

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

    # validate explicit fields list — allow any readable field (stored or computed);
    # search_read handles both, only ORDER BY requires stored fields
    fields = []
    for f in spec.get("fields", []):
        fname = str(f).split(".")[0]
        if fname in all_fields:
            fields.append(fname)

    # validate order clause — ORDER BY requires stored fields
    order = str(spec.get("order", "") or "").strip()
    if order:
        order_field = order.split()[0]
        if order_field not in _stored_fields:
            _logger.debug("Dropping non-stored order field %r on %s", order_field, model_name)
            order = ""

    raw_limit = spec.get("limit", 50)
    try:
        limit = min(int(raw_limit or 50), _MAX_LIMIT_CAP)
    except (TypeError, ValueError):
        limit = 50
    chart_type = spec.get("chart_type", "none")
    if chart_type not in ("bar", "line", "pie", "doughnut", "none"):
        chart_type = "none"

    # For flat detail queries (no groupby, no measures), ensure key fields are present.
    # Compensates for smaller LLMs that mention fields in reasoning but omit them from spec.
    if not groupby and not measures:
        for default_field in _DEFAULT_FLAT_FIELDS.get(model_name, []):
            if default_field not in fields and default_field in all_fields:
                fields.append(default_field)

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
    model_name = spec["model"]
    domain = spec["domain"]

    # Odoo silently adds active=True to all queries unless active_test=False is set.
    # If the domain explicitly filters on active (e.g. lost/archived records), bypass it.
    has_active_filter = any(
        isinstance(d, (list, tuple)) and len(d) >= 1 and str(d[0]).split(".")[0] == "active"
        for d in domain
    )
    if has_active_filter:
        Model = env[model_name].with_context(active_test=False)
    else:
        Model = env[model_name]

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
