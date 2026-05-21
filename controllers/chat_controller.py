"""
AI Chatbot HTTP controllers.

All JSON endpoints use type='jsonrpc' (Odoo 19).
The streaming endpoint uses type='http' and emits Server-Sent Events.
"""
import json
import logging
import traceback

from odoo import http
from odoo.http import request
from odoo.modules.registry import Registry

from ..services import chart_builder, guardrails, intent_classifier, llm_client, orm_query_builder

_logger = logging.getLogger(__name__)

_MAX_HISTORY = 12  # fallback if param missing
_SYSTEM_PROMPT_TEMPLATE = """\
You are an Odoo ERP business assistant. You ONLY answer questions about business data,
sales, invoices, CRM, inventory, HR, and Odoo ERP usage.
You do NOT tell jokes, write stories, or answer general knowledge questions.
Today's date: {today}.
Current user: {user_name} ({user_login}).

=== RESPONSE FORMAT (STRICTLY FOLLOW THIS) ===

For data questions WITH a table:
1. One short answer sentence — state the key finding with **bold** for the most important number or name.
2. A contextual section heading (e.g. "### Top Sales Orders", "### Low Stock & Reorder Alerts", "### Revenue by Category").
3. A Markdown pipe table. Rules:
   - Use clear human-readable column headers (Customer not partner_id, Amount not amount_total).
   - For inventory/stock queries: add a Status column using emojis:
       🚨 Critical  — stock ≤ 25% of reorder point
       ⚠️ Low Stock — stock < reorder point
       ✅ At Limit  — stock = reorder point exactly
       ✅ OK        — stock > reorder point
   - For invoice/order status: use ✅ Paid, 🔴 Overdue, 🟡 Pending, 🔵 Draft.
   - Bold the most important value in each row using **value**.
   - Right-align numeric columns.
4. **Key Takeaway:** — 2-3 sentences with actionable business insight. Include percentages, comparisons, or urgency.
   Example: "**Whey Protein Powder 1kg** is critical at only **25%** of its reorder level. Immediate replenishment is recommended to avoid stockouts."

For single-value answers (counts, totals):
- One bold sentence. Example: "The total invoiced amount for the last 90 days is **₹7,701,896.85**."
- **Key Takeaway:** one sentence explaining what this means for the business.

For greetings: briefly introduce yourself and list 5 things you can help with (sales, invoices, inventory, HR, CRM).
For how-to questions: answer concisely and professionally.
=== END FORMAT ===
"""


# PostgreSQL 15+ dropped deprecated timezone aliases; map them to canonical names
_TZ_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Ulaanbaatar": "Asia/Ulaanbaatar",
}


def _normalize_tz(tz: str) -> str:
    return _TZ_ALIASES.get(tz, tz)


def _build_system_prompt(env) -> str:
    from datetime import date

    return _SYSTEM_PROMPT_TEMPLATE.format(
        today=date.today().isoformat(),
        user_name=env.user.name,
        user_login=env.user.login,
    )


def _get_max_history(env) -> int:
    try:
        return int(
            env["ir.config_parameter"]
            .sudo()
            .get_param("ai_chatbot.max_history_messages", str(_MAX_HISTORY))
        )
    except (TypeError, ValueError):
        return _MAX_HISTORY


class AiChatbotController(http.Controller):

    # ------------------------------------------------------------------
    # Session management endpoints — type='jsonrpc' [ODOO 19]
    # ------------------------------------------------------------------

    @http.route(
        "/ai_chatbot/session/list",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def session_list(self) -> list[dict]:
        """
        Return the 20 most-recent open sessions for the current user.

        Returns:
            list of {id, name, last_activity, message_count}

        Errors:
            None — returns empty list on any error.
        """
        sessions = request.env["ai.chat.session"].search(
            [("user_id", "=", request.env.user.id), ("state", "=", "open")],
            order="last_activity desc",
            limit=20,
        )
        return [
            {
                "id": s.id,
                "name": s.name or "New Chat",
                "last_activity": s.last_activity.isoformat() if s.last_activity else None,
                "message_count": len(s.message_ids),
            }
            for s in sessions
        ]

    @http.route(
        "/ai_chatbot/session/new",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def session_new(self) -> dict:
        """
        Create and return a new chat session.

        Returns:
            {session_id: int, name: str}

        Errors:
            500 — on ORM failure.
        """
        session = request.env["ai.chat.session"].create({"name": "New Chat"})
        return {"session_id": session.id, "name": session.name}

    @http.route(
        "/ai_chatbot/session/<int:session_id>/messages",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def session_messages(self, session_id: int) -> list[dict]:
        """
        Return the full message history for a session.

        Args:
            session_id: ai.chat.session id (path param)

        Returns:
            list of {id, role, content, chart_spec, created_at}

        Errors:
            404 — session not found or not owned by current user.
        """
        session = request.env["ai.chat.session"].browse(session_id)
        if not session.exists() or session.user_id.id != request.env.user.id:
            return []
        return [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content or "",
                "chart_spec": m.chart_spec,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in session.message_ids
        ]

    @http.route(
        "/ai_chatbot/session/<int:session_id>/archive",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def session_archive(self, session_id: int) -> dict:
        """
        Archive a session.

        Args:
            session_id: ai.chat.session id

        Returns:
            {ok: bool}
        """
        session = request.env["ai.chat.session"].browse(session_id)
        if session.exists() and session.user_id.id == request.env.user.id:
            session.action_archive_session()
            return {"ok": True}
        return {"ok": False}

    @http.route(
        "/ai_chatbot/session/<int:session_id>/rename",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def session_rename(self, session_id: int, name: str = "") -> dict:
        """
        Rename a session.

        Args:
            session_id: ai.chat.session id
            name: new name string

        Returns:
            {ok: bool, name: str}
        """
        session = request.env["ai.chat.session"].browse(session_id)
        if not session.exists() or session.user_id.id != request.env.user.id:
            return {"ok": False, "name": ""}
        name = (name or "").strip()[:120]
        if name:
            session.name = name
        return {"ok": True, "name": session.name}

    @http.route(
        "/ai_chatbot/test_connection",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def test_connection(self) -> dict:
        """
        Ping the configured LLM and return latency + preview.

        Returns:
            {ok: bool, latency_ms: float, preview: str, error: str|None}
        """
        if not request.env.user.has_group("base.group_system"):
            return {"ok": False, "error": "Admin only"}
        return llm_client.ping(request.env)

    # ------------------------------------------------------------------
    # Streaming chat endpoint — type='http', SSE
    # ------------------------------------------------------------------

    @http.route(
        "/ai_chatbot/chat/stream",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def chat_stream(self, **kwargs):
        """
        Accept a user message and stream the assistant response as SSE.

        Request body (JSON): {session_id: int, content: str}

        SSE frames:
          event: token   data: <text delta>
          event: error   data: <error message>
          event: done    data: {message_id: int, chart_spec: str|null}

        Errors:
            event: error — rate limit, injection, or LLM failure.
        """
        env = request.env

        # parse body
        try:
            body = json.loads(request.httprequest.data or b"{}")
        except json.JSONDecodeError:
            return self._sse_error("Invalid JSON body")

        session_id = body.get("session_id")
        content = (body.get("content") or "").strip()

        if not content:
            return self._sse_error("Empty message")

        # -- rate limit --
        allowed, reason = guardrails.check_rate_limit(env, env.user.id)
        if not allowed:
            return self._sse_error(reason)

        # -- injection check --
        safe, reason = guardrails.check_injection(content)
        if not safe:
            return self._sse_error("Message was blocked by the safety filter.")

        # -- PII redaction for outbound --
        outbound_content = content
        if guardrails.should_redact(env):
            outbound_content = guardrails.redact_pii(content)

        # -- resolve session --
        if session_id:
            session = env["ai.chat.session"].browse(session_id)
            if not session.exists() or session.user_id.id != env.user.id:
                return self._sse_error("Session not found")
        else:
            session = env["ai.chat.session"].create({"name": "New Chat"})

        # -- persist user message --
        env["ai.chat.message"].create(
            {
                "session_id": session.id,
                "role": "user",
                "content": content,
            }
        )
        # commit so the user message is visible immediately
        env.cr.commit()

        # -- build LLM messages --
        max_history = _get_max_history(env)
        system_prompt = _build_system_prompt(env)
        llm_messages = [{"role": "system", "content": system_prompt}]

        history = session.message_ids.filtered(lambda m: m.role in ("user", "assistant"))
        # take last max_history, excluding the one we just created
        for msg in history[-(max_history + 1) : -1]:
            llm_messages.append({"role": msg.role, "content": msg.content or ""})

        # add the current user message (redacted version for external providers)
        llm_messages.append({"role": "user", "content": outbound_content})

        # -- intent classification --
        intent = intent_classifier.classify(env, outbound_content)
        _logger.info("Chat intent for user %s: %s", env.user.login, intent)

        # -- off-topic: refuse immediately without calling the LLM --
        if intent == "off_topic":
            refusal = (
                "I'm your Odoo business assistant — I can only help with business data, "
                "sales, invoices, CRM, inventory, and ERP-related questions.\n\n"
                "Please ask something related to your business data."
            )

            def _refusal_gen():
                dbname = env.cr.dbname
                msg_id = None
                with Registry(dbname).cursor() as new_cr:
                    new_env = env(cr=new_cr)
                    msg = new_env["ai.chat.message"].create({
                        "session_id": session.id,
                        "role": "assistant",
                        "content": refusal,
                        "chart_spec": None,
                    })
                    msg_id = msg.id
                yield _sse_frame("token", refusal)
                yield _sse_frame("done", json.dumps({"message_id": msg_id, "chart_spec": None}))

            return request.make_response(
                _refusal_gen(),
                headers=[
                    ("Content-Type", "text/event-stream"),
                    ("Cache-Control", "no-cache"),
                    ("X-Accel-Buffering", "no"),
                    ("Connection", "keep-alive"),
                ],
            )

        # -- data / chart queries --
        chart_spec_json: str | None = None
        context_addition = ""

        if intent in ("data_query", "chart"):
            spec = orm_query_builder.build_query_spec(env, outbound_content, intent)
            _logger.info("Query spec: %s", spec)
            if spec:
                result = orm_query_builder.execute_query(env, spec)
                _logger.info("Query result rows=%d columns=%s", len(result.get("rows", [])), result.get("columns"))
                if result.get("rows"):
                    # Auto-promote to chart if result has numeric columns and multiple rows
                    if result.get("chart_type", "none") == "none":
                        result["chart_type"] = _auto_chart_type(result)

                    # build chart spec first so we know whether to suppress table output
                    is_chart = result.get("chart_type", "none") != "none" or intent == "chart"
                    if is_chart:
                        chart_cfg = chart_builder.build(result)
                        if chart_cfg:
                            chart_spec_json = json.dumps(chart_cfg)

                    user_tz = _normalize_tz(env.user.tz or "UTC")
                    table_text = _format_table(result["columns"], result["rows"], user_tz)
                    if chart_spec_json:
                        context_addition = (
                            f"\n\n=== LIVE DATABASE RESULTS ===\n{table_text}\n"
                            "=== END OF DATABASE RESULTS ===\n\n"
                            "STRICT RULES:\n"
                            "1. A chart has already been rendered in the UI from this data.\n"
                            "2. Use ONLY the numbers above — do NOT use your training knowledge for figures.\n"
                            "3. Do NOT output markdown tables, ASCII charts, or additional numbers.\n"
                            "4. Write exactly 1-3 sentences summarising the key insight from the data above."
                        )
                    else:
                        context_addition = (
                            f"\n\n=== LIVE DATABASE RESULTS ===\n{table_text}\n"
                            "=== END OF DATABASE RESULTS ===\n\n"
                            "STRICT RULES:\n"
                            "1. Use ONLY the numbers from the database results above.\n"
                            "2. Do NOT use your training knowledge to invent or adjust figures.\n"
                            "3. Answer concisely based solely on the data above."
                        )

        if context_addition:
            llm_messages[-1]["content"] += context_addition

        # -- stream response --
        def generate():
            assembled = []
            try:
                for token in llm_client.chat_completion_stream(env, llm_messages):
                    assembled.append(token)
                    yield _sse_frame("token", token)

                full_text = "".join(assembled)

                # persist assistant message in a fresh cursor so client disconnect
                # doesn't leave a dangling transaction
                dbname = env.cr.dbname
                uid = env.uid
                with Registry(dbname).cursor() as new_cr:
                    new_env = env(cr=new_cr)
                    msg = new_env["ai.chat.message"].create(
                        {
                            "session_id": session.id,
                            "role": "assistant",
                            "content": full_text,
                            "chart_spec": chart_spec_json,
                        }
                    )
                    # last_activity is updated by ai.chat.message.create via write

                yield _sse_frame(
                    "done",
                    json.dumps(
                        {"message_id": msg.id, "chart_spec": chart_spec_json}
                    ),
                )
            except Exception as exc:
                _logger.error("Streaming error: %s\n%s", exc, traceback.format_exc())
                yield _sse_frame("error", str(exc))

        return request.make_response(
            generate(),
            headers=[
                ("Content-Type", "text/event-stream"),
                ("Cache-Control", "no-cache"),
                ("X-Accel-Buffering", "no"),
                ("Connection", "keep-alive"),
            ],
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sse_error(message: str):
        def gen():
            yield _sse_frame("error", message)

        return request.make_response(
            gen(),
            headers=[
                ("Content-Type", "text/event-stream"),
                ("Cache-Control", "no-cache"),
            ],
        )


def _sse_frame(event: str, data: str) -> str:
    # SSE spec: newlines inside data must be sent as separate "data:" lines,
    # otherwise the frontend line-splitter drops everything after the first \n.
    encoded = "\n".join(f"data: {line}" for line in data.split("\n"))
    return f"event: {event}\n{encoded}\n\n"


def _auto_chart_type(result: dict) -> str:
    """
    Automatically pick a chart type for grouped results with numeric measures.
    Returns "none" for flat detail lists (too many mixed-type columns).
    """
    rows = result.get("rows", [])
    columns = result.get("columns", [])
    if len(rows) < 2 or not columns:
        return "none"

    # Count strictly numeric vs non-numeric columns (skip col 0 which is the label)
    numeric_count = 0
    non_numeric_count = 0
    for i in range(1, len(columns)):
        vals = [r[i] for r in rows if i < len(r) and r[i] is not None]
        if vals and all(isinstance(v, (int, float)) for v in vals):
            numeric_count += 1
        else:
            non_numeric_count += 1

    if numeric_count == 0:
        return "none"
    # Only auto-promote if it's clearly a grouped/aggregate result:
    # more numeric cols than non-numeric, or col names end with _sum/_count/_avg
    agg_suffixes = ("_sum", "_count", "_avg", "_max", "_min", "count", "sum")
    is_aggregate = any(str(c).endswith(agg_suffixes) for c in columns)
    if is_aggregate or (numeric_count >= non_numeric_count and len(columns) <= 3):
        return "bar"
    return "none"


_FIELD_LABELS = {
    "partner_id": "Customer",
    "amount_total": "Amount",
    "amount_untaxed": "Subtotal",
    "date_order": "Order Date",
    "invoice_date": "Invoice Date",
    "create_date": "Created",
    "name": "Reference",
    "state": "Status",
    "expected_revenue": "Expected Revenue",
    "stage_id": "Stage",
    "department_id": "Department",
    "job_title": "Job Title",
    "product_id": "Product",
    "categ_id": "Category",
    "quantity_sold": "Qty Sold",
    "amount_total_sum": "Total Amount",
    "id_count": "Count",
    "qty_on_hand": "Current Stock",
    "product_min_qty": "Reorder Point",
    "product_max_qty": "Max Qty",
    "scheduled_date": "Scheduled Date",
    "picking_type_id": "Operation Type",
    "list_price": "Price",
}


def _format_table(columns: list, rows: list, tz_name: str = "UTC") -> str:
    from datetime import datetime, timezone
    import pytz

    try:
        user_tz = pytz.timezone(tz_name)
    except Exception:
        user_tz = pytz.utc

    def _fmt(v):
        if v is None:
            return ""
        if isinstance(v, datetime):
            utc = v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
            # If stored as midnight UTC it's a date-only field — show date only
            if utc.hour == 0 and utc.minute == 0 and utc.second == 0:
                return utc.astimezone(user_tz).strftime("%Y-%m-%d")
            return utc.astimezone(user_tz).strftime("%Y-%m-%d %H:%M:%S")
        # Odoo returns datetimes as strings like "2026-05-21 07:37:49"
        if isinstance(v, str) and len(v) == 19 and v[10] == " ":
            try:
                dt = datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                    return dt.astimezone(user_tz).strftime("%Y-%m-%d")
                return dt.astimezone(user_tz).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        return str(v)

    headers = [_FIELD_LABELS.get(str(c), str(c).replace("_", " ").title()) for c in columns]
    lines = [" | ".join(headers)]
    lines.append("-|-".join("-" * max(len(h), 3) for h in headers))
    for row in rows:
        lines.append(" | ".join(_fmt(v) for v in row))
    return "\n".join(lines)
