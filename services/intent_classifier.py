"""
Intent classifier — uses the router model for a cheap JSON-only call.

Intent categories:
  smalltalk   — greetings, casual conversation
  help        — how-to questions about Odoo features
  data_query  — requests for Odoo data (flat list)
  chart       — requests for Odoo data rendered as a chart
  off_topic   — jokes, personal questions, coding help, general knowledge (NOT business related)
"""
import json
import logging

from . import llm_client

_logger = logging.getLogger(__name__)

_VALID_INTENTS = {"smalltalk", "help", "data_query", "chart", "off_topic"}

_SYSTEM_PROMPT = """\
You are an intent classifier for an Odoo ERP AI assistant.
Classify the user message into exactly one of these intents:
  - smalltalk  : greetings, thanks, casual conversation ("hi", "hello", "thank you")
  - help       : how-to questions about Odoo features or ERP usage
  - data_query : ANY request for business data — numbers, counts, totals, lists, revenue, orders, invoices, leads, employees, products, customers
  - chart      : same as data_query but user explicitly mentions chart, graph, visualization, treemap, or plot
  - off_topic  : jokes, riddles, stories, general knowledge, coding, weather, sports, personal questions — ANYTHING not related to business data or Odoo ERP

IMPORTANT RULES:
- When in doubt between help and data_query, choose data_query.
- "tell me a joke", "write a poem", "what is Python?", "who is Einstein?" → ALWAYS off_topic
- "how many", "total", "show me", "revenue", "sales", "orders", "invoices", "leads" → data_query or chart
- "give me charts", "show charts", "treemap", "visualize" → chart

Examples:
  "hi" → smalltalk
  "tell me a joke" → off_topic
  "write me a poem" → off_topic
  "what is the capital of France?" → off_topic
  "help me with Python code" → off_topic
  "how do I create an invoice?" → help
  "how many sale orders?" → data_query
  "what is our total revenue?" → data_query
  "give me sales related charts" → chart
  "top categories treemap" → chart

Respond ONLY with a JSON object: {"intent": "<one of the five intents>"}"""


_OFF_TOPIC_KEYWORDS = [
    "joke", "jokes", "funny", "riddle", "riddles", "poem", "story", "stories",
    "tell me about", "who is", "what is python", "capital of", "weather",
    "sports", "movie", "movies", "song", "songs", "recipe", "recipes",
    "write a", "write me", "help me code", "coding help",
]


def _is_off_topic_by_keyword(msg: str) -> bool:
    lower = msg.lower()
    return any(kw in lower for kw in _OFF_TOPIC_KEYWORDS)


def classify(env, user_message: str) -> str:
    """
    Return one of: smalltalk | help | data_query | chart | off_topic.
    Falls back to 'smalltalk' on any error.

    Args:
        env: Odoo environment
        user_message: the raw user message text

    Returns:
        str: intent label
    """
    # Hard-coded keyword override — catches clear off-topic messages without burning an LLM call
    if _is_off_topic_by_keyword(user_message):
        return "off_topic"

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_message[:1000]},
    ]
    try:
        raw = llm_client.chat_completion_sync(
            env,
            messages,
            model_role="router",
            max_tokens=32,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
        intent = data.get("intent", "smalltalk")
        if intent not in _VALID_INTENTS:
            _logger.warning("Unknown intent %r — falling back to smalltalk", intent)
            return "smalltalk"
        return intent
    except Exception as exc:
        _logger.warning("Intent classification failed (%s) — defaulting to smalltalk", exc)
        return "smalltalk"
