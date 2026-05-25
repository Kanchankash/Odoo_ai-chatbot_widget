"""
Build Chart.js v4 config dicts from query result sets.
Supports: bar, line, pie, doughnut.
"""
import logging
from typing import Any

_logger = logging.getLogger(__name__)

_PALETTE = [
    "#4f86c6", "#f0a500", "#2ca25f", "#e34a33",
    "#756bb1", "#de2d26", "#31a354", "#3182bd",
]

# Human-readable column labels — mirrors _FIELD_LABELS in chat_controller
_FIELD_LABELS = {
    "partner_id": "Customer",
    "amount_total": "Amount",
    "amount_total_sum": "Total Amount",
    "amount_untaxed": "Subtotal",
    "price_subtotal": "Subtotal",
    "price_subtotal_sum": "Revenue",
    "date_order": "Order Date",
    "invoice_date": "Invoice Date",
    "create_date": "Created",
    "scheduled_date": "Scheduled Date",
    "name": "Reference",
    "state": "Status",
    "stage_id": "Stage",
    "expected_revenue": "Expected Revenue",
    "expected_revenue_sum": "Pipeline Value",
    "department_id": "Department",
    "job_id": "Job Position",
    "job_title": "Job Title",
    "product_id": "Product",
    "categ_id": "Category",
    "product_uom_qty": "Qty",
    "product_uom_qty_sum": "Qty Sold",
    "id_count": "Count",
    "list_price": "Price",
}


def _label(col: str) -> str:
    """Convert a raw field name to a human-readable chart label."""
    col_str = str(col)
    if col_str in _FIELD_LABELS:
        return _FIELD_LABELS[col_str]
    # Strip aggregation suffixes added by ORM (e.g. amount_total:sum → amount_total_sum)
    base = col_str.replace(":sum", "").replace(":count", "").replace(":avg", "")
    if base in _FIELD_LABELS:
        return _FIELD_LABELS[base]
    return col_str.replace("_", " ").replace(":", " ").title()


def _is_numeric_col(col_idx: int, rows: list) -> bool:
    vals = [r[col_idx] for r in rows if col_idx < len(r) and r[col_idx] is not None]
    if not vals:
        return False
    return all(isinstance(v, (int, float)) for v in vals)


def _fmt_label(val) -> str:
    """Format a row[0] value as a readable chart label."""
    if val is None:
        return ""
    s = str(val)
    # Raw datetime strings like "2026-05-01 00:00:00" → "May 2026"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            from datetime import datetime
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
            return dt.strftime("%b %Y")
        except ValueError:
            pass
    # Tuple from Many2one: "(42, 'Customer Name')" → "Customer Name"
    if s.startswith("(") and "," in s:
        try:
            inner = s.strip("()").split(",", 1)[1].strip().strip("'\"")
            return inner if inner else s
        except Exception:
            pass
    return s


def build(query_result: dict) -> dict[str, Any] | None:
    chart_type = query_result.get("chart_type", "none")
    if chart_type == "none":
        return None

    columns = query_result.get("columns", [])
    rows = query_result.get("rows", [])

    if not columns or not rows:
        return None

    # Format first column as readable labels
    labels = [_fmt_label(row[0]) for row in rows]

    # If all X labels are identical the chart conveys nothing — skip it
    if len(set(labels)) <= 1 and len(rows) > 1:
        return None

    # Detect multi-dimension groupby (e.g. month × customer).
    # When the same X label repeats many times, it means there are multiple
    # series that Chart.js cannot represent as a simple bar/line chart.
    # Guard: if max(label occurrences) > 2 AND unique labels × 3 < total rows → skip chart.
    unique_labels = set(labels)
    if len(rows) > 0 and len(unique_labels) > 1:
        max_repeat = max(labels.count(lbl) for lbl in unique_labels)
        if max_repeat > 2 and len(unique_labels) * 3 < len(rows):
            _logger.debug(
                "Skipping chart: multi-dimension data (%d rows, %d unique X labels, max repeat %d)",
                len(rows), len(unique_labels), max_repeat,
            )
            return None

    if chart_type in ("pie", "doughnut"):
        return _pie_config(chart_type, columns, rows, labels)
    return _cartesian_config(chart_type, columns, rows, labels)


def _cartesian_config(chart_type: str, columns: list, rows: list, labels: list) -> dict:
    datasets = []
    for col_idx in range(1, len(columns)):
        # Skip non-numeric columns — don't plot dates, names, IDs as bars
        if not _is_numeric_col(col_idx, rows):
            continue
        values = []
        for row in rows:
            val = row[col_idx] if col_idx < len(row) else 0
            try:
                values.append(float(val) if val is not None else 0.0)
            except (TypeError, ValueError):
                values.append(0.0)
        color = _PALETTE[len(datasets) % len(_PALETTE)]
        datasets.append({
            "label": _label(columns[col_idx]),
            "data": values,
            "backgroundColor": color + "cc",
            "borderColor": color,
            "borderWidth": 1,
        })

    if not datasets:
        return None

    return {
        "type": chart_type,
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "responsive": True,
            "plugins": {
                "legend": {"position": "top"},
                "tooltip": {"mode": "index"},
            },
            "scales": {
                "y": {
                    "beginAtZero": True,
                    "ticks": {
                        "precision": 0,
                        "callback": "formatAmount",
                    },
                }
            },
        },
    }


def _pie_config(chart_type: str, columns: list, rows: list, labels: list) -> dict:
    # Find first numeric column after col 0 for values
    value_col = 1
    for i in range(1, len(columns)):
        if _is_numeric_col(i, rows):
            value_col = i
            break

    values = []
    for row in rows:
        val = row[value_col] if len(row) > value_col else 0
        try:
            values.append(float(val) if val is not None else 0.0)
        except (TypeError, ValueError):
            values.append(0.0)

    colors = [_PALETTE[i % len(_PALETTE)] for i in range(len(labels))]

    return {
        "type": chart_type,
        "data": {
            "labels": labels,
            "datasets": [{
                "label": _label(columns[value_col]),
                "data": values,
                "backgroundColor": colors,
                "borderWidth": 1,
            }],
        },
        "options": {
            "responsive": True,
            "plugins": {"legend": {"position": "right"}},
        },
    }
