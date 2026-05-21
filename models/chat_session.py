import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AiChatSession(models.Model):
    _name = "ai.chat.session"
    _description = "AI Chat Session"
    _order = "last_activity desc"
    _rec_name = "name"

    name = fields.Char(
        string="Session Name",
        compute="_compute_name",
        store=True,
        readonly=False,  # allows manual rename
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        required=True,
        index=True,
        default=lambda self: self.env.user,
        ondelete="cascade",
    )
    state = fields.Selection(
        [("open", "Open"), ("archived", "Archived")],
        string="State",
        default="open",
        index=True,
    )
    message_ids = fields.One2many(
        "ai.chat.message",
        "session_id",
        string="Messages",
    )
    last_activity = fields.Datetime(
        string="Last Activity",
        index=True,
        default=fields.Datetime.now,
    )

    _user_id_not_null = models.Constraint(
        "CHECK(user_id IS NOT NULL)", "User is required."
    )

    @api.depends("message_ids.content", "message_ids.role")
    def _compute_name(self):
        for session in self:
            # Only preserve names that were manually set (not the default "New Chat")
            if session.name and session.name != "New Chat":
                continue
            first_user_msg = session.message_ids.filtered(
                lambda m: m.role == "user"
            )[:1]
            if first_user_msg:
                content = first_user_msg.content or ""
                session.name = content[:60] + ("…" if len(content) > 60 else "")
            else:
                session.name = "New Chat"

    def action_archive_session(self) -> None:
        self.ensure_one()
        self.state = "archived"

    def action_reopen_session(self) -> None:
        self.ensure_one()
        self.state = "open"
