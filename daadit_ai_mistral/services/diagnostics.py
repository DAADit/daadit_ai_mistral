# -*- coding: utf-8 -*-
"""Process-wide diagnostic instrumentation for the Mistral provider.

What happens at import time:

1. **Module-load marker** — a single ``_logger.info(...)`` line so that
   you can confirm from Odoo.sh logs that the current version is
   actually loaded (vs. an older cached build that doesn't have the
   patches).

2. **UserError stack-trace tap** — ``odoo.exceptions.UserError.__init__``
   is wrapped so that whenever a ``UserError`` is constructed with a
   message containing one of our target markers (e.g. ``"No embedding
   model found for the selected provider"``), the full Python stack
   that led there is logged at WARNING level.

   This tap is **disabled by default** because it's process-wide:
   every ``UserError`` ever constructed in Odoo, anywhere — record
   rules, security validations, account locks, etc. — pays a
   ``str(message_arg)`` + substring scan on every raise, even if it
   isn't ours. With high-volume validation pages (account, sale order
   forms) that can add measurable latency.

   The tap was instrumental in v3.5.x to find out where the provider
   lookup actually lived. Now that we know (``odoo.addons.ai.utils.
   llm_providers.get_provider`` and stock's
   ``ai_agent.write → _get_provider`` chain), it earns its keep only
   for follow-up debugging. Re-enable per-database via::

       ir.config_parameter:
         daadit_ai_mistral.diag_trace_user_errors = True
"""
import logging
import traceback

_logger = logging.getLogger(__name__)

# Markers we care about. Keep in sync with services/registry_patches.py.
_TARGET_MARKERS = (
    "No embedding model found",
    "No provider found for the selected",
)

_PATCH_INSTALLED = False


def _install_user_error_trace_tap():
    """Idempotently wrap ``odoo.exceptions.UserError.__init__``."""
    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return
    try:
        from odoo.exceptions import UserError
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.diagnostics: cannot import UserError; "
            "skipping trace tap."
        )
        return

    original_init = UserError.__init__
    if getattr(original_init, "_daadit_diag_wrapped", False):
        _PATCH_INSTALLED = True
        return

    def _wrapped_init(self, *args, **kwargs):
        # Resolve the message argument into a string we can scan. Odoo
        # frequently passes a lazy-translation object as the first arg.
        message_arg = None
        if args:
            message_arg = args[0]
        elif "message" in kwargs:
            message_arg = kwargs["message"]
        try:
            message_str = str(message_arg) if message_arg is not None else ""
        except Exception:  # noqa: BLE001
            message_str = ""

        if any(marker in message_str for marker in _TARGET_MARKERS):
            stack = "".join(traceback.format_stack()[:-1])
            _logger.warning(
                "daadit_ai_mistral.diagnostics: UserError raised with target "
                "message %r — caller stack:\n%s",
                message_str, stack,
            )

        return original_init(self, *args, **kwargs)

    _wrapped_init._daadit_diag_wrapped = True
    UserError.__init__ = _wrapped_init
    _PATCH_INSTALLED = True
    _logger.info(
        "daadit_ai_mistral.diagnostics: UserError trace tap installed "
        "for markers %r",
        _TARGET_MARKERS,
    )


_logger.info("daadit_ai_mistral: diagnostics module loaded")


def maybe_install_trace_tap_from_env(env):
    """Conditionally install the UserError trace tap based on the
    ``daadit_ai_mistral.diag_trace_user_errors`` ICP. Called from
    ``ai.agent._register_hook`` so we have an env to read config from.
    """
    try:
        flag = env["ir.config_parameter"].sudo().get_param(
            "daadit_ai_mistral.diag_trace_user_errors", default="False"
        )
    except Exception:  # noqa: BLE001
        return False
    enabled = str(flag).strip().lower() in ("1", "true", "t", "yes", "y")
    if enabled:
        _install_user_error_trace_tap()
        return True
    _logger.debug(
        "daadit_ai_mistral.diagnostics: UserError trace tap disabled "
        "(set ir.config_parameter daadit_ai_mistral.diag_trace_user_errors "
        "= True to enable for debugging)"
    )
    return False
