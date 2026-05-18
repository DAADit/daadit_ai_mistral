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

    def _patched_init(api_self, env=None, provider=None, *args, **kwargs):
        if provider == "mistral":
            # Skip stock's provider-validation that raises NotImplementedError
            # and set up the bare minimum state Mistral routing needs.
            api_self.env = env
            api_self.provider = "mistral"
            return None
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

    LLMApiService.__init__ = _patched_init
    if original_request_llm is not None:
        LLMApiService.request_llm = _patched_request_llm
    LLMApiService._daadit_mistral_patched = True
    LLMApiService._daadit_original_init = original_init
    LLMApiService._daadit_original_request_llm = original_request_llm

    _PATCHED = True
    _logger.info(
        "daadit_ai_mistral.llm_api_patch: LLMApiService.__init__/request_llm "
        "patched to support provider='mistral' "
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
        if ch is None:
            return None
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
    return None


_LANGUAGE_MIRROR_INSTRUCTION = (
    "IMPORTANT: Always respond in the same language as the user's most "
    "recent message. If the user writes in Dutch, reply in Dutch. If "
    "the user writes in French, reply in French. Do NOT translate "
    "technical identifiers inside backticks (such as `account.move`, "
    "`res.partner`, field names like `stage_id`)."
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
    if not messages:
        prompt = next(
            (kwargs.get(k) for k in _PROMPT_KEYS if kwargs.get(k)),
            None,
        )
        system = next(
            (kwargs.get(k) for k in _SYSTEM_KEYS if kwargs.get(k)),
            None,
        )
        if prompt or system:
            messages = []
            if system:
                messages.append({"role": "system", "content": str(system)})
            if prompt:
                messages.append({"role": "user", "content": str(prompt)})

    # --- Defaults / hard validation ----------------------------------
    if not model:
        _logger.warning(
            "daadit_ai_mistral.llm_api_patch: no 'model' found in "
            "request_llm call; falling back to mistral-medium-latest"
        )
        model = "mistral-medium-latest"

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
    iteration = 0
    MAX_ITER = 6
    response = None
    access_denial = None  # set when a tool call is denied by admin policy

    while iteration < MAX_ITER:
        response = client.chat_completion(
            model=model,
            messages=conversation,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=normalized_tools,
            tool_choice=tool_choice,
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
