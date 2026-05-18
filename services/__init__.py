# -*- coding: utf-8 -*-
# Import diagnostics FIRST so the UserError trace tap is in place before
# any other module code can raise.
from . import diagnostics  # noqa: F401  (side-effect import)
from . import mistral_client
from . import registry_patches
from . import tool_dispatch
from . import llm_api_patch

# Best-effort: try to install the LLMApiService patch right now, at import
# time. This usually fails because stock's
# ``odoo.addons.ai.utils.llm_api_service`` hasn't been imported yet —
# that's expected; the patch is also retried from ai.agent's
# ``_register_hook`` (after stock is fully loaded) and from
# ``_get_provider`` (just-in-time, immediately before LLMApiService is
# instantiated in stock's ``_generate_response``). Swallowing here is
# intentional.
import logging as _logging
_log = _logging.getLogger(__name__)
try:
    if llm_api_patch.patch_llm_api_service():
        _log.info(
            "daadit_ai_mistral.services: LLMApiService patched at import time"
        )
    else:
        _log.info(
            "daadit_ai_mistral.services: LLMApiService not yet importable "
            "at services/__init__.py time — will retry from _register_hook "
            "and _get_provider"
        )
except Exception:  # noqa: BLE001
    _log.exception(
        "daadit_ai_mistral.services: import-time LLMApiService patch raised"
    )
