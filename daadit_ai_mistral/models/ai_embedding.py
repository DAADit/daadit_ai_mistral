# -*- coding: utf-8 -*-
"""Extends ``ai.embedding`` to support Mistral's ``mistral-embed`` model.

Background
----------
Stock Odoo 19 Enterprise ``ai.embedding`` has a hardcoded selection on
``embedding_model`` with two options:

    * ``text-embedding-3-small``  → OpenAI
    * ``gemini-embedding-001``    → Google

Both go through Odoo IAP unless a custom OpenAI/Google key is set in
General Settings → AI. We add ``mistral-embed`` and route it to
``POST https://api.mistral.ai/v1/embeddings``.

Hookpoint caveat
----------------
We do not (yet) know the exact name of the method on ``ai.embedding`` that
performs the actual HTTP embedding call. The method is invoked from the
"AI Embedding: Generate Embeddings" server action (id 1657 on staging),
which calls some method on ``ai.embedding``. Likely candidates, in order:

    * ``_generate_embeddings``   (most likely)
    * ``_compute_embeddings``
    * ``_create_embeddings``
    * ``_call_embedding_api``

As with ai.agent, we declare overrides for all candidates. The one that
matches the real Enterprise method intercepts; the others are dead methods.

Once verified against the Enterprise source, simplify to keep only the
override that actually runs.
"""
import logging

from odoo import api, fields, models

from ..services.mistral_client import (
    EMBEDDING_MODEL as MISTRAL_EMBED,
    MistralClient,
    is_mistral_embedding_model,
)
from ..services import registry_patches

_logger = logging.getLogger(__name__)


# Selection entries we add to ai.embedding.embedding_model.
# Label format mirrors stock entries: ``("technical_id", "Provider Name")``.
MISTRAL_EMBEDDING_SELECTION = [
    (MISTRAL_EMBED, "Mistral"),
]


class AIEmbedding(models.Model):
    _inherit = "ai.embedding"

    # ------------------------------------------------------------------ #
    # Selection extension                                                #
    #                                                                    #
    # The stock ``ai.embedding.embedding_model`` is a STATIC selection   #
    # — we append via ``selection_add=``.                                #
    #                                                                    #
    # ``ondelete`` can't use ``'set default'`` here because the stock    #
    # field is ``required=True`` without a ``default=`` value (asserted  #
    # by Odoo's fields_selection.py at field-setup time). Instead we     #
    # provide an explicit fallback: if our ``mistral-embed`` option is   #
    # removed (e.g. module uninstall), existing chunks switch to         #
    # ``text-embedding-3-small`` — the OpenAI option that's always       #
    # present in the stock selection. Re-running the embedding cron      #
    # will regenerate the vectors against whichever provider is then     #
    # active.                                                            #
    # ------------------------------------------------------------------ #
    embedding_model = fields.Selection(
        selection_add=MISTRAL_EMBEDDING_SELECTION,
        ondelete={key: "set text-embedding-3-small"
                  for key, _label in MISTRAL_EMBEDDING_SELECTION},
    )

    # ------------------------------------------------------------------ #
    # Mistral embedding generation                                       #
    # ------------------------------------------------------------------ #

    def _daadit_call_mistral_embeddings(self, texts):
        """POST a batch of strings to Mistral's /embeddings endpoint.

        Returns a list of vectors aligned with ``texts``. Empty inputs
        return a zero-length list (caller decides what to do).
        """
        client = MistralClient.from_env(self.env)
        response = client.embeddings(
            inputs=list(texts),
            model=MISTRAL_EMBED,
        )
        usage = MistralClient.extract_usage(response)
        vectors = MistralClient.extract_embeddings(response)
        _logger.info(
            "Mistral embed ok: count=%d tokens=%s",
            len(vectors),
            usage.get("total_tokens", "?"),
        )
        # Token-tracking row.
        try:
            self.env["daadit_ai_mistral.usage"].sudo().record_usage(
                kind="embedding",
                model=MISTRAL_EMBED,
                prompt_tokens=usage.get("total_tokens") or usage.get("prompt_tokens") or 0,
                completion_tokens=0,
                iterations=1,
                has_tools=False,
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral: embedding usage row creation failed"
            )
        return vectors

    def _daadit_should_route_to_mistral(self):
        """Return True if all selected records use the Mistral embedding model."""
        return bool(self) and all(
            is_mistral_embedding_model(rec.embedding_model) for rec in self
        )

    # ------------------------------------------------------------------ #
    # Candidate dispatch overrides                                       #
    # ------------------------------------------------------------------ #

    def _generate_embeddings(self, *args, **kwargs):
        if self._daadit_should_route_to_mistral():
            return self._daadit_run_embedding_pipeline()
        try:
            return super()._generate_embeddings(*args, **kwargs)
        except AttributeError:
            _logger.warning(
                "daadit_ai_mistral: ai.embedding has no _generate_embeddings — "
                "check the real method name in the Enterprise source."
            )
            raise

    def _compute_embeddings(self, *args, **kwargs):
        if self._daadit_should_route_to_mistral():
            return self._daadit_run_embedding_pipeline()
        try:
            return super()._compute_embeddings(*args, **kwargs)
        except AttributeError:
            raise

    def _create_embeddings(self, *args, **kwargs):
        if self._daadit_should_route_to_mistral():
            return self._daadit_run_embedding_pipeline()
        try:
            return super()._create_embeddings(*args, **kwargs)
        except AttributeError:
            raise

    def _call_embedding_api(self, *args, **kwargs):
        if self._daadit_should_route_to_mistral():
            return self._daadit_run_embedding_pipeline()
        try:
            return super()._call_embedding_api(*args, **kwargs)
        except AttributeError:
            raise

    # ------------------------------------------------------------------ #
    # Registry hook — mirrors the one on ai.agent. The provider→         #
    # embedding-model lookup might be implemented as a method or class   #
    # dict on ai.embedding rather than ai.agent, so we run the same      #
    # bytecode + dict scan here.                                         #
    # ------------------------------------------------------------------ #

    @api.model
    def _register_hook(self):
        res = super()._register_hook()
        try:
            self._daadit_install_provider_patches()
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral: provider patching on ai.embedding failed"
            )
        return res

    @api.model
    def _daadit_install_provider_patches(self):
        cls = type(self)

        targets = registry_patches.discover_lookup_methods(cls)
        method_names = [name for name, _base in targets]
        if targets:
            _logger.info(
                "daadit_ai_mistral: ai.embedding bytecode scan found "
                "embedding-lookup methods: %s",
                [(n, b.__module__) for n, b in targets],
            )

        # On ai.embedding records the relevant signal is ``embedding_model``.
        # On the call site that fires from ai.agent's write() the ``self``
        # may actually be an ``ai.agent`` recordset (because the lookup
        # method might live on ai.embedding but be invoked via an
        # ``self.env['ai.embedding'].…`` proxy that passes the agent's
        # provider). We accept both.
        def _is_target(rec):
            try:
                em = getattr(rec, "embedding_model", None)
                if em and is_mistral_embedding_model(em):
                    return True
                lm = getattr(rec, "llm_model", None)
                if lm and is_mistral_model(lm):
                    return True
            except Exception:  # noqa: BLE001
                pass
            return False

        patched_methods = registry_patches.install_method_overrides(
            cls,
            method_names,
            is_target_record=_is_target,
            target_return_value=MISTRAL_EMBED,
            log_label="daadit_ai_mistral[ai.embedding]",
        )

        dict_specs = registry_patches.discover_provider_dicts(cls)
        patched_dicts = registry_patches.patch_provider_dicts(
            dict_specs,
            log_label="daadit_ai_mistral[ai.embedding]",
        )

        _logger.info(
            "daadit_ai_mistral: ai.embedding registry patches — "
            "shadowed methods=%s, patched dicts=%s",
            patched_methods, patched_dicts,
        )

    # ------------------------------------------------------------------ #
    # Internal pipeline                                                  #
    # ------------------------------------------------------------------ #

    def _daadit_run_embedding_pipeline(self):
        """Generate embeddings for the selected ai.embedding chunks.

        Reads ``content`` from each record, batches the API call, writes
        ``embedding_vector`` back. Marks ``has_embedding_generation_failed``
        if anything goes wrong.

        Mistral's embedding endpoint accepts a list ``input`` so we batch
        in groups of 96 (well under the 128-input limit) to keep payload
        sizes reasonable.
        """
        BATCH = 96
        records = self.filtered(lambda r: r.content)
        if not records:
            return True
        try:
            for offset in range(0, len(records), BATCH):
                chunk = records[offset:offset + BATCH]
                vectors = self._daadit_call_mistral_embeddings(
                    [r.content for r in chunk]
                )
                for record, vector in zip(chunk, vectors):
                    record.embedding_vector = vector
                    record.has_embedding_generation_failed = False
        except Exception as exc:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral: embedding generation failed: %s", exc
            )
            for record in records:
                record.has_embedding_generation_failed = True
            raise
        return True
