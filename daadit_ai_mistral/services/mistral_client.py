# -*- coding: utf-8 -*-
"""HTTP client for the Mistral AI APIs (chat completions + embeddings).

Mistral's wire format is OpenAI-compatible (same ``messages`` array, same
``tools``/``tool_choice``/``tool_calls`` envelope, same ``choices`` response
shape) which keeps the mapping from Odoo's existing OpenAI plumbing simple.

Endpoints used:
    * POST /v1/chat/completions  — for ai.agent (llm_model = mistral-*)
    * POST /v1/embeddings        — for ai.embedding (embedding_model = mistral-embed)

Docs: https://docs.mistral.ai/api/
"""
import json
import logging
import random
import time
from typing import Iterable, List, Mapping, Optional, Sequence

import requests

from odoo.exceptions import UserError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)


# Chat / completion models we expose. Keep in sync with ai_agent.py's selection.
SUPPORTED_MODELS = (
    "mistral-large-latest",
    "mistral-medium-latest",
    "mistral-small-latest",
    "codestral-latest",
    "pixtral-large-latest",
    "ministral-8b-latest",
    "ministral-3b-latest",
)

# Embedding model exposed for ai.embedding. Mistral currently advertises
# ``mistral-embed`` — single model, 1024 dims.
EMBEDDING_MODEL = "mistral-embed"


# Keys that ``extra`` is allowed to set on a chat-completion payload.
# Anything outside this set is silently dropped — preventing a
# misbehaving caller from overriding ``messages`` / ``tools`` /
# ``model`` via the convenience kwarg. Source: Mistral API reference,
# fields documented as caller-controllable on /v1/chat/completions.
_ALLOWED_EXTRA_CHAT_KEYS = frozenset({
    "top_p",
    "random_seed",
    "safe_prompt",
    "response_format",
    "presence_penalty",
    "frequency_penalty",
    "n",
    "min_tokens",
    "stop",
    "prediction",
    "parallel_tool_calls",
})


def is_mistral_model(model_id: str) -> bool:
    """Return True if ``model_id`` is one we should route to Mistral."""
    if not model_id:
        return False
    return model_id in SUPPORTED_MODELS or model_id.startswith(
        ("mistral-", "codestral-", "pixtral-", "ministral-")
    )


def is_mistral_embedding_model(model_id: str) -> bool:
    return bool(model_id) and model_id == EMBEDDING_MODEL


def _stringify_error(value) -> str:
    """Coerce a JSON error field (str, list, dict, None) to a human-readable
    one-line string. Handles FastAPI's ``detail`` shape specifically and
    recurses into nested wrappers (e.g. Mistral's
    ``{"message": {"detail": [...]}}``)."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # Pydantic-style: {"loc": [...], "msg": "...", "type": "..."}
        msg = value.get("msg") or value.get("message")
        if msg:
            return _stringify_error(msg)  # recurse — msg may itself be dict/list
        # Mistral wraps validation errors as {"detail": [...]}
        if "detail" in value:
            return _stringify_error(value["detail"])
        # Generic 'error' wrapper
        if "error" in value:
            return _stringify_error(value["error"])
        return json.dumps(value)[:500]
    if isinstance(value, list):
        # FastAPI validation errors are a list of {"loc","msg","type"}
        parts = []
        for item in value:
            if isinstance(item, dict):
                loc = ".".join(str(x) for x in item.get("loc", []))
                msg = item.get("msg") or item.get("message") or ""
                if loc and msg:
                    parts.append(f"{loc}: {msg}")
                elif msg:
                    parts.append(str(msg))
                else:
                    parts.append(json.dumps(item)[:200])
            else:
                parts.append(str(item))
        return "; ".join(parts)[:500]
    return str(value)[:300]


def _redact_payload(payload):
    """Return a copy of the request payload with potentially long fields
    truncated, so logs stay readable."""
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    msgs = out.get("messages")
    if isinstance(msgs, list):
        out["messages"] = [
            {
                "role": m.get("role") if isinstance(m, dict) else "?",
                "content_len": (
                    len(m.get("content") or "") if isinstance(m, dict)
                    else len(str(m))
                ),
            }
            for m in msgs
        ]
    inputs = out.get("input")
    if isinstance(inputs, list):
        out["input"] = f"<{len(inputs)} items>"
    return out


# ---------------------------------------------------------------------------
# Retry policy for rate limits and transient upstream failures (Fase 0,
# Governance & guardrails knowledge id 173: "backoff met jitter bij
# rate-limit-fouten in plaats van retry-stormen").
#
# A request is retried when Mistral answers 429 (rate limit) or a
# transient 5xx (502/503/504), or when the connection itself fails.
# Waits grow exponentially with full jitter and honour Retry-After when
# the server sends one. Non-transient statuses (400/401/422/...) are
# never retried — they would fail identically.
# ---------------------------------------------------------------------------
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 20.0


def _retry_wait_seconds(attempt, retry_after=None):
    """Seconds to sleep before retry ``attempt`` (1-based).

    Full-jitter exponential backoff: uniform(0, base * 2^attempt),
    capped. When the server supplied a parseable Retry-After, that
    value wins (also capped) — plus a little jitter so concurrent
    workers don't stampede on the exact same second.
    """
    if retry_after:
        try:
            ra = float(retry_after)
            if ra > 0:
                return min(ra, _BACKOFF_CAP_SECONDS) + random.uniform(0, 1)
        except (TypeError, ValueError):
            pass
    return random.uniform(0, min(
        _BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** attempt)
    ))


class MistralClient:
    """Thin wrapper around requests for Mistral's API.

    Designed to be stateless and instantiated per-call from the dispatch
    overrides on ``ai.agent`` and ``ai.embedding``. The ``from_env`` factory
    pulls config from ``ir.config_parameter`` so no model env binding is
    needed at import time.
    """

    def __init__(self, api_key: str, base_url: str, timeout: int = 60,
                 env=None):
        if not api_key:
            raise UserError(_(
                "No Mistral API key is configured. Enable 'Use your own "
                "Mistral AI account' in General Settings → AI and paste your "
                "key from console.mistral.ai."
            ))
        # Defense-in-depth: even if someone tampered with
        # ir.config_parameter directly (bypassing the settings view),
        # validate the URL right before we send the key. The
        # res.config.settings constrains catch UI saves; this one
        # catches every other path.
        normalised = (base_url or "https://api.mistral.ai/v1").rstrip("/")
        if env is not None:
            from odoo.addons.daadit_ai_mistral.models.res_config_settings import (
                ResConfigSettings,
            )
            try:
                ResConfigSettings._validate_base_url(env, normalised)
            except Exception:
                # ValidationError re-raised so the user sees a clear
                # message instead of a Mistral 4xx later. We do NOT
                # log the URL — admins reading the trace shouldn't
                # learn anything they don't already know.
                raise
        # Cap timeout at the same hard ceiling as the settings view, in
        # case ir.config_parameter holds a stale unreasonable value.
        try:
            timeout = max(1, min(int(timeout or 60), 600))
        except (TypeError, ValueError):
            timeout = 60
        self.api_key = api_key
        self.base_url = normalised
        self.timeout = timeout

    # --- factory ---------------------------------------------------------

    @classmethod
    def from_env(cls, env) -> "MistralClient":
        """Build a client from ``ir.config_parameter`` values.

        URL + timeout are validated on construction; an out-of-range or
        off-allowlist URL raises ValidationError BEFORE the key is sent.
        """
        ICP = env["ir.config_parameter"].sudo()
        if ICP.get_param("daadit_ai_mistral.mistral_key_enabled") not in (
            "True",
            "1",
            True,
        ):
            raise UserError(_(
                "Mistral API key is not enabled. Toggle 'Use your own "
                "Mistral AI account' in General Settings → AI."
            ))
        api_key = ICP.get_param("daadit_ai_mistral.mistral_key", default="")
        # Fase-0 API-key-splitsing (Governance & guardrails, knowledge
        # id 173): scheduled/batch callers flag themselves through the
        # env context. When a dedicated batch key is configured, batch
        # traffic uses it — so a runaway scheduled agent can exhaust
        # its own rate limit but never the client-facing key that
        # serves Ask AI / Milo livechat. Falls back to the main key
        # when no batch key is set, so the split is opt-in.
        if env.context.get("daadit_mistral_batch"):
            batch_key = (ICP.get_param(
                "daadit_ai_mistral.mistral_key_batch", default="",
            ) or "").strip()
            if batch_key:
                api_key = batch_key
        return cls(
            api_key=api_key,
            base_url=ICP.get_param(
                "daadit_ai_mistral.mistral_base_url",
                default="https://api.mistral.ai/v1",
            ),
            timeout=int(
                ICP.get_param("daadit_ai_mistral.mistral_timeout", default="60")
                or 60
            ),
            env=env,
        )

    # --- internal HTTP helper -------------------------------------------

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps(payload)
        resp = None
        last_exc = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = requests.post(
                    url,
                    headers=headers,
                    data=body,
                    timeout=self.timeout,
                )
                last_exc = None
            except requests.RequestException as exc:
                # Connection-level failure — transient by nature.
                last_exc = exc
                resp = None
            if resp is not None and resp.status_code not in _RETRY_STATUSES:
                break
            if attempt >= _MAX_ATTEMPTS:
                break
            wait = _retry_wait_seconds(
                attempt,
                resp.headers.get("Retry-After") if resp is not None else None,
            )
            _logger.warning(
                "Mistral API transient failure on %s (%s) — retry %d/%d "
                "in %.1fs",
                path,
                resp.status_code if resp is not None else f"conn: {last_exc}",
                attempt, _MAX_ATTEMPTS - 1, wait,
            )
            time.sleep(wait)
        if resp is None:
            raise RuntimeError(
                f"Mistral request failed after {_MAX_ATTEMPTS} attempts: "
                f"{last_exc}"
            ) from last_exc

        if 400 <= resp.status_code < 500:
            try:
                err = resp.json() or {}
            except ValueError:
                err = {}

            # Mistral / FastAPI errors come back in several shapes; route
            # every candidate field through _stringify_error so dicts and
            # nested ``{"message": {"detail": [...]}}`` envelopes get
            # flattened to a readable string.
            detail = (
                _stringify_error(err.get("message"))
                or _stringify_error(err.get("error"))
                or _stringify_error(err.get("detail"))
                or resp.text[:500]
            )

            # Log enough to debug a 4xx without putting user content in
            # operational logs. We log:
            #   * status, path, attempted model, message count, tool count
            #   * the parsed ``detail`` string we already extracted
            # We do NOT log the redacted payload (it still contains
            # message lengths + roles, which is fine but noisy) or the
            # full response body — those can occasionally echo user-
            # supplied strings.
            _logger.warning(
                "Mistral API %d on %s | model=%s msgs=%d tools=%d | "
                "detail=%s",
                resp.status_code, path,
                payload.get("model") if isinstance(payload, dict) else "?",
                len(payload.get("messages") or []) if isinstance(payload, dict) else 0,
                len(payload.get("tools") or []) if isinstance(payload, dict) else 0,
                (detail or "")[:300],
            )
            raise UserError(_(
                "Mistral API rejected the request (HTTP %(status)s): %(detail)s",
                status=resp.status_code,
                detail=detail,
            ))
        if resp.status_code >= 500:
            raise RuntimeError(
                f"Mistral API server error {resp.status_code}: {resp.text[:200]}"
            )

        return resp.json()

    # --- internal HTTP GET helper ---------------------------------------

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"Mistral request failed: {exc}") from exc

        if 400 <= resp.status_code < 500:
            try:
                err = resp.json() or {}
            except ValueError:
                err = {}
            detail = _stringify_error(err.get("message") or err) or resp.text[:500]
            raise UserError(_(
                "Mistral API rejected the request (HTTP %(status)s): "
                "%(detail)s",
                status=resp.status_code,
                detail=detail,
            ))
        if resp.status_code >= 500:
            raise RuntimeError(
                f"Mistral API server error {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        return resp.json()

    def list_models(self) -> list:
        """Return available models from ``GET /v1/models``.

        Mistral returns ``{"object": "list", "data": [{"id": "...",
        "name": "...", "description": "...", "capabilities": {...}}]}``.
        We keep only chat-capable models (``capabilities.completion_chat``
        when present) and return ``{"id", "display_name"}`` dicts.
        """
        data = self._get("/models") or {}
        items = data.get("data") or []
        out = []
        seen = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = (it.get("id") or "").strip()
            if not mid or mid in seen:
                continue
            caps = it.get("capabilities") or {}
            # If capabilities are reported, keep only chat-capable models
            # (skip embedding/OCR/moderation-only ids). If absent, keep it.
            if isinstance(caps, dict) and "completion_chat" in caps \
                    and not caps.get("completion_chat"):
                continue
            seen.add(mid)
            out.append({
                "id": mid,
                "display_name": (it.get("name") or mid).strip(),
            })
        return out

    # --- chat completions -----------------------------------------------

    def chat_completion(
        self,
        model: str,
        messages: List[Mapping[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[Sequence[Mapping[str, object]]] = None,
        tool_choice: Optional[object] = None,
        stream: bool = False,
        extra: Optional[Mapping[str, object]] = None,
    ) -> dict:
        """Call ``POST /chat/completions``.

        Tool-calling (``tools``/``tool_choice``) follows the OpenAI-compatible
        format that Mistral mirrors:

            tools = [{"type": "function", "function": {"name": "...",
                       "description": "...", "parameters": {...JSON Schema...}}}]
            tool_choice = "auto" | "none" | "any" | {"type": "function",
                                                      "function": {"name": "..."}}
        """
        payload: dict = {
            "model": model,
            "messages": list(messages),
            "stream": stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = list(tools)
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if extra:
            # Allowlist so a caller can't override messages/model/tools
            # via the convenience kwarg. Unknown keys are dropped with
            # a debug log.
            for k, v in extra.items():
                if k in _ALLOWED_EXTRA_CHAT_KEYS:
                    payload[k] = v
                else:
                    _logger.debug(
                        "Mistral chat: dropped non-allowlisted extra "
                        "kwarg %r", k,
                    )

        _logger.debug(
            "Mistral chat → model=%s msgs=%d tools=%d",
            model, len(messages), len(tools or []),
        )
        return self._post("/chat/completions", payload)

    # --- embeddings ------------------------------------------------------

    def embeddings(
        self,
        inputs: Sequence[str],
        *,
        model: str = EMBEDDING_MODEL,
        encoding_format: str = "float",
    ) -> dict:
        """Call ``POST /embeddings`` and return the parsed response.

        Response shape:
            { "object": "list",
              "data": [ {"object": "embedding", "index": 0, "embedding": [...]}, ... ],
              "model": "mistral-embed",
              "usage": {"prompt_tokens": ..., "total_tokens": ...} }
        """
        if not inputs:
            return {"data": [], "usage": {}}
        payload = {
            "model": model,
            "input": list(inputs),
            "encoding_format": encoding_format,
        }
        _logger.debug("Mistral embed → model=%s inputs=%d", model, len(inputs))
        return self._post("/embeddings", payload)

    # --- convenience extractors -----------------------------------------

    @staticmethod
    def extract_text(response: Mapping[str, object]) -> str:
        """Pull the first assistant message text out of a chat response."""
        choices = response.get("choices") or []
        if not choices:
            return ""
        msg = (choices[0] or {}).get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return ""

    @staticmethod
    def extract_tool_calls(response: Mapping[str, object]) -> list:
        """Return the ``tool_calls`` list from the first choice, or [].

        Each tool_call:
            {"id": "...", "type": "function",
             "function": {"name": "...", "arguments": "<json string>"}}
        """
        choices = response.get("choices") or []
        if not choices:
            return []
        msg = (choices[0] or {}).get("message") or {}
        return list(msg.get("tool_calls") or [])

    @staticmethod
    def extract_finish_reason(response: Mapping[str, object]) -> str:
        choices = response.get("choices") or []
        if not choices:
            return ""
        return (choices[0] or {}).get("finish_reason") or ""

    @staticmethod
    def extract_usage(response: Mapping[str, object]) -> dict:
        return dict(response.get("usage") or {})

    @staticmethod
    def extract_embeddings(response: Mapping[str, object]) -> List[List[float]]:
        """Return list of vectors (one per input) in the original input order."""
        items = list(response.get("data") or [])
        # API guarantees order via ``index`` — sort defensively.
        items.sort(key=lambda d: d.get("index", 0))
        return [list(item.get("embedding") or []) for item in items]
