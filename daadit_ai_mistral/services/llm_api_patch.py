# -*- coding: utf-8 -*-
"""Monkey-patch ``odoo.addons.ai.utils.llm_api_service.LLMApiService`` so
``provider='mistral'`` is supported end-to-end.

Why a monkey-patch instead of a subclass: stock Enterprise's
``ai.agent._generate_response`` constructs ``LLMApiService`` directly
by importing the class — it doesn't go through a factory or a registry
that we could override via ``_inherit``. The only practical hook is to
replace ``__init__`` and ``request_llm`` on the class object itself.

Traceback that motivates this (v3.5.2 staging):

    File "enterprise/ai/models/ai_agent.py", line 530, in _generate_response
        llm_response = LLMApiService(env=self.env,
                                     provider=self._get_provider()
                                     ).request_llm(...)
    File "enterprise/ai/utils/llm_api_service.py", line 96, in __init__
        raise NotImplementedError(f"Unsupported provider: {self.provider}")
    NotImplementedError: Unsupported provider: mistral

After this patch:

* ``__init__`` short-circuits the provider validation when
  ``provider == 'mistral'``, storing ``env`` and ``provider`` on the
  instance so subsequent calls work.
* ``request_llm`` dispatches through ``MistralClient.chat_completion``
  for Mistral, returning the **raw** Mistral response. Mistral's API is
  OpenAI-compatible (same ``choices[0].message.content``,
  ``choices[0].message.tool_calls``, ``usage``), so any caller that
  unpacks an OpenAI-shaped dict will keep working unchanged.
* All other providers fall through to the original ``__init__`` /
  ``request_llm`` unchanged.

If a future Enterprise version reshapes the expected response, the
:func:`_log_first_call_args` helper logs the actual signature observed
on the first Mistral call — that's the line to grep for if you need to
adapt the wrapper.
"""
import json
import logging
import re
import time
import uuid

from .mistral_client import (
    EMBEDDING_MODEL,
    MistralClient,
    MistralUnavailable,
    is_mistral_embedding_model,
    is_mistral_model,
)
from . import tool_dispatch

try:
    # Optional: daadit_ai_claude is not a hard dependency, so a
    # deployment without it must still import this module. When it is
    # installed, its request path doubles as our fallback provider.
    from odoo.addons.daadit_ai_claude.services import (
        llm_api_patch as claude_patch,
    )
except ImportError:  # pragma: no cover - depends on installed addons
    claude_patch = None

_logger = logging.getLogger(__name__)

_PATCHED = False

_ASK_AGENT_NAME_KEYS = (
    "agent_name", "agent", "name", "target",
    "target_agent", "agent_id", "specialist",
)


def _delegate_target_name(tool_call):
    """Best-effort: pull the delegated-to agent name out of a router
    tool call's arguments. Returns a str or None; never raises."""
    try:
        raw = (tool_call.get("function") or {}).get("arguments") or "{}"
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if isinstance(data, dict):
            for key in _ASK_AGENT_NAME_KEYS:
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


# Fallback provider (taak 709). "claude" (default) or "off".
_FALLBACK_ICP = "daadit_ai_mistral.fallback_provider"
_FALLBACK_MODEL_ICP = "daadit_ai_mistral.fallback_model"
_FALLBACK_MODEL_DEFAULT = "claude-sonnet-4-6"


def _fallback_model(env):
    if env is None:
        return _FALLBACK_MODEL_DEFAULT
    try:
        name = env["ir.config_parameter"].sudo().get_param(
            _FALLBACK_MODEL_ICP,
        )
    except Exception:  # noqa: BLE001
        return _FALLBACK_MODEL_DEFAULT
    return (name or "").strip() or _FALLBACK_MODEL_DEFAULT


def _fallback_enabled(env):
    """True when an unavailable Mistral may be answered by Claude."""
    if claude_patch is None:
        return False
    if env is None:
        return False
    try:
        mode = env["ir.config_parameter"].sudo().get_param(
            _FALLBACK_ICP, "claude",
        )
    except Exception:  # noqa: BLE001
        return False
    return (mode or "").strip().lower() in ("claude", "anthropic", "on", "1")


def _request_with_fallback(api_self, args, kwargs):
    """Run the Mistral turn; hand it to Claude when Mistral is down.

    Only ``MistralUnavailable`` triggers the switch — connection
    failure, 5xx, or a rate limit that survived every retry. A 4xx is
    the request being wrong, and re-sending a wrong request to another
    provider just burns a second bill.

    The model name is replaced, because a Mistral model id means
    nothing to Anthropic; everything else (messages, tools, the agent
    on the threadlocal) carries over unchanged, so the colleague keeps
    the same instructions and the same tools.
    """
    try:
        return _request_llm_mistral(api_self, *args, **kwargs)
    except MistralUnavailable as exc:
        env = getattr(api_self, "env", None)
        if not _fallback_enabled(env):
            raise
        model = _fallback_model(env)
        _logger.warning(
            "daadit_ai_mistral.llm_api_patch: Mistral niet beschikbaar "
            "(%s) — deze beurt wordt beantwoord door %s", exc, model,
        )
        claude_kwargs = dict(kwargs)
        claude_kwargs["model"] = model
        # Positional Mistral model ids are dropped: Claude sniffs
        # positional strings for a model name and would ignore a
        # Mistral id anyway, but leaving it in reads as a bug later.
        claude_args = tuple(
            a for a in args
            if not (isinstance(a, str) and is_mistral_model(a))
        )
        return claude_patch._request_llm_claude(
            api_self, *claude_args, **claude_kwargs
        )


def _diag_nonmistral_delegation(api_self, where, kwargs):
    """Persist a structure-only record to ir.logging every time our
    patch hands an LLM call to stock because provider != 'mistral'.

    This is the exact point where a call that SHOULD have been Mistral
    slips through to stock's openai/google path (and, with no openai
    key, raises the 'No API key set for provider openai' dialog). We
    log the provider value, model kwarg, and a short caller stack —
    names only, no message content — so the slip's origin is
    deterministic instead of guessed. (v19.0.4.2.2, diagnostic.)
    """
    try:
        import sys
        provider = getattr(api_self, "provider", None)
        model = None
        for k in _MODEL_KEYS:
            if kwargs.get(k):
                model = kwargs.get(k)
                break
        frames = []
        frame = sys._getframe(2)
        depth = 0
        while frame is not None and depth < 18:
            frames.append(frame.f_code.co_name)
            frame = frame.f_back
            depth += 1
        env = getattr(api_self, "env", None)
        if env is not None:
            env["ir.logging"].sudo().create({
                "name": "daadit_ai_mistral.nonmistral",
                "type": "server",
                "level": "WARNING",
                "message": (
                    "NONMISTRAL_DELEGATION where=%s provider=%r model=%r "
                    "router_depth=%s stack=%s" % (
                        where, provider, model,
                        getattr(tool_dispatch.router_state, "depth", 0),
                        frames,
                    )
                )[:8000],
                "path": "daadit_ai_mistral",
                "func": "_diag_nonmistral_delegation",
                "line": "0",
            })
            env.cr.commit()
    except Exception:  # noqa: BLE001
        pass


def patch_llm_api_service() -> bool:
    """Install the Mistral-aware patch on ``LLMApiService``.

    Returns ``True`` if the patch is now in place (whether installed by
    this call or already present from a prior call), ``False`` if the
    target class couldn't be imported.
    """
    global _PATCHED
    if _PATCHED:
        return True

    try:
        from odoo.addons.ai.utils import llm_api_service as _llm_mod
    except ImportError:
        _logger.warning(
            "daadit_ai_mistral.llm_api_patch: "
            "odoo.addons.ai.utils.llm_api_service is not importable; "
            "the chat dispatch patch will not be active. Mistral chat "
            "won't work until the import path matches."
        )
        return False

    LLMApiService = getattr(_llm_mod, "LLMApiService", None)
    if LLMApiService is None:
        _logger.warning(
            "daadit_ai_mistral.llm_api_patch: LLMApiService class not "
            "found in odoo.addons.ai.utils.llm_api_service."
        )
        return False

    if getattr(LLMApiService, "_daadit_mistral_patched", False):
        _PATCHED = True
        return True

    _logger.info(
        "daadit_ai_mistral.llm_api_patch: applying patch to %s.%s "
        "(id=%s)",
        LLMApiService.__module__, LLMApiService.__name__,
        id(LLMApiService),
    )

    original_init = LLMApiService.__init__
    original_request_llm = getattr(LLMApiService, "request_llm", None)
    # AI Fields (enterprise/ai_fields/tools.py) and any other low-level
    # caller use `_request_llm` (underscore-prefixed), NOT the public
    # `request_llm`. We must patch both, otherwise force_provider works
    # but the internal dispatcher still falls into stock's
    # `raise NotImplementedError()` at the bottom of `_request_llm`.
    original_request_llm_internal = getattr(LLMApiService, "_request_llm", None)
    # `ai.agent._build_rag_context` embeds the user's prompt by calling
    # `get_embedding` on this class directly — it never goes through
    # `ai.embedding`, where our `_generate_embeddings` override handles
    # the indexing side. See `_patched_get_embedding` below.
    original_get_embedding = getattr(LLMApiService, "get_embedding", None)

    def _resolve_force_provider(env):
        """Read the system parameter that forces a non-stock provider.

        Returns the provider string ("mistral", etc.) when force mode is
        active, or None to mean "leave provider alone".

        Force mode values:
            "off" or absent  -> no override (stock behaviour)
            "auto"           -> override only when caller passed no provider
                                 or provider == "openai" (the stock default).
                                 Caller-specified "google" is respected.
            "always"         -> override every call regardless of what the
                                 caller asked for. Use with care: this also
                                 reroutes embeddings, which Mistral may not
                                 support for every model.

        Reading the parameter is wrapped in a try/except because __init__ is
        sometimes hit outside a normal request context (e.g. early registry
        load, scheduled actions before cron-cursor is fully ready); the safe
        fallback is to skip the override.
        """
        if env is None:
            return None
        try:
            icp = env["ir.config_parameter"].sudo()
            mode = (icp.get_param(
                "daadit_ai_mistral.force_provider") or "").strip().lower()
            forced = (icp.get_param(
                "daadit_ai_mistral.force_provider_name")
                or "mistral").strip().lower()
        except Exception:  # noqa: BLE001
            return None
        if mode in ("off", "", "no", "0", "false"):
            return None
        if mode in ("auto", "always") and forced:
            return (mode, forced)
        return None

    def _patched_init(api_self, env=None, provider=None, *args, **kwargs):
        # Step 1: honor an existing "mistral" call exactly like before.
        if provider == "mistral":
            api_self.env = env
            api_self.provider = "mistral"
            return None

        # Step 2: optional force-mode. Lets a non-agent code path (AI
        # Fields button, automation server actions, embeddings controller,
        # the bare `LLMApiService(request.env)` in agent.py) be redirected
        # to Mistral without us having to hook each call site.
        resolved = _resolve_force_provider(env)
        if resolved:
            mode, forced = resolved
            should_override = (
                mode == "always"
                or (mode == "auto" and provider in (None, "openai"))
            )
            if should_override and forced == "mistral":
                _logger.info(
                    "daadit_ai_mistral.llm_api_patch: force_provider=%s — "
                    "rerouting LLMApiService(provider=%r) to 'mistral'",
                    mode, provider,
                )
                api_self.env = env
                api_self.provider = "mistral"
                return None
            if should_override and forced != "mistral":
                # Future-proof: lets the user point force_provider_name at
                # some other custom provider another addon installs.
                provider = forced

        # Step 3: stock path.
        return original_init(api_self, env=env, provider=provider,
                             *args, **kwargs)

    def _patched_request_llm(api_self, *args, **kwargs):
        if getattr(api_self, "provider", None) != "mistral":
            _diag_nonmistral_delegation(api_self, "request_llm", kwargs)
            if original_request_llm is None:
                raise AttributeError(
                    "LLMApiService.request_llm not found on stock; "
                    "cannot delegate non-Mistral call."
                )
            return original_request_llm(api_self, *args, **kwargs)
        # SEC H3: clear the threadlocal that ``ai.agent._get_provider``
        # set just before constructing LLMApiService — so it cannot
        # leak into a subsequent unrelated request handled by the same
        # worker thread. Stock workers normally process one request at
        # a time, but gevent / async setups can interleave; the
        # try/finally pattern is the cheap way to make state-bleed
        # impossible.
        # A top-level turn with an observer attached gets bracketed so
        # the observer sees both ends even when the turn raises. Sub-runs
        # (depth > 0) are part of their parent's turn and must not open a
        # second one.
        hooks = getattr(tool_dispatch, "turn_hooks", None)
        depth = 0
        try:
            depth = getattr(tool_dispatch.router_state, "depth", 0) or 0
        except Exception:  # noqa: BLE001
            depth = 0
        agent = getattr(tool_dispatch.current_agent, "record", None)

        if hooks is not None and not depth:
            try:
                hooks.begin(agent)
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "daadit_ai_mistral: turn observer begin raised — "
                    "continuing without it"
                )
            answer = None
            try:
                answer = _request_with_fallback(api_self, args, kwargs)
                return answer
            finally:
                try:
                    hooks.end(agent, answer)
                except Exception:  # noqa: BLE001
                    _logger.exception(
                        "daadit_ai_mistral: turn observer end raised — "
                        "the answer was already returned"
                    )
                # Sluit de denkstappen-lijst af zodat de frontend de
                # "bezig"-status stopt. Eén keer per top-level beurt.
                _notify_step(agent, "Klaar", kind="done", done=True)
                try:
                    tool_dispatch.current_agent.record = None
                except Exception:  # noqa: BLE001
                    pass

        try:
            return _request_with_fallback(api_self, args, kwargs)
        finally:
            try:
                tool_dispatch.current_agent.record = None
            except Exception:  # noqa: BLE001
                pass

    def _patched_request_llm_internal(api_self, *args, **kwargs):
        """Low-level dispatcher patch.

        Stock's `_request_llm` body (regel 540 in
        enterprise/ai/utils/llm_api_service.py):

            if self.provider == 'openai':
                return self._request_llm_openai(...)
            if self.provider == 'google':
                return self._request_llm_google(...)
            raise NotImplementedError()

        AI Fields (enterprise/ai_fields/tools.py:182) calls this internal
        method directly with kwargs `llm_model=`, `system_prompts=`,
        `user_prompts=`, etc., and unpacks the result as
        `response, *__ = llm_api._request_llm(...)` — so the caller
        expects a tuple where the first element is the list of text
        responses. Our Mistral pipeline already returns a list of
        strings; we wrap it in a tuple so unpacking works.
        """
        if getattr(api_self, "provider", None) != "mistral":
            _diag_nonmistral_delegation(api_self, "_request_llm", kwargs)
            if original_request_llm_internal is None:
                raise AttributeError(
                    "LLMApiService._request_llm not found on stock; "
                    "cannot delegate non-Mistral call."
                )
            return original_request_llm_internal(api_self, *args, **kwargs)
        try:
            adapted = _request_with_fallback(api_self, args, kwargs)
        finally:
            try:
                tool_dispatch.current_agent.record = None
            except Exception:  # noqa: BLE001
                pass
        # Stock `_request_llm_openai`/`_google` return a 4-tuple
        # (response, to_call, next_inputs, request_token_usage) — see
        # `_request_llm_openai_helper` at line 367. We return the same
        # arity so any caller (AI Fields uses `response, *__ = ...`,
        # request_llm-loop uses 4-tuple) can unpack uniformly.
        return (adapted, [], [], {})

    def _patched_get_embedding(api_self, *args, **kwargs):
        """Query-side embedding dispatch.

        Stock `get_embedding` builds its headers via `_get_base_headers`
        → `_get_api_token`, whose provider table holds only openai and
        google, so it raises `UserError("Unsupported provider 'mistral'")`
        for us. That is what breaks chatting with a Mistral agent that
        has sources: `_build_rag_context` embeds the prompt before any
        chat call happens. Scheduled runs never touch RAG, which is why
        they kept working while the chat window did not.

        Mistral's /embeddings response is already OpenAI-shaped, so
        callers doing `response['data'][0]['embedding']` need no change.
        """
        if getattr(api_self, "provider", None) != "mistral":
            _diag_nonmistral_delegation(api_self, "get_embedding", kwargs)
            if original_get_embedding is None:
                raise AttributeError(
                    "LLMApiService.get_embedding not found on stock; "
                    "cannot delegate non-Mistral call."
                )
            return original_get_embedding(api_self, *args, **kwargs)

        inputs = kwargs.get("input", args[0] if args else None)
        if inputs is None:
            inputs = []
        elif isinstance(inputs, str):
            inputs = [inputs]
        else:
            inputs = list(inputs)

        # `dimensions` is accepted and ignored: mistral-embed has a fixed
        # 1024-wide output, and the stored vectors were written by
        # ai_embedding._daadit_call_mistral_embeddings, which ignores it
        # too. Honouring it here would query a different vector space
        # than the one the sources were indexed into.
        model = kwargs.get("model") or EMBEDDING_MODEL
        if not is_mistral_embedding_model(model):
            _logger.info(
                "daadit_ai_mistral.llm_api_patch: get_embedding asked for "
                "model=%r on provider='mistral'; using %r instead so the "
                "query matches the indexed vectors.",
                model, EMBEDDING_MODEL,
            )
            model = EMBEDDING_MODEL

        client = MistralClient.from_env(api_self.env)
        response = client.embeddings(inputs, model=model)

        try:
            usage = MistralClient.extract_usage(response)
            api_self.env["daadit_ai_mistral.usage"].sudo().record_usage(
                kind="embedding",
                model=model,
                prompt_tokens=usage.get("total_tokens")
                or usage.get("prompt_tokens")
                or 0,
                completion_tokens=0,
                iterations=1,
                has_tools=False,
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral: embedding usage row creation failed"
            )
        return response

    LLMApiService.__init__ = _patched_init
    if original_get_embedding is not None:
        LLMApiService.get_embedding = _patched_get_embedding
    if original_request_llm is not None:
        LLMApiService.request_llm = _patched_request_llm
    if original_request_llm_internal is not None:
        LLMApiService._request_llm = _patched_request_llm_internal
    LLMApiService._daadit_mistral_patched = True
    LLMApiService._daadit_original_init = original_init
    LLMApiService._daadit_original_request_llm = original_request_llm
    LLMApiService._daadit_original_request_llm_internal = original_request_llm_internal
    LLMApiService._daadit_original_get_embedding = original_get_embedding

    _PATCHED = True
    _logger.info(
        "daadit_ai_mistral.llm_api_patch: LLMApiService.__init__/"
        "request_llm/_request_llm/get_embedding patched to support "
        "provider='mistral' (class: %s.%s)",
        LLMApiService.__module__, LLMApiService.__name__,
    )
    return True


_MODEL_KEYS = ("model", "llm_model", "model_name", "name")
# 'inputs' / 'input' are stock Enterprise's actual names — confirmed
# from a v3.6.2 first-call log (kwargs included {'inputs', 'tools',
# 'temperature'}). 'messages'/'msgs'/etc. kept for forward-compat in
# case a future Enterprise release renames it.
_MESSAGE_KEYS = ("inputs", "input", "messages", "msgs",
                 "prompt_messages", "history", "conversation",
                 "chat_history")
_TOOLS_KEYS = ("tools", "functions")
_TOOL_CHOICE_KEYS = ("tool_choice", "function_call")
_TEMPERATURE_KEYS = ("temperature", "temp")
_MAX_TOKENS_KEYS = ("max_tokens", "max_completion_tokens", "max_new_tokens")
_PROMPT_KEYS = ("prompt", "user_prompt", "user_message")
_SYSTEM_KEYS = ("system_prompt", "system", "system_message", "instructions")
_BODY_KEYS = ("body", "payload", "data", "params", "request_body",
              "request", "kwargs")


def _normalize_message_dict(d):
    """Coerce one message-like dict into Mistral's ``{role, content}``
    shape. Handles a few common alternative key names that stock might
    use internally."""
    role = (
        d.get("role")
        or d.get("author")
        or d.get("from")
        or "user"
    )
    content = (
        d.get("content")
        if d.get("content") is not None else (
            d.get("text")
            or d.get("message")
            or d.get("body")
            or ""
        )
    )
    if not isinstance(content, (str, list)):
        content = str(content)
    return {"role": role, "content": content}


def _adapt_response_to_text_messages(mistral_response):
    """Convert a Mistral chat-completions response into the shape stock
    Enterprise's ``_post_ai_response`` actually consumes: a flat **list
    of strings**, where each string is one assistant message body.

    Stock iterates the return value of ``request_llm`` and passes each
    item *directly* to ``markdown()`` — the v3.6.7 staging traceback
    confirms this::

        File "ai_agent.py", line 417, in _generate_response_for_channel
            self._post_ai_response(channel, message)
        File "ai_agent.py", line 497, in _post_ai_response
            raw_html = markdown(message, …)
        File "markdown2.py", line 334, in convert
            text = str(text, 'utf-8')
        TypeError: decoding to str: dict found

    No intermediate frame extracts text from a structured item; the
    iterated value goes straight to ``markdown()``. So each list entry
    must be a plain ``str``.

    Tool calls are intentionally dropped from this adapter for now —
    stock's path through ``_post_ai_response`` is text-only, and
    function-call execution is a separate plumbing job (we'd need to
    intercept earlier in the dispatch and feed a tool-result message
    back, which requires mirroring stock's tool-execution loop).
    Without that, returning ``function_call`` placeholders here would
    just crash the same way.

    Pixtral content is a list of ``{type, text}`` parts — those parts
    are concatenated into one string per choice.
    """
    if not isinstance(mistral_response, dict):
        return []

    out = []
    for choice in mistral_response.get("choices") or []:
        msg = (choice.get("message") if isinstance(choice, dict) else None) or {}
        content = msg.get("content")

        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict)
            )

        if text and text.strip():
            out.append(text)
    return out


def _normalize_tools(tools):
    """Convert various tool shapes to Mistral's expected envelope.

    Mistral (and the OpenAI tool-calling spec it mirrors) require::

        [{"type": "function",
          "function": {"name": "...", "description": "...",
                       "parameters": {<JSON schema>}}}]

    Stock Enterprise's ``request_llm`` was observed (v3.6.4 staging
    log) to pass a flat list of strings — just function names like
    ``["ir_actions_server_open_menu_kanban", ...]`` — which Mistral
    rejects with HTTP 422 ("Input should be a valid dictionary or
    object to extract fields from").

    We accept three shapes and normalize all three:

      * Already-shaped dict ``{"type":"function","function":{...}}`` →
        passed through unchanged.
      * Bare function dict ``{"name": ..., "description": ..., ...}`` →
        wrapped in the ``{type, function}`` envelope.
      * Plain string (just a name) → wrapped as a minimal function def
        with empty parameters and a synthesised description from the
        snake-cased name.

    Returns ``None`` when the input is empty / not a list, so the caller
    can omit the kwarg from the Mistral payload entirely.
    """
    if not tools:
        return None

    # Stock has been observed (v3.6.5 staging) to pass ``tools`` as a
    # dict rather than a list — handle both shapes before iterating.
    if isinstance(tools, dict):
        if "tools" in tools and isinstance(tools["tools"], (list, tuple)):
            # Wrapper: {"tools": [...], "choice": ...}
            tools = tools["tools"]
        else:
            # Mapping: {name: def_or_meta} — convert each key into a
            # synthesised function dict that the loop below will then
            # wrap in the {type, function} envelope.
            converted = []
            for name, val in tools.items():
                if isinstance(val, dict):
                    converted.append({"name": name, **val})
                elif isinstance(val, str):
                    converted.append({"name": name, "description": val})
                else:
                    converted.append({"name": name})
            tools = converted

    if not isinstance(tools, (list, tuple)):
        return None

    out = []
    for tool in tools:
        if isinstance(tool, dict):
            if "function" in tool and "type" in tool:
                # Already in Mistral/OpenAI tool-call envelope.
                out.append(tool)
            elif "name" in tool:
                # Bare function dict — wrap it.
                out.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description") or "",
                        "parameters": tool.get("parameters") or {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        },
                    },
                })
            # else: unrecognised dict shape — drop it
        elif isinstance(tool, str):
            # Just a name. Synthesise a minimal but valid def so Mistral
            # at least sees the function exists. Description is the
            # name with underscores → spaces, parameters are an empty
            # object — the model can still call the function but won't
            # have a real schema until stock's tool-builder is mirrored.
            out.append({
                "type": "function",
                "function": {
                    "name": tool,
                    "description": tool.replace("_", " ").strip(),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            })
    return out or None


def _normalize_messages(value):
    """Convert arbitrary stock input shapes into a Mistral-compatible
    list of ``{role, content}`` dicts.

    Accepted shapes:
      * ``str`` → ``[{"role":"user","content":str}]``
      * ``dict`` (single message) → ``[normalized]``
      * ``list[str]`` → each wrapped as user message
      * ``list[dict]`` → each normalized
      * empty / unrenderable → ``None``
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if isinstance(value, dict):
        return [_normalize_message_dict(value)]
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        out = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                out.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                out.append(_normalize_message_dict(item))
            elif hasattr(item, "_name"):
                # Odoo recordset entry — probably a message or chunk
                # record. Try to extract text-ish fields.
                txt = (
                    getattr(item, "content", None)
                    or getattr(item, "body", None)
                    or getattr(item, "text", None)
                    or ""
                )
                role = getattr(item, "role", None) or getattr(item, "author", None) or "user"
                out.append({"role": str(role), "content": str(txt)})
        return out or None
    return None


def _extract_from_dict(d, model, messages, tools, tool_choice,
                       temperature, max_tokens):
    """Try to pull the standard parameters out of a dict (could be a
    direct kwargs dict, a positional body, or a nested ``body=`` kwarg).

    Returns updated tuple of (model, messages, tools, tool_choice,
    temperature, max_tokens).
    """
    if not isinstance(d, dict):
        return model, messages, tools, tool_choice, temperature, max_tokens
    for k in _MODEL_KEYS:
        if not model and k in d:
            model = d.get(k)
            break
    for k in _MESSAGE_KEYS:
        if not messages and k in d:
            cand = d.get(k)
            normalized = _normalize_messages(cand)
            if normalized:
                messages = normalized
                break
    for k in _TOOLS_KEYS:
        if tools is None and k in d:
            tools = d.get(k)
            break
    for k in _TOOL_CHOICE_KEYS:
        if tool_choice is None and k in d:
            tool_choice = d.get(k)
            break
    for k in _TEMPERATURE_KEYS:
        if temperature is None and k in d:
            temperature = d.get(k)
            break
    for k in _MAX_TOKENS_KEYS:
        if max_tokens is None and k in d:
            max_tokens = d.get(k)
            break
    return model, messages, tools, tool_choice, temperature, max_tokens


def _format_access_denied_message(info):
    """Render an admin-policy denial as a clean English markdown message.

    We deliberately keep the source in English and translate via Mistral
    just before returning to the user (see ``_translate_to_chat_language``
    below). That way the message follows the *chat* language — the
    language the user is typing in — rather than the user's Odoo
    account-language preference. A user typing in Dutch gets a Dutch
    reply even when their account is set to en_US.
    """
    requested = info.get("model_name") or "?"
    allowed = info.get("allowed_models") or []
    blocked = info.get("blocked_models") or []

    lines = [
        "**🔒 Access blocked by your administrator.**",
        "",
        f"This AI agent is not permitted to query the model `{requested}`.",
    ]

    if requested in blocked:
        lines.append(
            "It is on the **block list** for this agent — admins have "
            "explicitly chosen to keep this data out of AI responses."
        )
    elif allowed:
        lines.append("")
        lines.append(
            "The administrator has restricted this agent to the "
            "following models:"
        )
        # markdown2 needs a blank line before the first list item, or
        # the bullets get concatenated into one paragraph.
        lines.append("")
        for m in sorted(allowed):
            lines.append(f"- `{m}`")
        lines.append("")
        lines.append(
            "If you need information about something else, contact "
            "your system administrator to request access — they can "
            "extend the agent's permitted models."
        )
    else:
        lines.append("")
        lines.append(
            "Contact your system administrator if you believe this "
            "is wrong."
        )

    return "\n".join(lines)


def _agent_steps():
    """De gedeelde denkstappen-laag (labels + bus), of ``None``.

    De labels en het bus-verkeer wonen sinds v19.0.6.17.0 in
    ``daadit_ai_agent_schedule.services.agent_steps`` zodat Mistral, Loes
    en Claude precies dezelfde vaste regels sturen. Lui geïmporteerd en
    best-effort: draait deze provider zonder die module, dan is er
    simpelweg geen voortgangsregel.
    """
    try:
        from odoo.addons.daadit_ai_agent_schedule.services import agent_steps
        return agent_steps
    except Exception:  # noqa: BLE001
        return None


def _turn_id():
    """Het uuid van de lopende top-level beurt, gedeeld met de
    usage-/run-administratie zodat live-stappen en achteraf-rapport
    hetzelfde id dragen. Sub-runs erven het via de threadlocal."""
    try:
        return getattr(tool_dispatch.router_state, "turn_uuid", None)
    except Exception:  # noqa: BLE001
        return None


_SMALLTALK_OPENERS = (
    "hoi", "hallo", "hai", "hey", "hi", "yo", "dag", "goedemorgen",
    "goedemiddag", "goedenavond", "goeiemorgen", "hee",
)
_SMALLTALK_PHRASES = (
    "hoe gaat het", "hoe is het", "alles goed", "hoe gaat ie",
    "bedankt", "dank je", "dankje", "dank u", "thanks", "top",
    "goed bezig", "fijne dag", "tot morgen", "welterusten",
)


def _is_smalltalk(conversation):
    """True when the user's last message is a greeting or a pleasantry.

    Used to skip the forced tool call on the first turn. Kept
    deliberately narrow: short message AND a recognised opener or
    phrase. A long message that happens to start with "hoi" is a real
    question with a polite opening, not small talk.
    """
    tekst = ""
    for m in reversed(conversation or []):
        if isinstance(m, dict) and m.get("role") == "user":
            tekst = str(m.get("content") or "").strip().lower()
            break
    if not tekst or len(tekst) > 60:
        return False
    if any(p in tekst for p in _SMALLTALK_PHRASES):
        return True
    woorden = [w.strip(".,!?;:") for w in tekst.split()]
    return bool(woorden) and woorden[0] in _SMALLTALK_OPENERS


def _step_text(tc):
    """Vaste, PII-vrije NL-regel voor een op handen zijnde tool-aanroep.

    Dunne wrapper om de gedeelde labelset zodat elke provider dezelfde
    tekst stuurt. Valt de gedeelde module weg, dan een neutrale regel.
    """
    steps = _agent_steps()
    if steps is None:
        return "Ik voer een actie uit"
    return steps.label_for_tool_call(tc)


def _safe_name(raw):
    """Alleen een naam-achtige token doorlaten (via de gedeelde vangrail)."""
    steps = _agent_steps()
    return steps.safe_agent_name(raw) if steps is not None else ""


def _notify_step(agent, text, depth=0, kind="think", done=False):
    """Push one thinking step to the chatting user, immediately.

    Dunne wrapper om de gedeelde ``agent_steps.emit`` (labels + bus wonen
    sinds v19.0.6.17.0 in ``daadit_ai_agent_schedule``). Best-effort: een
    fout hier mag nooit een chatbeurt breken.
    """
    steps = _agent_steps()
    if steps is None:
        return
    steps.emit(agent, text, turn_id=_turn_id(), depth=depth, kind=kind,
               done=done)


def _colleagues_allowed_for_model(agent, model_name):
    """Return active agents whose own scope permits ``model_name``.

    Used to turn a scope denial into a routing instruction. An agent
    qualifies when the model is not on its block list AND either its
    allow list is empty (= unrestricted) or contains the model.

    Ordered so that a *restricted* agent that explicitly lists the model
    comes first: an agent configured for exactly this model is a better
    owner than an unrestricted generalist that merely happens to be
    allowed everything.
    """
    if not agent or not model_name:
        return []
    try:
        Agent = agent.env["ai.agent"].sudo()
        specific = []
        generic = []
        for cand in Agent.search([]):
            if cand.id == agent.id:
                continue
            blocked = set(cand.daadit_blocked_model_ids.mapped("model"))
            if model_name in blocked:
                continue
            allowed = set(cand.daadit_allowed_model_ids.mapped("model"))
            if model_name in allowed:
                specific.append(cand)
            elif not allowed:
                generic.append(cand)
        return specific + generic
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.llm_api_patch: could not determine which "
            "colleagues may query %s", model_name,
        )
        return []


def _scope_redirect_result(agent, denial, colleagues):
    """Build the tool result that replaces a scope denial at depth 0.

    Deliberately contains NO polished, user-presentable prose. The old
    behaviour ended the turn and printed "🔒 Access blocked by your
    administrator" straight to the user; the concierge's instruction to
    route instead was unreachable, because the loop had already broken.
    Now the model gets a machine instruction and nothing worth pasting.
    """
    namen = [c.name for c in colleagues[:3] if c.name]
    eerste = namen[0] if namen else ""
    return {
        "ok": False,
        "error": "OUT_OF_SCOPE",
        "model": denial.get("model_name") or "?",
        "route_to": eerste,
        "route_alternatives": namen[1:],
        "instruction": (
            "Dit model valt buiten jouw bereik, maar wel binnen dat van "
            "een collega. Dit is GEEN eindantwoord en GEEN fout van de "
            "gebruiker: toon deze melding niet en vraag ook niet om "
            "rechten. Roep nu ir_actions_server_ask_agent aan met "
            "agent_name='%s' en question = de volledige oorspronkelijke "
            "vraag, inclusief alle context. Geef daarna het antwoord van "
            "die collega door." % (eerste or "de juiste specialist")
        ),
    }


# A language reference shorter than this carries no reliable language
# signal — "?", "Mark?" and "ok" have all been mistaken for French.
MIN_LANG_REF_LETTERS = 12


def _letter_count(text):
    """Number of alphabetic characters in ``text`` (any script)."""
    return sum(1 for ch in text if ch.isalpha())


def _translate_to_chat_language(client, model, ref_messages, text):
    """Use Mistral itself to translate ``text`` into whatever language
    the conversation is in.

    Mistral mirrors the user's language by default, so we feed it the
    user's previous messages as a language reference, then ask for a
    same-language version of our English source string. Markdown
    formatting and backticked identifiers are preserved by instruction.

    On any error (network, parsing) we return the source string
    unchanged — falling back to English is preferable to an empty
    response. Best-effort by design.

    Costs roughly one extra API round-trip per access-denial / empty
    response — usually a few hundred tokens. The trade-off is worth it
    for any team chatting in a non-English language.
    """
    if not text or not ref_messages:
        return text

    # Pull the most recent user-authored messages as the language
    # reference. Messages too short to carry a language ("?", "Mark?",
    # "ok") are skipped: prod showed a Dutch conversation whose denial
    # message came back in French because the turn before it was a bare
    # "?". Up to three qualifying messages are joined so the language is
    # unambiguous even for terse chatters.
    user_texts = [
        m["content"] for m in ref_messages
        if isinstance(m, dict)
        and m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m.get("content").strip()
    ]
    usable = [
        t for t in user_texts if _letter_count(t) >= MIN_LANG_REF_LETTERS
    ]
    if not usable:
        return text
    last_user = "\n\n".join(usable[-3:])[-800:]

    prompt = [
        {
            "role": "system",
            "content": (
                "You are a translator. Translate the user's input into "
                "the SAME language as the reference text below. "
                "Strictly preserve markdown: ** for bold, ` for inline "
                "code, - for bullet items, blank lines between paragraphs, "
                "and emoji. Do NOT translate identifiers inside backticks "
                "(like `account.move`, `res.partner`). Output only the "
                "translation — no preamble, no quotes, no explanation."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Reference text (target language):\n"
                f"---\n{last_user[:800]}\n---\n\n"
                f"Translate this:\n{text}"
            ),
        },
    ]
    try:
        response = client.chat_completion(
            model=model,
            messages=prompt,
            temperature=0.0,
        )
        translated = MistralClient.extract_text(response)
        translated = (translated or "").strip()
        return translated or text
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.llm_api_patch: translation via Mistral "
            "failed — falling back to English source"
        )
        return text


def _slug_tool_name(action_name):
    """Map an ``ir.actions.server`` display name to its dispatch slug.

    Mirror of the helper in ``daadit_ai_agent_schedule`` (kept local —
    this module must not depend on the schedule module):
    ``"AI: Read group"`` → ``ir_actions_server_read_group``.
    """
    import re as _re
    name = (action_name or "").strip()
    if ":" in name:
        name = name.split(":", 1)[1]
    slug = _re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return "ir_actions_server_" + slug if slug else ""


def _resolve_agent(api_self, request_kwargs=None):
    """Find the ``ai.agent`` record that triggered this chat call.

    Two paths in priority order:

    1. **Threadlocal set by our ``_get_provider`` override.** Works when
       MRO routes through our class. But our global-module patch on
       ``odoo.addons.ai.utils.llm_providers.get_provider`` short-circuits
       stock's ``_get_provider`` which may bypass our override entirely.

    2. **``env.context['discuss_channel']`` fallback.** Stock's
       controller does
       ``self.with_context(discuss_channel=channel)._generate_response(…)``
       in ``_generate_response_for_channel``, so the channel is always
       in scope for the duration of ``request_llm``. ``channel.ai_agent_id``
       is the agent.

    Returns the ``ai.agent`` recordset (singleton) at the **caller's**
    privilege level — never sudo. Tool dispatch must run as the actual
    chat user so Odoo's RBAC, record rules, and multi-company rules are
    enforced for everything ``_ai_tool_*`` does. We use ``.sudo()`` only
    momentarily to *read* ``ai_agent_id`` (a field with elevated access
    requirements), then re-browse the id on the regular env to drop the
    sudo flag for downstream calls.
    """
    rec = getattr(tool_dispatch.current_agent, "record", None)
    if rec is not None:
        try:
            if rec.id:
                # Drop sudo if it was set by mistake. ``ai.agent`` should
                # always be accessible to the chat user under stock
                # access rules (the agent is what they're chatting with).
                return api_self.env["ai.agent"].browse(rec.id)
        except Exception:  # noqa: BLE001
            pass

    # Fallback: discuss_channel.ai_agent_id
    try:
        ch = api_self.env.context.get("discuss_channel")
        if ch is not None:
            # ``ch`` may be a recordset or an id; normalize.
            if isinstance(ch, int):
                ch = api_self.env["discuss.channel"].sudo().browse(ch)
            if hasattr(ch, "sudo") and hasattr(ch, "ai_agent_id"):
                agent_id = ch.sudo().ai_agent_id.id
                if agent_id:
                    # Re-browse on the non-sudo env so subsequent
                    # _ai_tool_* calls run as the actual user.
                    ag = api_self.env["ai.agent"].browse(agent_id)
                    _logger.info(
                        "daadit_ai_mistral.llm_api_patch: agent resolved "
                        "via env.context['discuss_channel'].ai_agent_id "
                        "(threadlocal was empty) → ai.agent(%s) as user %s",
                        agent_id, api_self.env.uid,
                    )
                    return ag
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.llm_api_patch: agent fallback lookup raised"
        )

    # Fallback 3 (v19.0.4.1.3): plain context keys. Some Enterprise
    # entry points (the standalone AI chat panel controller among
    # them) don't put a discuss channel in context but do carry the
    # agent id under a simple key.
    try:
        for _key in ("ai_agent_id", "agent_id", "default_ai_agent_id",
                     "default_agent_id"):
            val = api_self.env.context.get(_key)
            if isinstance(val, int) and val:
                ag = api_self.env["ai.agent"].browse(val).exists()
                if ag:
                    _logger.info(
                        "daadit_ai_mistral.llm_api_patch: agent resolved "
                        "via env.context[%r] → ai.agent(%s) as user %s",
                        _key, ag.id, api_self.env.uid,
                    )
                    return ag
    except Exception:  # noqa: BLE001
        pass

    # Fallback 3b (v19.0.4.1.4): request kwargs. Stock's request_llm
    # may carry the channel or agent under a kwarg. Accept recordsets
    # (ai.agent / discuss.channel) and channel-ish integer ids.
    try:
        for _k, _v in (request_kwargs or {}).items():
            vname = getattr(_v, "_name", None)
            if vname == "ai.agent" and getattr(_v, "ids", None) and len(_v.ids) == 1:
                ag = api_self.env["ai.agent"].browse(_v.ids[0])
                _logger.info(
                    "daadit_ai_mistral.llm_api_patch: agent resolved via "
                    "request kwarg %r (ai.agent recordset) → ai.agent(%s)",
                    _k, ag.id,
                )
                return ag
            if vname == "discuss.channel" and getattr(_v, "ids", None) and len(_v.ids) == 1:
                agent_id = _v.sudo().ai_agent_id.id
                if agent_id:
                    _logger.info(
                        "daadit_ai_mistral.llm_api_patch: agent resolved "
                        "via request kwarg %r (discuss.channel %s) → "
                        "ai.agent(%s)", _k, _v.ids[0], agent_id,
                    )
                    return api_self.env["ai.agent"].browse(agent_id)
            if (
                isinstance(_v, int) and _v
                and isinstance(_k, str)
                and ("channel" in _k.lower() or "thread" in _k.lower())
            ):
                ch2 = api_self.env["discuss.channel"].sudo().browse(_v).exists()
                if ch2 and ch2.ai_agent_id:
                    _logger.info(
                        "daadit_ai_mistral.llm_api_patch: agent resolved "
                        "via request kwarg %r=%s (channel id) → "
                        "ai.agent(%s)", _k, _v, ch2.ai_agent_id.id,
                    )
                    return api_self.env["ai.agent"].browse(ch2.ai_agent_id.id)
    except Exception:  # noqa: BLE001
        pass

    # Fallback 4 (v19.0.4.1.3): call-stack walk. The Enterprise AI
    # chat-panel controller constructs LLMApiService directly — our
    # ai.agent._get_provider never runs on that path, so neither the
    # threadlocal nor the discuss-channel context is populated
    # (observed on prod 2026-07-03 15:28 UTC: Mistral returned
    # tool_calls, dispatch had no agent, user got the "couldn't find
    # the AI Agent record" fallback). Walk the frames and pick the
    # nearest local that is an ai.agent singleton — or, since
    # v19.0.4.1.4, a discuss.channel singleton with an agent (the
    # ai_chat channel is what the panel controller actually holds).
    # Read-only inspection; the returned record is re-browsed on the
    # caller's env so RBAC is preserved. Bounded depth keeps this
    # cheap and safe.
    try:
        import sys
        frame = sys._getframe(1)
        depth = 0
        while frame is not None and depth < 40:
            for val in frame.f_locals.values():
                try:
                    vname = getattr(val, "_name", None)
                    if (
                        vname == "ai.agent"
                        and hasattr(val, "ids")
                        and len(val.ids) == 1
                        and val.ids[0]
                    ):
                        ag = api_self.env["ai.agent"].browse(val.ids[0])
                        _logger.info(
                            "daadit_ai_mistral.llm_api_patch: agent "
                            "resolved via call-stack walk (frame depth "
                            "%s, function %r) → ai.agent(%s) as user %s",
                            depth, frame.f_code.co_name, ag.id,
                            api_self.env.uid,
                        )
                        return ag
                    if (
                        vname == "discuss.channel"
                        and hasattr(val, "ids")
                        and len(val.ids) == 1
                        and val.ids[0]
                    ):
                        agent_id = val.sudo().ai_agent_id.id
                        if agent_id:
                            _logger.info(
                                "daadit_ai_mistral.llm_api_patch: agent "
                                "resolved via call-stack walk channel "
                                "(depth %s, fn %r, channel %s) → "
                                "ai.agent(%s)", depth,
                                frame.f_code.co_name, val.ids[0], agent_id,
                            )
                            return api_self.env["ai.agent"].browse(agent_id)
                except Exception:  # noqa: BLE001
                    continue
            frame = frame.f_back
            depth += 1
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.llm_api_patch: stack-walk agent lookup "
            "raised; continuing without agent"
        )

    # Fallback 5 (v19.0.4.1.4): newest ai_chat channel of this user.
    # The standalone AI panel creates a discuss.channel of type
    # 'ai_chat' with ai_agent_id set the moment the conversation
    # starts (verified on prod: channel 73 created at the exact
    # second of the failing message). When every structural fallback
    # missed, the most recently active ai_chat channel this user is
    # a member of is the conversation being answered. Heuristic —
    # logged loudly so we can spot any mis-resolution — but strictly
    # better than failing the whole turn.
    try:
        partner_id = api_self.env.user.partner_id.id
        ch3 = api_self.env["discuss.channel"].sudo().search(
            [
                ("channel_type", "=", "ai_chat"),
                ("ai_agent_id", "!=", False),
                ("channel_member_ids.partner_id", "in", [partner_id]),
            ],
            order="id desc", limit=1,
        )
        if ch3 and ch3.ai_agent_id:
            _logger.warning(
                "daadit_ai_mistral.llm_api_patch: agent resolved via "
                "LAST-RESORT newest-ai_chat-channel heuristic "
                "(channel %s → ai.agent %s, user %s). If this ever "
                "picks the wrong agent, the request context/kwargs "
                "diagnostics row in ir.logging shows what was "
                "available.", ch3.id, ch3.ai_agent_id.id, api_self.env.uid,
            )
            _record_resolution_diagnostics(api_self, request_kwargs,
                                           resolved_via="heuristic")
            return api_self.env["ai.agent"].browse(ch3.ai_agent_id.id)
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.llm_api_patch: ai_chat-channel heuristic "
            "raised; continuing without agent"
        )

    _record_resolution_diagnostics(api_self, request_kwargs,
                                   resolved_via="FAILED")
    return None


def _record_resolution_diagnostics(api_self, request_kwargs, resolved_via):
    """Persist a structure-only snapshot of the request to ir.logging.

    Written when agent resolution needed the last-resort heuristic or
    failed outright, so the next occurrence can be fixed
    deterministically instead of by guesswork. Logs KEY NAMES and
    types only — never values — so no chat content or PII lands in
    ir.logging.
    """
    try:
        import sys
        ctx_keys = sorted((api_self.env.context or {}).keys())
        kw_desc = {
            str(k): type(v).__name__ + (
                f"[{getattr(v, '_name', '')}]" if hasattr(v, "_name") else ""
            )
            for k, v in (request_kwargs or {}).items()
        }
        frames = []
        frame = sys._getframe(2)
        depth = 0
        while frame is not None and depth < 25:
            frames.append(
                f"{frame.f_code.co_name}({','.join(list(frame.f_locals.keys())[:10])})"
            )
            frame = frame.f_back
            depth += 1
        api_self.env["ir.logging"].sudo().create({
            "name": "daadit_ai_mistral.agent_resolution",
            "type": "server",
            "level": "WARNING",
            "message": (
                f"AGENT_RESOLUTION via={resolved_via} uid={api_self.env.uid} "
                f"ctx_keys={ctx_keys} kwargs={kw_desc} stack={frames}"
            )[:8000],
            "path": "daadit_ai_mistral",
            "func": "_resolve_agent",
            "line": "0",
        })
        api_self.env.cr.commit()
    except Exception:  # noqa: BLE001
        pass


# Waarom dit bestaat: gesprekken eindigen in een vraag die de collega
# zelf had kunnen beantwoorden. Waargenomen 27-07: de blog-tool gáf de
# lijst van twee blogs terug, waarna de agent "welke blog zal ik
# gebruiken?" doorspeelde naar de gebruiker. Zo'n beurt kost een volle
# extra ronde en laat de collega nutteloos aanvoelen — precies het
# tegenovergestelde van een medewerker die je werk uit handen neemt.
_SELF_SERVICE_INSTRUCTION = (
    "CRITICAL — DECIDE, DON'T ASK: You have tools that read this Odoo "
    "database. Never ask the user for anything you can look up, and "
    "never hand back a choice you can make yourself. Before you ask a "
    "question: (1) search Odoo for the answer; (2) if a tool result "
    "already lists the options, pick the one that best fits the task; "
    "(3) if the options are genuinely equivalent, take the most "
    "obvious one, act, and state which one you took and how to change "
    "it. Asking 'which blog should I use?' right after a tool handed "
    "you the list of blogs is a failure, not caution. Only ask the "
    "user when the answer cannot exist in Odoo, or when the action is "
    "irreversible or costly (publishing, sending email, deleting, "
    "spending money) — and then ask once, with your recommendation "
    "already in the question. If the work belongs to another "
    "colleague and you have a tool to reach them, use it; do not tell "
    "the user to go ask that colleague."
)


_LANGUAGE_MIRROR_INSTRUCTION = (
    "IMPORTANT: Always respond in the same language as the user's most "
    "recent message. If the user writes in Dutch, reply in Dutch. If "
    "the user writes in French, reply in French. Do NOT translate "
    "technical identifiers inside backticks (such as `account.move`, "
    "`res.partner`, field names like `stage_id`).\n\n"
    "CRITICAL — TOOL CALLS: When invoking a function/tool, the "
    "`name` field of the tool call MUST be the exact, literal "
    "identifier you were given in the tools list — typically an "
    "ASCII snake_case string starting with `ir_actions_server_` "
    "(for example `ir_actions_server_search`, "
    "`ir_actions_server_get_fields`, "
    "`ir_actions_server_read_group`). NEVER translate, localise, "
    "or paraphrase a tool name. Translated names (e.g. "
    "`ir_actions_server_velden_oproepen`, `..._zoeken`) do not "
    "exist and will be rejected as 'Unknown tool'. If a tool you "
    "need isn't in the provided tools list, do not invent one — "
    "tell the user in plain language which capability is missing.\n\n"
    + _SELF_SERVICE_INSTRUCTION
)


def _inject_language_mirror(conversation):
    """Append a language-mirroring instruction to the conversation's
    system message so Mistral replies in the user's language.

    Why: stock Odoo's agent system prompts are typically in English,
    and our tool schemas are in English. Smaller Mistral models
    (`ministral-8b`, `mistral-small`) follow those language cues and
    answer in English even when the user types Dutch. This single
    appended instruction reliably flips that bias without altering
    the agent's intended persona.

    Strategy:
      * If there's a system message at index 0, append the instruction
        to its content (separated by a blank line).
      * Otherwise, prepend a fresh system message containing only the
        instruction.

    Returns a new list — does not mutate the caller's structure.
    """
    if not conversation:
        return [{
            "role": "system",
            "content": _LANGUAGE_MIRROR_INSTRUCTION,
        }]

    out = []
    injected = False
    for i, m in enumerate(conversation):
        if not injected and isinstance(m, dict) and m.get("role") == "system":
            new_msg = dict(m)
            existing = new_msg.get("content") or ""
            new_msg["content"] = (
                f"{existing}\n\n{_LANGUAGE_MIRROR_INSTRUCTION}"
                if existing else _LANGUAGE_MIRROR_INSTRUCTION
            )
            out.append(new_msg)
            injected = True
        else:
            out.append(m)

    if not injected:
        # No system message at all — put one up front.
        out.insert(0, {
            "role": "system",
            "content": _LANGUAGE_MIRROR_INSTRUCTION,
        })
    return out


def _inject_runtime_context(conversation):
    """Append the current date/time to the conversation's system message.

    Why: Mistral gets no clock. On prod (2026-07-03, log #88) a Dutch
    "deze maand"-question was answered by filtering ``create_date`` on
    **October 2023** — the model guessed a date from its training data
    and produced confidently wrong, empty results. One line of runtime
    context eliminates the whole failure class for every relative-date
    question ("deze maand", "vorige week", "dit kwartaal").

    Same injection strategy as ``_inject_language_mirror``: append to
    the first system message, or prepend a fresh one. Returns a new
    list; does not mutate the input.
    """
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        now_local = now_utc.astimezone(ZoneInfo("Europe/Amsterdam"))
        local_label = "Europe/Amsterdam"
    except Exception:  # noqa: BLE001
        now_local = now_utc
        local_label = "UTC"
    instruction = (
        f"CURRENT DATE/TIME: it is now {now_local.strftime('%A %Y-%m-%d %H:%M')} "
        f"({local_label}); {now_utc.strftime('%Y-%m-%d %H:%M')} UTC. "
        f"Resolve every relative date reference (today, this month, "
        f"last week, dit kwartaal, deze maand) against THIS date — "
        f"never against your training data."
    )
    if not conversation:
        return [{"role": "system", "content": instruction}]
    out = []
    injected = False
    for m in conversation:
        if not injected and isinstance(m, dict) and m.get("role") == "system":
            new_msg = dict(m)
            existing = new_msg.get("content") or ""
            new_msg["content"] = (
                f"{existing}\n\n{instruction}" if existing else instruction
            )
            out.append(new_msg)
            injected = True
        else:
            out.append(m)
    if not injected:
        out.insert(0, {"role": "system", "content": instruction})
    return out


def _answer_looks_degenerate(text):
    """True when a routed answer shows the classic runaway-generation
    pattern and must NOT be relayed verbatim to the user.

    Added in 19.0.6.5.5 after a live incident: a specialist asked to do
    something outside its scope refused correctly, then kept appending
    "Einde. ❌✅🛑🚀" variations until it hit its 4000-token cap. Before
    the pass-through (19.0.6.5.4) the concierge re-generated the answer
    and silently absorbed that noise; relaying verbatim removed that
    accidental filter. This restores it as an explicit one — on a hit we
    fall back to the normal path so the concierge summarises instead.

    Three independent signals, any of which is enough:

    * length — a chat answer beyond ~6000 chars is not a chat answer;
    * line repetition — fewer than 40% distinct non-empty lines over a
      reasonable number of lines means the model is looping;
    * tail repetition — the same short fragment repeated many times in
      the last stretch, which catches loops that vary just enough to
      keep lines "distinct".
    """
    if not text:
        return False
    if len(text) > 6000:
        return True

    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped:
            lines.append(stripped)
    if len(lines) >= 12:
        distinct = len(set(lines))
        if distinct * 100 < len(lines) * 40:
            return True

    tail = text[-1500:]
    for marker in ("Einde", "EINDE", "---", "Tot ziens", "Final Answer"):
        if tail.count(marker) >= 6:
            return True
    return False


# Het ruwe resultaat van een specialist-subrun: een JSON-object met een
# ``answer``/``ok``-veld én een ``error``-veld. Beide velden moeten er
# staan — een antwoord dat zélf over JSON gaat ("stuur {"answer": …}")
# heeft die tweede helft niet en blijft dus heel.
_LEAK_JSON_RE = re.compile(r"\{\s*\"(?:answer|ok)\"\s*:")
_LEAK_JSON_CONFIRM_RE = re.compile(r"\"error\"\s*:")
# De staart van de interne opdrachtlijst, direct gevolgd door de naam
# van de collega aan wie werd gedelegeerd: ``]]   -   Pim <deelvraag>``.
_LEAK_DELEGATION_RE = re.compile(r"\]\]\s*-+\s*[A-Z][a-z]+\b")
# Tekens waar een doorgeslagen generatie in blijft hangen. Een echte
# tekst gebruikt deze nooit tientallen keren achter elkaar.
_RUNAWAY_RE = re.compile(r"[>\-*=._~#·]{40,}")
_EMPTY_AFTER_STRIP = (
    "Er ging iets mis bij het opstellen van dit antwoord. "
    "Stel je vraag opnieuw."
)


def _strip_runaway_and_leaks(text):
    """Snij een doorgeslagen staart en een gelekt tool-resultaat van een
    antwoord af.

    Twee dingen die de gebruiker nooit mag zien, en die tot 3-8-2026
    beide in één chatbericht van de concierge stonden:

    * **Het interne verkeer.** Het model zette de deelvraag die het aan
      een specialist stelde, plus het ruwe JSON-resultaat
      (``{"answer": …, "error": null}``), letterlijk in zijn eigen
      antwoordtekst. Dat is geen antwoord maar de binnenkant van het
      systeem, en het spreekt bovendien de tekst erboven tegen.
    * **De doorgeslagen staart.** Daarna liep de generatie vast in een
      lus van ``>``- en ``-``-tekens tot het tokenplafond.

    ``_answer_looks_degenerate`` ving dit al af voor het *doorgegeven*
    antwoord van een specialist, maar niet voor de eigen tekst van de
    agent — precies het geval dat live misging. Deze functie snijdt in
    plaats van te verwerpen: wat vóór de rommel staat is bruikbaar en
    blijft staan.

    Returns ``(schone_tekst, is_gesnoeid)``.
    """
    if not isinstance(text, str) or not text:
        return text, False
    original = text.rstrip()

    cut = len(text)
    json_hit = _LEAK_JSON_RE.search(text)
    if json_hit and _LEAK_JSON_CONFIRM_RE.search(text, json_hit.end()):
        cut = json_hit.start()
    delegation_hit = _LEAK_DELEGATION_RE.search(text)
    if delegation_hit and delegation_hit.start() < cut:
        cut = delegation_hit.start()
    runaway_hit = _RUNAWAY_RE.search(text)
    if runaway_hit and runaway_hit.start() < cut:
        cut = runaway_hit.start()
    text = text[:cut]

    # Losse resten van de afgesneden blokken: de sluithaken van de
    # interne opdrachtlijst en een reeks scheidingsregels.
    text = re.sub(r"\s*\]\]\s*-*\s*\Z", "", text)
    text = re.sub(r"(?:\n\s*(?:-+|>+|=+|Einde bericht\.?)\s*)+\Z", "", text)
    text = text.rstrip()
    return text, text != original


def _clean_adapted(adapted, where):
    """Pas :func:`_strip_runaway_and_leaks` toe op wat de gebruiker te
    zien krijgt. Werkt zowel op losse strings als op
    ``{role, content}``-dicts, omdat beide vormen door deze module
    heen lopen. Blijft er na het snijden niets over, dan komt er een
    korte melding in plaats van een leeg bericht."""
    out = []
    for item in adapted or []:
        if isinstance(item, str):
            cleaned, trimmed = _strip_runaway_and_leaks(item)
            cleaned = cleaned or (_EMPTY_AFTER_STRIP if trimmed else cleaned)
        elif isinstance(item, dict) and isinstance(item.get("content"), str):
            cleaned_text, trimmed = _strip_runaway_and_leaks(item["content"])
            if trimmed and not cleaned_text:
                cleaned_text = _EMPTY_AFTER_STRIP
            cleaned = dict(item, content=cleaned_text)
        else:
            out.append(item)
            continue
        if trimmed:
            _logger.warning(
                "daadit_ai_mistral.llm_api_patch: antwoord gesnoeid (%s) — "
                "gelekt tool-resultaat en/of doorgeslagen staart verwijderd",
                where,
            )
        out.append(cleaned)
    return out


def _request_llm_mistral(api_self, *args, **kwargs):
    """Mistral-side replacement for ``LLMApiService.request_llm``.

    Best-effort parameter extraction from ``args``/``kwargs`` because the
    stock signature of ``request_llm`` is closed-source. The first call
    is logged in full so the wrapper can be tightened later.

    Extraction order:
      1. Direct kwargs (``model=``, ``messages=``, etc., under any of
         several name variants).
      2. Positional args: a Mistral-shaped string ⇒ model; a list of
         {role, content} dicts ⇒ messages; a single dict ⇒ treated as a
         body and recursed into.
      3. Nested body kwargs (``body=``, ``payload=``, etc.).
      4. ``prompt`` / ``system_prompt`` strings ⇒ assembled into messages.

    If no messages can be reconstructed, raise a clear error rather than
    forwarding an empty list to Mistral (which would 400 with an
    unhelpful generic rejection).
    """
    _log_first_call_args(args, kwargs)

    model = messages = tools = tool_choice = temperature = max_tokens = None

    # --- 1. Direct kwargs --------------------------------------------
    (model, messages, tools, tool_choice, temperature, max_tokens) = \
        _extract_from_dict(
            kwargs, model, messages, tools, tool_choice,
            temperature, max_tokens,
        )

    # --- 2. Positional sniffing --------------------------------------
    for a in args:
        if isinstance(a, str) and not model:
            # Accept any string that looks like a Mistral model name.
            # Stock might pass arg[0] as a model name.
            if is_mistral_model(a):
                model = a
        elif isinstance(a, (list, tuple)) and not messages:
            normalized = _normalize_messages(a)
            if normalized:
                messages = normalized
        elif isinstance(a, dict):
            (model, messages, tools, tool_choice, temperature, max_tokens) = \
                _extract_from_dict(
                    a, model, messages, tools, tool_choice,
                    temperature, max_tokens,
                )

    # --- 3. Nested body kwargs ---------------------------------------
    for k in _BODY_KEYS:
        nested = kwargs.get(k)
        if isinstance(nested, dict):
            (model, messages, tools, tool_choice, temperature, max_tokens) = \
                _extract_from_dict(
                    nested, model, messages, tools, tool_choice,
                    temperature, max_tokens,
                )

    # --- 4. prompt / system_prompt ⇒ assemble messages ---------------
    # AI Fields and other ai_fields/tools.py-style callers pass plural
    # kwargs as lists: `system_prompts=[...]`, `user_prompts=[...]`.
    # The legacy singular form (`prompt=`, `system_prompt=`) is also
    # supported. Lists get joined into one block per role; strings are
    # used as-is.
    if not messages:
        def _collect(kwargs_dict, keys_singular, keys_plural):
            # Plural variant first: build per-key list (could be list or str).
            collected = []
            for k in keys_plural:
                val = kwargs_dict.get(k)
                if not val:
                    continue
                if isinstance(val, (list, tuple)):
                    for item in val:
                        if item:
                            collected.append(str(item))
                else:
                    collected.append(str(val))
            if collected:
                return "\n\n".join(collected)
            # Singular fallback.
            for k in keys_singular:
                val = kwargs_dict.get(k)
                if val:
                    return str(val)
            return None

        # _PROMPT_KEYS / _SYSTEM_KEYS contain the singular forms. The
        # plural forms are AI Fields-specific so we list them inline.
        _USER_PROMPT_KEYS_PLURAL = ("user_prompts", "user_messages", "prompts")
        _SYSTEM_PROMPT_KEYS_PLURAL = ("system_prompts", "system_messages",
                                      "instructions_list")
        prompt = _collect(kwargs, _PROMPT_KEYS, _USER_PROMPT_KEYS_PLURAL)
        system = _collect(kwargs, _SYSTEM_KEYS, _SYSTEM_PROMPT_KEYS_PLURAL)
        if prompt or system:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            if prompt:
                messages.append({"role": "user", "content": prompt})

    # --- 5. JSON schema enforcement (AI Fields, structured prompts) --
    # AI Fields (enterprise/ai_fields/tools.py:182) passes `schema=`
    # alongside the prompts and then calls `json.loads(response[0])`
    # on the result. Without telling Mistral to constrain output to
    # JSON, it returns prose ("Hier is mijn analyse: ..."), the parse
    # fails, and the user sees "Oeps, het antwoord kon niet worden
    # verwerkt".
    #
    # Mistral's strict structured outputs use this body shape:
    #   response_format = {
    #     "type": "json_schema",
    #     "json_schema": {"name": "...", "strict": True, "schema": {...}},
    #   }
    # mistral-medium-latest / -large-latest accept it. Smaller models
    # may not, so we degrade gracefully to "json_object" mode, which
    # at minimum guarantees the response is valid JSON of *some*
    # shape (client-side validation handles the rest).
    schema = None
    for k in ("schema", "json_schema", "response_schema", "output_schema"):
        if kwargs.get(k):
            schema = kwargs[k]
            break

    response_format_extra = None
    if schema and isinstance(schema, dict):
        # Models that have documented strict json_schema support.
        STRICT_JSON_SCHEMA_MODELS = (
            "mistral-large-latest", "mistral-medium-latest",
            "mistral-large-", "mistral-medium-", "ministral-",
        )
        # We hand the schema + the model-allowlist to the call site;
        # the final response_format dict is built there once `model`
        # is fully resolved (it may still be substituted below).
        response_format_extra = {
            "_schema": schema,
            "_strict_models_prefix": STRICT_JSON_SCHEMA_MODELS,
        }

    # --- Defaults / hard validation ----------------------------------
    # Two ways `model` can be wrong here:
    #   (a) absent — caller didn't include any model kwarg
    #   (b) present but not a Mistral model — AI Fields hardcodes
    #       `llm_model="gpt-4o"` even when we route the call to Mistral
    # Both → substitute a Mistral default. Reading the system parameter
    # makes the default configurable per-deployment.
    if not model or not is_mistral_model(model):
        try:
            icp = api_self.env["ir.config_parameter"].sudo()
            mistral_default = (
                icp.get_param("daadit_ai_mistral.default_chat_model")
                or "mistral-medium-latest"
            ).strip()
        except Exception:  # noqa: BLE001
            mistral_default = "mistral-medium-latest"
        if not is_mistral_model(mistral_default):
            # Operator misconfigured the parameter; ignore it.
            mistral_default = "mistral-medium-latest"
        if model and not is_mistral_model(model):
            _logger.info(
                "daadit_ai_mistral.llm_api_patch: caller asked for "
                "non-Mistral model %r (likely AI Fields hardcoded "
                "gpt-4o) — substituting %s",
                model, mistral_default,
            )
        elif not model:
            _logger.warning(
                "daadit_ai_mistral.llm_api_patch: no 'model' found in "
                "request_llm call; falling back to %s", mistral_default,
            )
        model = mistral_default

    if not messages:
        # Don't even hit Mistral's API with an empty conversation —
        # raise a clear error pointing the operator at the diagnostic
        # log line.
        from odoo.exceptions import UserError
        from odoo.tools.translate import _
        raise UserError(_(
            "DAADit AI Mistral: could not extract 'messages' from "
            "request_llm call (args types=%(at)s, kwargs keys=%(kk)s). "
            "Look for the 'first request_llm(mistral) call' line in the "
            "Odoo log to see the exact arg names stock is using.",
            at=str([type(a).__name__ for a in args]),
            kk=str(list(kwargs.keys())),
        ))

    # De agent is hier al nodig: een tool die de operator zelf maakte
    # krijgt zijn schema uit de serveractie van déze agent.
    agent = _resolve_agent(api_self, request_kwargs=kwargs)

    # ---- Build proper tool definitions ------------------------------
    # If stock sent us a list of tool name strings, use the JSON-schema
    # definitions from ``tool_dispatch.TOOL_SCHEMAS`` for the standard
    # ten AI tools (Search / Read group / Get Fields / Open Menu *).
    # For anything else (already-shaped dicts, etc.), fall back to the
    # generic _normalize_tools converter from earlier versions.
    normalized_tools = None
    if tools:
        if isinstance(tools, (list, tuple)) and all(isinstance(t, str) for t in tools):
            normalized_tools = tool_dispatch.annotate_tools(tools, agent=agent)
        else:
            normalized_tools = _normalize_tools(tools)
        if not normalized_tools:
            _logger.warning(
                "daadit_ai_mistral.llm_api_patch: tools were provided but "
                "could not be normalized to Mistral format; dropping. "
                "Original tools type=%s, repr=%s",
                type(tools).__name__,
                _safe_repr(tools, limit=400),
            )

    # ---- Tool-execution loop ----------------------------------------
    # Run a bounded loop:
    #   call → if tool_calls, run each on the agent and feed back as
    #   ``role: tool`` messages → call again. Stop when the model
    #   returns a final text response OR we hit ``MAX_ITER``.
    _had_threadlocal = bool(getattr(tool_dispatch.current_agent, "record", None))

    # ---- Reconstruct tools from agent topics (v19.0.4.1.5) ----------
    # On the standalone AI chat-panel path the Enterprise controller
    # calls the LLM service directly and passes NO tools — stock's
    # OpenAI branch rebuilds them deeper down, so on the Mistral
    # branch that responsibility is OURS. Without this, the model
    # can only narrate ("ik ga eerst opzoeken…") and ask questions,
    # exactly what prod showed at 16:59 UTC (zero dispatch rows in
    # ir.logging for that turn).
    if agent is not None and not normalized_tools:
        try:
            # Inside a routed sub-run (depth > 0) the reconstruction must
            # apply the SAME strips the router applies when it builds the
            # list explicitly (v19.0.4.2.1) — otherwise a sub-run with an
            # empty explicit tool list silently regains the router tool
            # and every write-side tool via this path.
            _in_subrun = _router_depth > 0  # noqa: F821  (closure over _router_depth)
            names = []
            for action in agent.sudo().topic_ids.tool_ids:
                if action.model_id and action.model_id.model == "ai.agent":
                    slug = _slug_tool_name(action.name)
                    if not slug or slug in names:
                        continue
                    if _in_subrun and (
                        slug == tool_dispatch.ROUTER_TOOL_SLUG
                        or slug in tool_dispatch.WRITE_SIDE_TOOL_SLUGS
                    ):
                        continue
                    names.append(slug)
            if names:
                normalized_tools = tool_dispatch.annotate_tools(
                    names, agent=agent,
                )
                _logger.info(
                    "daadit_ai_mistral.llm_api_patch: caller passed no "
                    "tools — injected %d tool defs from agent %s topics "
                    "(subrun=%s): %s",
                    len(names), agent.id, _in_subrun, names,
                )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral.llm_api_patch: topic-tool "
                "reconstruction failed; continuing without tools"
            )

    # ---- Per-agent overrides ----------------------------------------
    # If the agent has explicit ``daadit_mistral_temperature`` /
    # ``daadit_mistral_max_tokens`` settings, they win over whatever
    # stock passed through ``request_llm`` kwargs.
    #
    # SEC L4 (v19.0.3.11.0): the temperature override is gated by an
    # explicit boolean ``daadit_mistral_temperature_active`` so a
    # value of 0.0 (deterministic mode) is honoured. Previously a
    # falsy 0.0 was indistinguishable from "unset" and silently
    # fell back to the response_style mapping.
    if agent is not None:
        try:
            if getattr(agent, "daadit_mistral_temperature_active", False):
                temperature = agent.daadit_mistral_temperature
        except Exception:  # noqa: BLE001
            pass
        try:
            agent_max = agent.daadit_mistral_max_tokens
            if agent_max:
                max_tokens = agent_max
        except Exception:  # noqa: BLE001
            pass

    client = MistralClient.from_env(api_self.env)
    conversation = list(messages)

    # ---- Reconstruct the agent system prompt (v19.0.4.1.5) ----------
    # Same bare-entry-point gap as the tools above: the panel path
    # sends the raw user message without the agent's system prompt,
    # so the model answers without persona, domain map or the
    # act-don't-ask directive. If the resolved agent has a prompt
    # and no system message in the conversation contains its opening
    # (first 120 chars as probe), prepend it.
    if agent is not None:
        try:
            sys_prompt = (agent.sudo().system_prompt or "").strip()
            if sys_prompt:
                probe = sys_prompt[:120]
                already_there = any(
                    isinstance(m, dict) and m.get("role") == "system"
                    and probe in (m.get("content") or "")
                    for m in conversation
                )
                if not already_there:
                    conversation.insert(
                        0, {"role": "system", "content": sys_prompt},
                    )
                    _logger.info(
                        "daadit_ai_mistral.llm_api_patch: caller sent no "
                        "agent system prompt — prepended %d chars from "
                        "agent %s", len(sys_prompt), agent.id,
                    )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral.llm_api_patch: system-prompt "
                "reconstruction failed; continuing"
            )

    conversation = _inject_language_mirror(conversation)
    conversation = _inject_runtime_context(conversation)

    # ---- Request-structure telemetry (v19.0.4.1.5) -------------------
    # One INFO row per chat turn with SHAPE only (roles, counts,
    # tool names, booleans) — no message content, no PII. This is what
    # turns the next "the agent behaves oddly" report into a
    # deterministic diagnosis instead of another guessing round.
    try:
        roles = ",".join(
            m.get("role", "?") for m in conversation if isinstance(m, dict)
        )
        sys_len = sum(
            len(m.get("content") or "")
            for m in conversation
            if isinstance(m, dict) and m.get("role") == "system"
        )
        tool_names_dbg = [
            t.get("function", {}).get("name", "?")
            for t in (normalized_tools or [])
        ][:12]
        api_self.env["ir.logging"].sudo().create({
            "name": "daadit_ai_mistral.request",
            "type": "server",
            "level": "INFO",
            "message": (
                f"REQ model={model} agent={agent.id if agent else None} "
                f"threadlocal={_had_threadlocal} msgs={len(conversation)} "
                f"roles=[{roles}] sys_len={sys_len} "
                f"tools={len(normalized_tools or [])}:{tool_names_dbg}"
            )[:8000],
            "path": "daadit_ai_mistral",
            "func": "_request_llm_mistral",
            "line": "0",
        })
        api_self.env.cr.commit()
    except Exception:  # noqa: BLE001
        pass

    iteration = 0
    # Fase 0 governance guardrail (Knowledge id 173): top-level loop
    # budget. v19.0.4.9.0 — now a System Parameter so it can be tuned
    # centrally without a deploy (Settings → Technical → System
    # Parameters). Defaults: 20 top-level, 4 sub-run; fail-open to those.
    # Sub-runs stay tighter — a dwaling sub-agent must never consume the
    # whole parent turn.
    try:
        _icp = api_self.env["ir.config_parameter"].sudo()
        MAX_ITER = int(_icp.get_param("daadit_ai_mistral.max_tool_iterations") or 20)
        _max_iter_subrun = int(
            _icp.get_param("daadit_ai_mistral.max_tool_iterations_subrun") or 4
        )
    except Exception:  # noqa: BLE001
        MAX_ITER = 20
        _max_iter_subrun = 4
    _router_depth = getattr(tool_dispatch.router_state, "depth", 0)
    if _router_depth > 0:
        MAX_ITER = _max_iter_subrun
    else:
        # Top-level turn: reset per-turn router width budget and
        # exhaustion flags so state from a previous turn on this
        # worker thread never leaks in (v19.0.4.2.1 / v19.0.4.4.0).
        tool_dispatch.router_state.calls = 0
        tool_dispatch.router_state.exhausted = False
        tool_dispatch.router_state.sub_failed = False
        # Models this turn was refused on policy grounds. Read by the
        # router to refuse a hop to a colleague who may not read them
        # either; reset here so a refusal from a previous turn on this
        # worker thread never blocks a legitimate delegation.
        tool_dispatch.router_state.denied_models = set()
        # v19.0.4.4.0: top_level_exhausted is read by
        # ``daadit_ai_agent_schedule`` after ``request_llm`` returns to
        # decide whether a scheduled run should be marked 'done' or
        # 'error'. Namespaced separately from ``exhausted`` (which is
        # sub-run specific) to keep concerns clean.
        tool_dispatch.router_state.top_level_exhausted = False
        tool_dispatch.router_state.exhaustion_reason = None
        # v19.0.4.8.0: one uuid per top-level turn. Every usage row
        # this turn creates (including router sub-calls, which inherit
        # the threadlocal) carries it, so the performance report can
        # count QUESTIONS (distinct turn_uuid / depth-0 rows) instead
        # of API calls.
        tool_dispatch.router_state.turn_uuid = uuid.uuid4().hex
    # Fase 0 governance guardrail: cap hallucinated tool-name attempts.
    # After 2 "Unknown tool: ..." results in the same turn we bail out
    # instead of letting the model keep guessing and burn the cap.
    unknown_tool_count = 0
    unknown_tool_break = False
    deadline_break = False
    response = None
    access_denial = None  # set when a tool call is denied by admin policy
    # v19.0.6.8.0: how many times this turn we turned a scope denial into
    # a routing instruction instead of ending the turn. Capped so a model
    # that keeps reaching for out-of-scope models can't loop.
    scope_redirects = 0
    # v19.0.6.5.4: concierge pass-through. When the TOP-LEVEL agent
    # routes a question via the router tool and the specialist returns a
    # real answer, that answer is relayed verbatim (with attribution)
    # instead of feeding it back to the model for one more full
    # generation that would only paraphrase it — the single biggest
    # latency component of a routed chat answer (30-60s). Multi-route
    # turns (several router calls in ONE assistant message) keep the
    # normal combining path. Tunable kill-switch via System Parameter
    # ``daadit_ai_mistral.router_passthrough`` (default on).
    router_passthrough = None
    try:
        _passthrough_enabled = str(
            _icp.get_param("daadit_ai_mistral.router_passthrough", "1")
        ).strip().lower() not in ("0", "false")
    except Exception:  # noqa: BLE001
        _passthrough_enabled = True

    # --- Daily cost cap (v19.0.4.3.0) --------------------------------
    # Single choke point for every Mistral chat call. When today's spend
    # has reached the configured cap, refuse the call with a clear,
    # translated message instead of hitting Mistral — and notify the
    # admin once per day. Fail-open: a broken breaker never blocks chat.
    try:
        from . import cost_cap
        blocked, spent, cap = cost_cap.check(api_self.env)
        if blocked:
            _logger.warning(
                "daadit_ai_mistral.llm_api_patch: daily cost cap reached "
                "(%.2f/%.2f) — refusing call for agent=%s",
                spent, cap, agent.id if agent else None,
            )
            msg = _translate_to_chat_language(
                client, model, conversation,
                cost_cap.blocked_message_en(spent, cap),
            )
            return [msg]
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.llm_api_patch: cost-cap gate raised; "
            "failing open"
        )

    # --- Fair use per collega ----------------------------------------
    # De systeemcap hierboven is een noodrem voor de hele database; deze
    # gate is de commerciële grens van één AI-collega. Boven de
    # drempel hoort de klant het te weten, en op 100% mag de collega nog
    # wel meedenken maar niets meer doen: één goedkope completion in
    # plaats van een toolketen, en de klant hoort waaróm. Alles dicht
    # gooien voelt als een storing en levert een supportgesprek op.
    fair_use_notice = ""
    try:
        from . import cost_cap as _cc
        _budget = _cc.agent_state(api_self.env, agent)
        fair_use_notice = _cc.fair_use_notice_en(_budget)
        if _cc.should_drop_tools(_budget):
            _logger.warning(
                "daadit_ai_mistral.llm_api_patch: budget bereikt voor "
                "agent=%s (%.0f%% van %.2f EUR) — tools uitgeschakeld "
                "voor deze beurt",
                agent.id if agent else None,
                (_budget or {}).get("ratio", 0.0) * 100,
                (_budget or {}).get("cap", 0.0),
            )
            normalized_tools = None
            tool_choice = None
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.llm_api_patch: fair-use gate raised; "
            "failing open"
        )
        fair_use_notice = ""

    try:
        _ch = api_self.env.context.get("discuss_channel")
        _status_channel_id = (
            _ch.id if _ch is not None and hasattr(_ch, "id")
            else (_ch if isinstance(_ch, int) else False)
        )
    except Exception:  # noqa: BLE001
        _status_channel_id = False

    while iteration < MAX_ITER:
        # v19.0.4.8.0: hard per-run time budget (Fase 0-gate). Callers
        # that own a whole run (daadit_ai_agent_schedule) set
        # ``router_state.run_deadline_monotonic`` before request_llm
        # and clear it in their finally. Checked between round-trips —
        # a run can overshoot by at most one Mistral call + its tools,
        # never hang for the full MAX_ITER budget. Interactive chat
        # never sets it, so this is a no-op there.
        _run_deadline = getattr(
            tool_dispatch.router_state, "run_deadline_monotonic", None,
        )
        if _run_deadline is not None and time.monotonic() >= _run_deadline:
            deadline_break = True
            _logger.warning(
                "daadit_ai_mistral.llm_api_patch: run deadline reached "
                "after %d iteration(s) — breaking the tool loop",
                iteration,
            )
            break

        # Build the per-call `extra` payload. When the caller passed a
        # JSON schema (AI Fields, structured automation actions), turn
        # it into Mistral's `response_format` and drop tools — Mistral
        # rejects requests where both are active simultaneously.
        chat_extra = None
        active_tools = normalized_tools
        active_tool_choice = tool_choice
        if response_format_extra:
            schema_obj = response_format_extra["_schema"]
            use_strict = model.startswith(
                response_format_extra["_strict_models_prefix"]
            )
            if use_strict:
                chat_extra = {
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "ai_response",
                            "strict": True,
                            "schema": schema_obj,
                        },
                    }
                }
            else:
                # Fallback for models without strict json_schema: at
                # least force valid JSON output. AI Fields validates
                # client-side after parsing, so shape mismatches will
                # still surface — but we no longer return prose.
                chat_extra = {
                    "response_format": {"type": "json_object"},
                }
                # Push the schema into the system prompt as a hint so
                # the model has *some* structural guidance.
                if not any(m.get("role") == "system" for m in conversation):
                    conversation.insert(0, {
                        "role": "system",
                        "content": (
                            "Return ONLY a single JSON object matching this "
                            "schema. Do not wrap in markdown.\n\n"
                            f"{json.dumps(schema_obj)}"
                        ),
                    })
            active_tools = None
            active_tool_choice = None

        # Force a tool call on the FIRST turn (v19.0.4.2.3). mistral-medium
        # regularly narrates a plan and asks "zal ik doorgaan?" instead of
        # emitting the tool call — defeating the whole "act, don't ask"
        # design at the mechanism level, not just the prompt. When tools
        # are available, no JSON schema is active, and the caller didn't
        # pin a tool_choice, we set tool_choice='any' for iteration 0 so
        # Mistral MUST call one of the provided tools. Later iterations
        # fall back to 'auto' (active_tool_choice stays as-is) so the
        # model can produce the final text answer after seeing results.
        #
        # v19.0.6.10.1: behalve bij een begroeting. Forceren betekent dat
        # het model MOET grijpen naar een tool, ook als er niets op te
        # zoeken valt. Op "Hi Robin, hoe gaat het" pakte de concierge dan
        # noodgedwongen de router, kreeg een merk-en-toon-verhandeling
        # terug van Maud, en gaf dat door als antwoord op een begroeting.
        # Bij small talk is 'auto' het juiste gedrag: gewoon antwoorden.
        if (
            iteration == 0
            and active_tools
            and not chat_extra
            and active_tool_choice in (None, "auto")
            and not _is_smalltalk(conversation)
        ):
            active_tool_choice = "any"
            _logger.info(
                "daadit_ai_mistral.llm_api_patch: forcing tool_choice='any' "
                "on first turn (%d tools) to prevent narrate-and-ask",
                len(active_tools),
            )

        # v19.0.6.10.0: de langste stilte zit NIET bij de tool-aanroepen
        # maar hiervoor — het wachten op het antwoord van het model zelf.
        # Bij een vraag zonder tools (een begroeting bijvoorbeeld) is dat
        # de hele wachttijd, en dan bleef de gebruiker alsnog naar "AI is
        # thinking" kijken. Meld dus ook deze ronde, vóór de round-trip.
        # v19.0.6.17.0: ook binnen een doorgerouteerde sub-run (depth > 0)
        # — de frontend springt die stappen in onder de hoofdstap, zodat
        # zichtbaar is dat de collega aan het werk is.
        _notify_step(
            agent,
            "Ik kijk ernaar" if iteration == 0
            else "Ik verwerk wat ik heb opgehaald",
            depth=_router_depth,
        )

        response = client.chat_completion(
            model=model,
            messages=conversation,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=active_tools,
            tool_choice=active_tool_choice,
            extra=chat_extra,
        )
        choice = (response.get("choices") or [{}])[0]
        msg = (choice.get("message") if isinstance(choice, dict) else None) or {}
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls or agent is None:
            break

        # Append the assistant's tool-call message verbatim so the
        # follow-up call can correlate the tool results.
        conversation.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": tool_calls,
        })

        for tc in tool_calls:
            # v19.0.6.9.0: tell the user what is happening while it
            # happens. "AI is thinking..." for forty seconds reads as a
            # hang; "Ik leg dit voor aan Sem" reads as work.
            # v19.0.6.17.0: nu op elke diepte, met ``depth`` mee zodat de
            # frontend sub-run-stappen ingesprongen onder de hoofdstap
            # toont in plaats van ze te verbergen.
            _notify_step(agent, _step_text(tc), depth=_router_depth,
                         kind="tool")
            # Zonder de gedeelde stappenmodule komt er niets in beeld;
            # dan blijft de losse kanaalmelding de enige terugkoppeling
            # tijdens een delegatie.
            if (
                _agent_steps() is None
                and _router_depth == 0
                and _status_channel_id
                and (tc.get("function") or {}).get("name")
                    == tool_dispatch.ROUTER_TOOL_SLUG
            ):
                agent._daadit_post_channel_status(
                    _status_channel_id,
                    agent._daadit_delegation_status_body(
                        _delegate_target_name(tc)
                    ),
                )

            result = tool_dispatch.run_tool_call(agent, tc)

            # A routed answer is the slowest step of all, so close it
            # off explicitly rather than leaving the last line hanging.
            if (
                (tc.get("function") or {}).get("name")
                == tool_dispatch.ROUTER_TOOL_SLUG
                and isinstance(result, dict)
            ):
                _wie = _safe_name(result.get("agent"))
                if result.get("ok"):
                    _notify_step(
                        agent,
                        ("%s heeft geantwoord, ik verwerk het" % _wie)
                        if _wie
                        else "De collega heeft geantwoord, ik verwerk het",
                        depth=_router_depth, kind="route",
                    )
                elif result.get("error"):
                    _notify_step(agent, "Dat lukte niet, ik probeer het anders",
                                 depth=_router_depth, kind="route")

            # v19.0.4.4.0: Fase 0 governance — tally hallucinated tool
            # names. Two "Unknown tool: ..." errors in a single turn
            # means the model is guessing wildly; stop instead of
            # feeding more errors back for it to hallucinate around.
            if isinstance(result, dict) and "Unknown tool:" in str(
                result.get("error", "")
            ):
                unknown_tool_count += 1
                if unknown_tool_count >= 2:
                    unknown_tool_break = True

            # If the tool was denied by the agent's allow/block list,
            # break the loop and surface a clean user-facing message
            # below. We do NOT feed the error back to Mistral, because
            # it tends to mis-paraphrase admin-policy denials as
            # "I made a mistake" recoveries (observed on staging).
            if isinstance(result, dict) and result.get("_daadit_access_denied"):
                # v19.0.6.8.0: at depth 0, a denial is not the end of the
                # road — it usually means the work belongs to a colleague.
                # The concierge's own instruction already said "route
                # instead of showing the block", but that rule could never
                # fire: this break ended the turn before he could act, and
                # the formatted denial went straight to the user. Robin
                # kept pasting "🔒 Toegang geblokkeerd" for models Sem is
                # perfectly allowed to read.
                #
                # So the redirect happens here, in code, not in a prompt.
                _denied_model = result.get("model_name")
                _colleagues = []
                _can_route = False
                try:
                    _can_route = _router_depth == 0 and any(
                        (t.get("function") or {}).get("name")
                        == tool_dispatch.ROUTER_TOOL_SLUG
                        for t in (active_tools or [])
                    )
                except Exception:  # noqa: BLE001
                    _can_route = False
                if _can_route and scope_redirects < 2:
                    _colleagues = _colleagues_allowed_for_model(
                        agent, _denied_model
                    )
                if _colleagues:
                    scope_redirects += 1
                    _logger.info(
                        "daadit_ai_mistral.llm_api_patch: scope denial on "
                        "%s rewritten as a routing instruction to %s "
                        "(redirect %d of 2)",
                        _denied_model, _colleagues[0].name, scope_redirects,
                    )
                    result = _scope_redirect_result(
                        agent, result, _colleagues
                    )
                    # Fall through: the instruction is appended as a normal
                    # tool result and the loop continues, so the model can
                    # actually make the routing call.
                else:
                    access_denial = result
                    break

            # v19.0.6.5.4: concierge pass-through (see init above).
            # Conditions: top-level turn, feature on, the ONLY tool call
            # of this assistant message, it is the router tool, and it
            # returned a real specialist answer.
            if (
                router_passthrough is None
                and _router_depth == 0
                and _passthrough_enabled
                and len(tool_calls) == 1
                and (tc.get("function") or {}).get("name")
                == tool_dispatch.ROUTER_TOOL_SLUG
                and isinstance(result, dict)
                and result.get("ok")
                and str(result.get("answer") or "").strip()
            ):
                if _answer_looks_degenerate(result.get("answer")):
                    # Runaway specialist answer: do NOT relay it. Fall
                    # through to the normal path so the concierge
                    # summarises and the user gets something readable.
                    _logger.warning(
                        "daadit_ai_mistral.llm_api_patch: pass-through "
                        "SKIPPED for specialist=%s — answer looks "
                        "degenerate (%d chars); falling back to "
                        "concierge summary",
                        result.get("agent") or "?",
                        len(str(result.get("answer") or "")),
                    )
                else:
                    router_passthrough = result
                    break

            try:
                content = json.dumps(result, default=str)
            except Exception:  # noqa: BLE001
                content = str(result)
            conversation.append({
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "name": (tc.get("function") or {}).get("name"),
                "content": content,
            })

        if access_denial or unknown_tool_break or router_passthrough:
            break

        iteration += 1
        _logger.info(
            "daadit_ai_mistral.llm_api_patch: tool iteration %d, "
            "%d tool_call(s) executed",
            iteration, len(tool_calls),
        )

    # v19.0.4.4.0: unified bail-out handling. Two independent conditions
    # can end the tool-call loop unsuccessfully: the MAX_ITER budget is
    # exhausted, or the model kept hallucinating tool names. Both mean
    # "the agent didn't produce a real answer" and should surface an
    # explicit, user-facing message plus set the top_level_exhausted
    # flag so callers (notably daadit_ai_agent_schedule) can reflect
    # the failure in their own state ('error' instead of 'done').
    bail_reason = None
    if deadline_break:
        bail_reason = "deadline"
    elif iteration >= MAX_ITER:
        bail_reason = "max_iter"
    elif unknown_tool_break:
        bail_reason = "unknown_tools"

    if bail_reason:
        _logger.warning(
            "daadit_ai_mistral.llm_api_patch: bail out reason=%s "
            "(iteration=%d MAX_ITER=%d unknown_tool_count=%d)",
            bail_reason, iteration, MAX_ITER, unknown_tool_count,
        )
        # v19.0.4.2.1: flag budget exhaustion so the router tool
        # (_ai_tool_ask_agent) can report failure instead of relaying
        # truncated narration or the panel fallback sentinel as if it
        # were a real specialist answer. Only meaningful inside a
        # routed sub-run (depth > 0).
        try:
            _bail_depth = getattr(tool_dispatch.router_state, "depth", 0)
        except Exception:  # noqa: BLE001
            _bail_depth = 0
        try:
            if _bail_depth > 0:
                tool_dispatch.router_state.exhausted = True
        except Exception:  # noqa: BLE001
            pass
        # v19.0.4.4.0: top-level flag read by the schedule module.
        # Namespaced separately from ``exhausted`` (sub-run only) so
        # each caller can inspect its own state without interference.
        #
        # v19.0.6.5.6: only a DEPTH-0 bail may set it. Before this the
        # assignment was unconditional, so a sub-run hitting its
        # (deliberately tighter) budget wrote the TOP-LEVEL flag on the
        # shared threadlocal. ``daadit_ai_agent_schedule`` reads that
        # flag after ``request_llm`` returns and marked the whole
        # scheduled run 'error' — even when the parent finished and
        # produced a complete report. Three in a row tripped the
        # circuit breaker, which is what silently disabled Eva's
        # monthly report (schedule 37) on 29-07-2026 and would hit any
        # delegating agent. The parent still learns a sub-run failed
        # via ``sub_failed``, which ``_ai_tool_ask_agent`` saves and
        # restores around every sub-run.
        if _bail_depth == 0:
            try:
                tool_dispatch.router_state.top_level_exhausted = True
                tool_dispatch.router_state.exhaustion_reason = bail_reason
            except Exception:  # noqa: BLE001
                pass
        if bail_reason == "deadline":
            en_message = (
                "This run reached its hard time budget before the "
                "task was finished. Partial work may have been done; "
                "check the run log. Consider a narrower instruction "
                "or a higher time budget."
            )
        elif bail_reason == "max_iter":
            en_message = (
                f"I couldn't finish this in {MAX_ITER} tool-call steps. "
                f"Try asking a more specific question, or break your "
                f"request into smaller pieces."
            )
        else:  # unknown_tools
            en_message = (
                f"The agent tried to call {unknown_tool_count} tool "
                f"names that don't exist. I stopped it to avoid burning "
                f"the call budget. Check the agent's topic configuration."
            )
        adapted = [
            _translate_to_chat_language(
                client, model, conversation, en_message,
            )
        ]
        usage = (response.get("usage") or {}) if isinstance(response, dict) else {}
        try:
            ch = api_self.env.context.get("discuss_channel")
            channel_id = ch.id if ch and hasattr(ch, "id") else (
                ch if isinstance(ch, int) else False
            )
            api_self.env["daadit_ai_mistral.usage"].sudo().record_usage(
                kind="chat", model=model,
                agent_id=agent.id if agent else False,
                channel_id=channel_id,
                prompt_tokens=usage.get("prompt_tokens") or 0,
                completion_tokens=usage.get("completion_tokens") or 0,
                iterations=iteration,
                has_tools=bool(normalized_tools),
                error=f"bail:{bail_reason}",
                depth=_router_depth,
                turn_uuid=getattr(
                    tool_dispatch.router_state, "turn_uuid", None,
                ),
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral.llm_api_patch: usage row creation "
                "failed during bail; continuing"
            )
        return adapted

    usage = (response.get("usage") or {}) if isinstance(response, dict) else {}

    # If the loop short-circuited on admin-policy denial, format the
    # user-facing message NOW and bypass Mistral's interpretation
    # entirely. Mistral never sees the denial; the user gets a clear
    # statement of what's blocked and by whom — translated into the
    # language of the conversation (overrides env.user.lang).
    if access_denial:
        en_message = _format_access_denied_message(access_denial)
        translated = _translate_to_chat_language(
            client, model, conversation, en_message,
        )
        adapted = [translated]
        _logger.info(
            "daadit_ai_mistral.llm_api_patch: chat short-circuited "
            "by admin-policy denial — model=%s requested=%s",
            model, access_denial.get("model_name"),
        )
        # Inside a routed sub-run, a policy denial is NOT an answer.
        # Left unflagged, ``_ai_tool_ask_agent`` relays "🔒 Access
        # blocked by your administrator" as ``{'ok': True, 'answer': …}``
        # and the delegating manager treats it as a delivered result —
        # Eva's monthly report (run 463) then invented the figures Bram
        # and Sanne never produced. Flag it so the router returns an
        # error that names the blocked model instead.
        try:
            if getattr(tool_dispatch.router_state, "depth", 0) > 0:
                tool_dispatch.router_state.sub_failed = True
                tool_dispatch.router_state.sub_denied_model = (
                    access_denial.get("model_name") or ""
                )
        except Exception:  # noqa: BLE001
            pass
        # Token-usage from the truncated call is still worth recording.
        try:
            ch = api_self.env.context.get("discuss_channel")
            channel_id = ch.id if ch and hasattr(ch, "id") else (
                ch if isinstance(ch, int) else False
            )
            api_self.env["daadit_ai_mistral.usage"].sudo().record_usage(
                kind="chat", model=model,
                agent_id=agent.id if agent else False,
                channel_id=channel_id,
                prompt_tokens=usage.get("prompt_tokens") or 0,
                completion_tokens=usage.get("completion_tokens") or 0,
                iterations=iteration + 1,
                has_tools=bool(normalized_tools),
                error=f"Access denied: {access_denial.get('model_name')}",
                depth=_router_depth,
                turn_uuid=getattr(
                    tool_dispatch.router_state, "turn_uuid", None,
                ),
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral.llm_api_patch: usage row creation "
                "failed during access-denial; continuing"
            )
        return adapted

    # v19.0.6.5.4: concierge pass-through — relay the routed specialist's
    # answer verbatim and skip the final paraphrasing generation.
    if router_passthrough:
        _pt_answer = str(router_passthrough.get("answer") or "").strip()
        _pt_answer, _pt_trimmed = _strip_runaway_and_leaks(_pt_answer)
        if _pt_trimmed:
            _logger.warning(
                "daadit_ai_mistral.llm_api_patch: doorgegeven antwoord van "
                "%s gesnoeid v\u00f3\u00f3r verzending",
                router_passthrough.get("agent") or "?",
            )
            _pt_answer = _pt_answer or _EMPTY_AFTER_STRIP
        _pt_agent = router_passthrough.get("agent") or "?"
        _via = agent.name if agent is not None else "concierge"
        adapted = ["%s\n\n— %s · via %s" % (_pt_answer, _pt_agent, _via)]
        _logger.info(
            "daadit_ai_mistral.llm_api_patch: router pass-through "
            "(specialist=%s chars=%d) — final generation skipped",
            _pt_agent, len(_pt_answer),
        )
        try:
            ch = api_self.env.context.get("discuss_channel")
            channel_id = ch.id if ch and hasattr(ch, "id") else (
                ch if isinstance(ch, int) else False
            )
            api_self.env["daadit_ai_mistral.usage"].sudo().record_usage(
                kind="chat", model=model,
                agent_id=agent.id if agent else False,
                channel_id=channel_id,
                prompt_tokens=usage.get("prompt_tokens") or 0,
                completion_tokens=usage.get("completion_tokens") or 0,
                iterations=iteration + 1,
                has_tools=bool(normalized_tools),
                depth=_router_depth,
                turn_uuid=getattr(
                    tool_dispatch.router_state, "turn_uuid", None,
                ),
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral.llm_api_patch: usage row creation "
                "failed during pass-through; continuing"
            )
        return adapted

    adapted = _adapt_response_to_text_messages(response)
    # De eigen tekst van de agent gaat door dezelfde zeef als een
    # doorgegeven antwoord: intern verkeer en een doorgeslagen staart
    # horen niet in de chat.
    adapted = _clean_adapted(adapted, "eindantwoord")
    _logger.info(
        "daadit_ai_mistral.llm_api_patch: Mistral chat ok "
        "(model=%s iterations=%d tokens=%s/%s text_chunks=%d "
        "agent_seen=%s)",
        model,
        iteration + 1,
        usage.get("prompt_tokens", "?"),
        usage.get("completion_tokens", "?"),
        len(adapted),
        bool(agent),
    )

    # ---- Persist usage row -----------------------------------------
    try:
        ch = api_self.env.context.get("discuss_channel")
        channel_id = ch.id if ch and hasattr(ch, "id") else (
            ch if isinstance(ch, int) else False
        )
        api_self.env["daadit_ai_mistral.usage"].sudo().record_usage(
            kind="chat",
            model=model,
            agent_id=agent.id if agent else False,
            channel_id=channel_id,
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            iterations=iteration + 1,
            has_tools=bool(normalized_tools),
            depth=_router_depth,
            turn_uuid=getattr(
                tool_dispatch.router_state, "turn_uuid", None,
            ),
        )
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.llm_api_patch: usage row creation failed; "
            "continuing"
        )

    if not adapted:
        # Mistral returned only tool_calls AND we couldn't loop
        # (probably because no agent was found in the threadlocal).
        first_choice_msg = (
            (response.get("choices") or [{}])[0].get("message") or {}
        ) if isinstance(response, dict) else {}
        # v19.0.4.2.4: flag this as a failed sub-run BEFORE translating,
        # so the router (_ai_tool_ask_agent) can detect the failure
        # language-independently. Previously the sentinel was translated
        # to the chat language (e.g. Dutch), so the router's English
        # prefix filter missed it and relayed the confusing "kon de AI
        # Agent-record niet vinden" sentinel to the user as a real
        # answer. The flag closes that leak regardless of translation.
        try:
            if getattr(tool_dispatch.router_state, "depth", 0) > 0:
                tool_dispatch.router_state.sub_failed = True
        except Exception:  # noqa: BLE001
            pass
        if first_choice_msg.get("tool_calls"):
            fallback_en = (
                "_(Mistral wanted to call a function but I couldn't "
                "find the AI Agent record to dispatch it. Try sending "
                "the message from inside the AI chat panel.)_"
            )
        else:
            fallback_en = "_(Empty response from Mistral.)_"
        # Same chat-language translation as the access-denied path.
        adapted = [
            _translate_to_chat_language(
                client, model, conversation, fallback_en,
            )
        ]
    # Fair use bovenaan het antwoord, niet als losse melding: een aparte
    # systeemregel leest als een storing, terwijl dit gewoon bij het
    # antwoord hoort. Eén regel, in de taal van het gesprek.
    if fair_use_notice:
        try:
            line = _translate_to_chat_language(
                client, model, conversation, fair_use_notice,
            )
            text = line.get("content") if isinstance(line, dict) else ""
            if text and adapted and isinstance(adapted[0], dict):
                adapted[0]["content"] = "_%s_\n\n%s" % (
                    text.strip().strip("_"),
                    adapted[0].get("content") or "",
                )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral.llm_api_patch: kon de fair-use-regel "
                "niet toevoegen; antwoord gaat ongewijzigd door"
            )
    return adapted


_FIRST_CALL_LOGGED = False


def _safe_repr(v, limit=600):
    try:
        s = repr(v)
    except Exception:  # noqa: BLE001
        try:
            s = str(v)
        except Exception:  # noqa: BLE001
            return "<unrenderable>"
    return s if len(s) <= limit else s[:limit] + f"…<truncated, full len={len(s)}>"


def _redact_value_for_log(v):
    """Return a one-line log fragment for ``v`` that does NOT echo any
    user content.

    SEC H2 — this used to be ``_safe_repr(v, limit=600)`` which would
    happily dump the user's first chat message into operational logs.
    Operational logs at Odoo.sh have a 30+ day retention and are
    typically not under GDPR scope, so PII landing there is a real
    leak. Now we log only structural info: type, length / size, and
    for dicts the set of keys.
    """
    t = type(v).__name__
    if v is None:
        return "type=NoneType"
    if isinstance(v, bool):
        return f"type={t} value={v}"
    if isinstance(v, (int, float)):
        return f"type={t}"
    if isinstance(v, str):
        return f"type=str len={len(v)}"
    if isinstance(v, (list, tuple)):
        item_types = sorted({type(x).__name__ for x in v[:10]})
        return f"type={t} len={len(v)} item_types={item_types}"
    if isinstance(v, dict):
        keys = sorted(str(k) for k in list(v.keys())[:30])
        return f"type=dict len={len(v)} keys={keys}"
    if isinstance(v, (bytes, bytearray)):
        return f"type={t} len={len(v)}"
    return f"type={t}"


def _log_first_call_args(args, kwargs):
    """One-shot structural log of the actual ``request_llm`` signature.

    SEC H2: we log ONLY structural info (types, lengths, dict keys) —
    never the values. The first-call diagnostic was originally meant
    to discover the stock kwarg names; that's been done since v3.6.x,
    so the log line is now mostly redundant. We keep it as a smoke
    test (helps confirm the patch is wired up after a build) but with
    zero PII risk.
    """
    global _FIRST_CALL_LOGGED
    if _FIRST_CALL_LOGGED:
        return
    _FIRST_CALL_LOGGED = True
    _logger.info(
        "daadit_ai_mistral.llm_api_patch: FIRST request_llm(mistral) "
        "call — positional=%d kwargs=%d kw_keys=%s",
        len(args), len(kwargs), sorted(kwargs.keys()),
    )
    for i, a in enumerate(args):
        _logger.info("  arg[%d] %s", i, _redact_value_for_log(a))
    for k, v in kwargs.items():
        _logger.info("  kw[%s] %s", k, _redact_value_for_log(v))
