"""
OpenAI-compatible LLM client with streaming support.

Reads configuration from ir.config_parameter at call time so settings changes
take effect immediately without a server restart.
"""
import json
import logging
from collections.abc import Generator
from typing import Any

import requests

_logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_URL = "http://192.168.0.162:8001/v1"
_DEFAULT_LOCAL_MODEL = "gemma4"
_DEFAULT_ROUTER_MODEL = "llama-3.1-8b-instant"
_DEFAULT_REASONER_MODEL = "llama-3.3-70b-versatile"
_DEFAULT_GROQ_URL = "https://api.groq.com/openai/v1"
_TIMEOUT_CONNECT = 10
_TIMEOUT_READ = 120


def _get_params(env) -> dict[str, Any]:
    """
    Build connection params.

    Hybrid mode (best accuracy + free):
      When provider=local AND a Groq API key is configured, router and reasoner
      calls go to Groq (accurate JSON structured output) while the streaming
      chat response uses the local model (private, no token cost).

    Full Groq mode: provider=groq — all calls go to Groq.
    Full local mode: provider=local, no Groq key — all calls go to local model.
    """
    get = env["ir.config_parameter"].sudo().get_param
    provider = get("ai_chatbot.provider", "local")
    groq_api_key = get("ai_chatbot.groq_api_key", "").strip()

    if provider == "groq":
        return {
            "provider": "groq",
            "chat_base_url": _DEFAULT_GROQ_URL,
            "chat_api_key": groq_api_key,
            "chat_model": get("ai_chatbot.reasoner_model", _DEFAULT_REASONER_MODEL),
            "structured_base_url": _DEFAULT_GROQ_URL,
            "structured_api_key": groq_api_key,
            "router_model": get("ai_chatbot.router_model", _DEFAULT_ROUTER_MODEL),
            "reasoner_model": get("ai_chatbot.reasoner_model", _DEFAULT_REASONER_MODEL),
        }

    local_url = get("ai_chatbot.local_base_url", _DEFAULT_LOCAL_URL).rstrip("/")
    local_model = get("ai_chatbot.local_model", _DEFAULT_LOCAL_MODEL)

    if groq_api_key:
        # Hybrid: Groq for structured JSON calls, local model for streaming chat
        _logger.debug("Hybrid mode: Groq router/reasoner + local chat (%s)", local_model)
        return {
            "provider": "hybrid",
            "chat_base_url": local_url,
            "chat_api_key": "local",
            "chat_model": local_model,
            "structured_base_url": _DEFAULT_GROQ_URL,
            "structured_api_key": groq_api_key,
            "router_model": get("ai_chatbot.router_model", _DEFAULT_ROUTER_MODEL),
            "reasoner_model": get("ai_chatbot.reasoner_model", _DEFAULT_REASONER_MODEL),
        }

    # Full local: all calls go to local model
    return {
        "provider": "local",
        "chat_base_url": local_url,
        "chat_api_key": "local",
        "chat_model": local_model,
        "structured_base_url": local_url,
        "structured_api_key": "local",
        "router_model": local_model,
        "reasoner_model": local_model,
    }


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def chat_completion_sync(
    env,
    messages: list[dict],
    model_role: str = "default",
    max_tokens: int = 512,
    response_format: dict | None = None,
    temperature: float = 0.2,
) -> str:
    """
    Non-streaming completion. Returns the assistant message content string.

    Args:
        env: Odoo environment
        messages: list of {"role": str, "content": str} dicts
        model_role: "router" | "reasoner" | "default"
        max_tokens: maximum tokens to generate
        response_format: optional {"type": "json_object"} for JSON mode
        temperature: sampling temperature

    Returns:
        str: assistant message content

    Raises:
        requests.HTTPError: on non-2xx response
        ValueError: on unexpected response shape
    """
    params = _get_params(env)
    # router + reasoner use the structured endpoint; default uses chat endpoint
    if model_role in ("router", "reasoner"):
        base_url = params["structured_base_url"]
        api_key = params["structured_api_key"]
        model = params.get(f"{model_role}_model") or params["reasoner_model"]
    else:
        base_url = params["chat_base_url"]
        api_key = params["chat_api_key"]
        model = params["chat_model"]

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format

    url = f"{base_url}/chat/completions"
    resp = requests.post(
        url,
        headers=_headers(api_key),
        json=payload,
        timeout=(_TIMEOUT_CONNECT, _TIMEOUT_READ),
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Unexpected LLM response shape: {data}") from exc


def chat_completion_stream(
    env,
    messages: list[dict],
    max_tokens: int = 1024,
    temperature: float = 0.7,
) -> Generator[str, None, None]:
    """
    Streaming completion. Yields token delta strings.
    Uses the default (reasoner/local) model for final answers.

    Args:
        env: Odoo environment
        messages: list of {"role": str, "content": str} dicts
        max_tokens: maximum tokens to generate
        temperature: sampling temperature

    Yields:
        str: token delta text fragments

    Raises:
        requests.HTTPError: on non-2xx response
    """
    params = _get_params(env)
    model = params["chat_model"]
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    url = f"{params['chat_base_url']}/chat/completions"
    with requests.post(
        url,
        headers=_headers(params["chat_api_key"]),
        json=payload,
        stream=True,
        timeout=(_TIMEOUT_CONNECT, _TIMEOUT_READ),
    ) as resp:
        resp.raise_for_status()
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line: str = (
                raw_line.decode("utf-8")
                if isinstance(raw_line, bytes)
                else raw_line
            )
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                return
            try:
                chunk = json.loads(data_str)
                delta = chunk["choices"][0]["delta"].get("content", "")
                if delta:
                    yield delta
            except (json.JSONDecodeError, KeyError, IndexError):
                _logger.debug("Skipping unparseable SSE chunk: %s", data_str)


def ping(env) -> dict[str, Any]:
    """
    Send a minimal completion to verify connectivity.

    Returns:
        dict with keys: ok (bool), latency_ms (float), preview (str), error (str|None)
    """
    import time

    t0 = time.monotonic()
    try:
        params = _get_params(env)
        content = chat_completion_sync(
            env,
            [{"role": "user", "content": "Say OK in one word."}],
            max_tokens=10,
        )
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        mode = params["provider"]
        return {"ok": True, "latency_ms": latency_ms, "preview": f"[{mode}] {content[:50]}", "error": None}
    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return {"ok": False, "latency_ms": latency_ms, "preview": "", "error": str(exc)}
