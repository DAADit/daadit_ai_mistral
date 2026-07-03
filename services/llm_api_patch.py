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

from .mistral_client import MistralClient, is_mistral_model
from . import tool_dispatch

_logger = logging.getLogger(__name__)

_PATCHED = False


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
            mode = (icp.get_param("daadit_ai_mistral.force_provider") or "").strip().lower()
            forced = (icp.get_param("daadit_ai_mistral.force_provider_name") or "mistral").strip().lower()
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
        try:
            return _request_llm_mistral(api_self, *args, **kwargs)
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
            if original_request_llm_internal is None:
                raise AttributeError(
                    "LLMApiService._request_llm not found on stock; "
                    "cannot delegate non-Mistral call."
                )
            return original_request_llm_internal(api_self, *args, **kwargs)
        try:
            adapted = _request_llm_mistral(api_self, *args, **kwargs)
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

    LLMApiService.__init__ = _patched_init
    if original_request_llm is not None:
        LLMApiService.request_llm = _patched_request_llm
    if original_request_llm_internal is not None:
        LLMApiService._request_llm = _patched_request_llm_internal
    LLMApiService._daadit_mistral_patched = True
    LLMApiService._daadit_original_init = original_init
    LLMApiService._daadit_original_request_llm = original_request_llm
    LLMApiService._daadit_original_request_llm_internal = original_request_llm_internal

    _PATCHED = True
    _logger.info(
        "daadit_ai_mistral.llm_api_patch: LLMApiService.__init__/"
        "request_llm/_request_llm patched to support provider='mistral' "
        "(class: %s.%s)",
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
            model = d.get(k); break
    for k in _MESSAGE_KEYS:
        if not messages and k in d:
            cand = d.get(k)
            normalized = _normalize_messages(cand)
            if normalized:
                messages = normalized; break
    for k in _TOOLS_KEYS:
        if tools is None and k in d:
            tools = d.get(k); break
    for k in _TOOL_CHOICE_KEYS:
        if tool_choice is None and k in d:
            tool_choice = d.get(k); break
    for k in _TEMPERATURE_KEYS:
        if temperature is None and k in d:
            temperature = d.get(k); break
    for k in _MAX_TOKENS_KEYS:
        if max_tokens is None and k in d:
            max_tokens = d.get(k); break
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

    # Pull the last user-authored message as the language reference.
    user_msgs = [
        m for m in ref_messages
        if isinstance(m, dict)
        and m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m.get("content").strip()
    ]
    if not user_msgs:
        return text
    last_user = user_msgs[-1]["content"]

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


def _resolve_agent(api_self):
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

    # Fallback 4 (v19.0.4.1.3): call-stack walk. The Enterprise AI
    # chat-panel controller constructs LLMApiService directly — our
    # ai.agent._get_provider never runs on that path, so neither the
    # threadlocal nor the discuss-channel context is populated
    # (observed on prod 2026-07-03 15:28 UTC: Mistral returned
    # tool_calls, dispatch had no agent, user got the "couldn't find
    # the AI Agent record" fallback). The controller *does* hold the
    # agent record in a local variable somewhere up-stack. Walk the
    # frames and pick the nearest local that is an ai.agent
    # singleton. Read-only inspection; the returned record is
    # re-browsed on the caller's env so RBAC is preserved. Bounded
    # depth keeps this cheap and safe.
    try:
        import sys
        frame = sys._getframe(1)
        depth = 0
        while frame is not None and depth < 30:
            for val in frame.f_locals.values():
                try:
                    if (
                        getattr(val, "_name", None) == "ai.agent"
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
                except Exception:  # noqa: BLE001
                    continue
            frame = frame.f_back
            depth += 1
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.llm_api_patch: stack-walk agent lookup "
            "raised; continuing without agent"
        )
    return None


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
    "tell the user in plain language which capability is missing."
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

    # ---- Build proper tool definitions ------------------------------
    # If stock sent us a list of tool name strings, use the JSON-schema
    # definitions from ``tool_dispatch.TOOL_SCHEMAS`` for the standard
    # ten AI tools (Search / Read group / Get Fields / Open Menu *).
    # For anything else (already-shaped dicts, etc.), fall back to the
    # generic _normalize_tools converter from earlier versions.
    normalized_tools = None
    if tools:
        if isinstance(tools, (list, tuple)) and all(isinstance(t, str) for t in tools):
            normalized_tools = tool_dispatch.annotate_tools(tools)
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
    agent = _resolve_agent(api_self)

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
    conversation = _inject_language_mirror(list(messages))
    conversation = _inject_runtime_context(conversation)
    iteration = 0
    MAX_ITER = 6
    response = None
    access_denial = None  # set when a tool call is denied by admin policy

    while iteration < MAX_ITER:
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
            result = tool_dispatch.run_tool_call(agent, tc)

            # If the tool was denied by the agent's allow/block list,
            # break the loop and surface a clean user-facing message
            # below. We do NOT feed the error back to Mistral, because
            # it tends to mis-paraphrase admin-policy denials as
            # "I made a mistake" recoveries (observed on staging).
            if isinstance(result, dict) and result.get("_daadit_access_denied"):
                access_denial = result
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

        if access_denial:
            break

        iteration += 1
        _logger.info(
            "daadit_ai_mistral.llm_api_patch: tool iteration %d, "
            "%d tool_call(s) executed",
            iteration, len(tool_calls),
        )

    if iteration >= MAX_ITER:
        _logger.warning(
            "daadit_ai_mistral.llm_api_patch: hit MAX_ITER=%d in tool "
            "loop; returning whatever final response we have",
            MAX_ITER,
        )

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
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral.llm_api_patch: usage row creation "
                "failed during access-denial; continuing"
            )
        return adapted

    adapted = _adapt_response_to_text_messages(response)
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
