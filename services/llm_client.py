"""
OpenAI-compatible LLM client with streaming support.

Reads configuration from ir.config_parameter at call time so settings changes
take effect immediately without a server restart.
"""
import json
import logging
import re
import threading
import time
from collections.abc import Generator
from typing import Any

import requests

_logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_URL = "http://localhost:11434/v1"   # Ollama default endpoint
_DEFAULT_LOCAL_MODEL = "mistral:latest"          # Chat / streaming responses
_DEFAULT_ROUTER_MODEL = "tinyllama:latest"       # Fast intent routing + summarization
_DEFAULT_REASONER_MODEL = "qwen2.5-coder:7b"     # Structured JSON query generation (pull if missing)
_DEFAULT_GROQ_URL = "https://api.groq.com/openai/v1"
_TIMEOUT_CONNECT = 10
_TIMEOUT_READ = 120

# If the model goes silent mid-stream for this long, the watchdog thread closes
# the connection. vLLM streaming ignores requests' read timeout past initial connect.
_STREAM_IDLE_TIMEOUT_S = 180

# Chat-template / reasoning-channel tokens that leak when vLLM's SSE parser
# doesn't fully match the model's template (observed with vLLM + Gemma/gemma4
# + hermes chat template). We strip these from the cumulative buffer so users
# never see raw `<|channel|>thought<channel|>` wrappers in the chat bubble.
_CHANNEL_OPEN = re.compile(r'<\|channel\|?>\s*\w+\s*(?:\n|\r\n)?', re.IGNORECASE)
_CHANNEL_CLOSE = re.compile(r'<\|?channel\|>', re.IGNORECASE)
_CHATML_TOKEN = re.compile(
    r'<\|(?:im_start|im_end|start_header_id|end_header_id|eot_id|eos'
    r'|endoftext|begin_of_text|end_of_text)\|>',
    re.IGNORECASE,
)
_GENERIC_PIPE_TOKEN = re.compile(r'<\|/?[A-Za-z0-9_.:-]+\|>')


def _strip_model_tokens(text: str) -> str:
    """Strip vLLM/Gemma chat-template tokens from a cumulative streamed buffer.

    Applied to the full accumulated text (not per-chunk) because markers often
    straddle SSE chunk boundaries — e.g. `<|channel>` arrives in one chunk and
    `thought` in the next.
    """
    if not text:
        return ''
    text = _CHANNEL_OPEN.sub('', text)
    text = _CHANNEL_CLOSE.sub('', text)
    text = _CHATML_TOKEN.sub('', text)
    text = _GENERIC_PIPE_TOKEN.sub('', text)
    return text


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
    # Retry up to 3 times on rate-limit (429) with exponential backoff
    last_resp = None
    for attempt in range(3):
        resp = requests.post(
            url,
            headers=_headers(api_key),
            json=payload,
            timeout=(_TIMEOUT_CONNECT, _TIMEOUT_READ),
        )
        last_resp = resp
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 3))
            wait = min(retry_after, 5)  # cap at 5s for fast fallback
            _logger.warning("Rate limited by %s — retrying in %ds (attempt %d/3)", url, wait, attempt + 1)
            if attempt < 2:
                time.sleep(wait)
                continue
            # All retries exhausted — fall back to local model if available
            local_url = params.get("chat_base_url", "").rstrip("/")
            if local_url and local_url != base_url:
                _logger.warning("Groq rate limit exhausted — falling back to local model for %s role", model_role)
                fallback_payload = dict(payload, model=params["chat_model"])
                fallback_url = f"{local_url}/chat/completions"
                fb_resp = requests.post(
                    fallback_url,
                    headers=_headers(params["chat_api_key"]),
                    json=fallback_payload,
                    timeout=(_TIMEOUT_CONNECT, _TIMEOUT_READ),
                )
                fb_resp.raise_for_status()
                fb_data = fb_resp.json()
                try:
                    return fb_data["choices"][0]["message"]["content"]
                except (KeyError, IndexError) as exc:
                    raise ValueError(f"Unexpected fallback LLM response shape: {fb_data}") from exc
        resp.raise_for_status()
        break
    data = last_resp.json()
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
    Streaming completion. Yields cleaned token delta strings.

    Improvements over a naive implementation:
    - Forces UTF-8 decoding on the response (prevents ISO-8859-1 mojibake on
      curly quotes / em-dashes from endpoints that omit charset in Content-Type).
    - Strips vLLM/Gemma chat-template tokens (`<|channel|>`, `<|im_end|>`, etc.)
      from the cumulative buffer so they never appear in the chat bubble.
    - Watchdog thread closes a stuck stream after _STREAM_IDLE_TIMEOUT_S of silence
      (vLLM streaming bypasses requests' read timeout past the initial connect).

    Yields:
        str: new cleaned characters since the last yield (delta, not full text).
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

    try:
        resp = requests.post(
            url,
            headers=_headers(params["chat_api_key"]),
            json=payload,
            stream=True,
            timeout=(_TIMEOUT_CONNECT, _STREAM_IDLE_TIMEOUT_S + 30),
        )
    except requests.exceptions.RequestException as exc:
        _logger.warning("LLM stream connect failed (%s): %s", url, exc)
        return

    resp.raise_for_status()
    # Force UTF-8 so curly quotes / em-dashes aren't decoded as ISO-8859-1 mojibake.
    resp.encoding = "utf-8"

    # Watchdog: close a silent stream after _STREAM_IDLE_TIMEOUT_S of inactivity.
    last_progress = [time.monotonic()]
    watchdog_stop = threading.Event()
    watchdog_timed_out = [False]

    def _watchdog():
        while not watchdog_stop.is_set():
            if watchdog_stop.wait(3):
                return
            if time.monotonic() - last_progress[0] > _STREAM_IDLE_TIMEOUT_S:
                watchdog_timed_out[0] = True
                try:
                    resp.close()
                except Exception:
                    pass
                return

    wd = threading.Thread(target=_watchdog, name="ai-stream-watchdog", daemon=True)
    wd.start()

    # Accumulate full text so token-stripping regexes can match markers that
    # straddle SSE chunk boundaries. Yield only the NEW cleaned characters.
    accumulated = ""
    last_clean_len = 0

    try:
        for raw_line in resp.iter_lines(decode_unicode=True):
            last_progress[0] = time.monotonic()
            if not raw_line:
                continue
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].lstrip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                piece = chunk["choices"][0]["delta"].get("content", "")
                if not piece:
                    continue
                accumulated += piece
                cleaned = _strip_model_tokens(accumulated)
                if len(cleaned) > last_clean_len:
                    yield cleaned[last_clean_len:]
                    last_clean_len = len(cleaned)
            except (json.JSONDecodeError, KeyError, IndexError):
                _logger.debug("Skipping unparseable SSE chunk: %s", data_str[:200])
    except (requests.exceptions.RequestException, OSError) as exc:
        if not watchdog_timed_out[0]:
            _logger.warning("LLM stream interrupted: %s", exc)
    finally:
        watchdog_stop.set()
        try:
            resp.close()
        except Exception:
            pass

    if watchdog_timed_out[0]:
        _logger.warning(
            "LLM stream idle timeout after %ds — vLLM worker may be stuck",
            _STREAM_IDLE_TIMEOUT_S,
        )


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
