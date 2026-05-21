import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AiChatMessage(models.Model):
    _name = "ai.chat.message"
    _description = "AI Chat Message"
    _order = "created_at asc"

    session_id = fields.Many2one(
        "ai.chat.session",
        string="Session",
        required=True,
        ondelete="cascade",
        index=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        related="session_id.user_id",
        store=True,
        index=True,
    )
    role = fields.Selection(
        [
            ("user", "User"),
            ("assistant", "Assistant"),
            ("system", "System"),
            ("tool", "Tool"),
        ],
        string="Role",
        required=True,
        index=True,
    )
    content = fields.Text(string="Content")
    tokens_in = fields.Integer(string="Input Tokens", default=0)
    tokens_out = fields.Integer(string="Output Tokens", default=0)
    chart_spec = fields.Text(string="Chart Spec (JSON)")
    created_at = fields.Datetime(
        string="Created At",
        default=fields.Datetime.now,
        index=True,
    )

    _role_check = models.Constraint(
        "CHECK(role IN ('user','assistant','system','tool'))",
        "Invalid role.",
    )

    @api.model_create_multi
    def create(self, vals_list: list[dict]) -> "AiChatMessage":
        records = super().create(vals_list)
        for record in records:
            record.session_id.last_activity = fields.Datetime.now()
        return records
