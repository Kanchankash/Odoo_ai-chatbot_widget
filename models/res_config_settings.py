import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# ir.config_parameter keys
_PARAM_PROVIDER = "ai_chatbot.provider"
_PARAM_LOCAL_URL = "ai_chatbot.local_base_url"
_PARAM_LOCAL_MODEL = "ai_chatbot.local_model"
_PARAM_GROQ_KEY = "ai_chatbot.groq_api_key"
_PARAM_ROUTER_MODEL = "ai_chatbot.router_model"
_PARAM_REASONER_MODEL = "ai_chatbot.reasoner_model"
_PARAM_ALLOWED_MODELS = "ai_chatbot.allowed_model_names"
_PARAM_MAX_RPM = "ai_chatbot.max_requests_per_minute"
_PARAM_MAX_HISTORY = "ai_chatbot.max_history_messages"
_PARAM_REDACT_PII = "ai_chatbot.redact_pii"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # --- provider selection ---
    ai_chatbot_provider = fields.Selection(
        [("local", "Local (OpenAI-compatible)"), ("groq", "Groq")],
        string="AI Provider",
        default="local",
        config_parameter=_PARAM_PROVIDER,
    )

    # --- local provider ---
    ai_chatbot_local_base_url = fields.Char(
        string="Local Base URL",
        default="http://192.168.0.162:8001/v1",
        config_parameter=_PARAM_LOCAL_URL,
    )
    ai_chatbot_local_model = fields.Char(
        string="Local Model",
        default="gemma4",
        config_parameter=_PARAM_LOCAL_MODEL,
    )

    # --- groq provider ---
    ai_chatbot_groq_api_key = fields.Char(
        string="Groq API Key",
        config_parameter=_PARAM_GROQ_KEY,
    )
    ai_chatbot_router_model = fields.Char(
        string="Router Model (Groq)",
        default="llama-3.1-8b-instant",
        config_parameter=_PARAM_ROUTER_MODEL,
    )
    ai_chatbot_reasoner_model = fields.Char(
        string="Reasoner Model (Groq)",
        default="llama-3.3-70b-versatile",
        config_parameter=_PARAM_REASONER_MODEL,
    )

    # --- access control ---
    ai_chatbot_allowed_model_ids = fields.Many2many(
        "ir.model",
        string="Allowed Odoo Models",
        compute="_compute_allowed_model_ids",
        inverse="_inverse_allowed_model_ids",
    )

    # --- limits ---
    ai_chatbot_max_rpm = fields.Integer(
        string="Max Requests / Min / User",
        default=20,
        config_parameter=_PARAM_MAX_RPM,
    )
    ai_chatbot_max_history = fields.Integer(
        string="Max History Messages to LLM",
        default=12,
        config_parameter=_PARAM_MAX_HISTORY,
    )
    ai_chatbot_redact_pii = fields.Boolean(
        string="Redact PII (Groq only)",
        config_parameter=_PARAM_REDACT_PII,
    )

    # ------------------------------------------------------------------
    # Allowed models: CSV stored in ir.config_parameter, exposed as M2M
    # ------------------------------------------------------------------

    @api.depends()
    def _compute_allowed_model_ids(self):
        IrModel = self.env["ir.model"]
        param = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(_PARAM_ALLOWED_MODELS, "")
        )
        model_names = [n.strip() for n in param.split(",") if n.strip()]
        records = IrModel.search([("model", "in", model_names)])
        for setting in self:
            setting.ai_chatbot_allowed_model_ids = records

    def _inverse_allowed_model_ids(self):
        names_csv = ",".join(self.ai_chatbot_allowed_model_ids.mapped("model"))
        self.env["ir.config_parameter"].sudo().set_param(
            _PARAM_ALLOWED_MODELS, names_csv
        )

    def action_test_llm_connection(self):
        """Ping the configured LLM and display a notification with results."""
        from ..services import llm_client

        result = llm_client.ping(self.env)
        if result["ok"]:
            msg = (
                f"Connection OK — {result['latency_ms']} ms\n"
                f"Preview: {result['preview']}"
            )
            msg_type = "success"
        else:
            msg = f"Connection FAILED: {result['error']}"
            msg_type = "danger"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Chatbot — Test Connection",
                "message": msg,
                "type": msg_type,
                "sticky": False,
            },
        }
