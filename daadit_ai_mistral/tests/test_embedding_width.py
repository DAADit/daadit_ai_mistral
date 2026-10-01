# -*- coding: utf-8 -*-
"""mistral-embed (1024) past in een vaste vectorkolom (1536) — taak 1483.

Nullen aanvullen laat inproduct, norm en cosinusafstand gelijk, zolang
opgeslagen stukken en de zoekvraag op dezelfde manier worden aangevuld.
"""
import ast
import math
from unittest.mock import patch

from odoo.tests import common, tagged

from ..services.llm_api_patch import pad_embedding_response
from ..services.mistral_client import pad_vector


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a))
                  * math.sqrt(sum(y * y for y in b)))


@tagged("post_install", "-at_install")
class TestEmbeddingWidth(common.TransactionCase):

    def test_aanvullen_laat_cosinus_gelijk(self):
        a, b = [0.1, 0.5, -0.3], [0.4, -0.2, 0.9]
        pa, pb = pad_vector(a, 8), pad_vector(b, 8)
        self.assertEqual(len(pa), 8)
        self.assertEqual(pa[3:], [0.0] * 5)
        self.assertAlmostEqual(_cosine(a, b), _cosine(pa, pb))

    def test_te_breed_wordt_geweigerd_en_onbekend_blijft_gelijk(self):
        with self.assertRaises(ValueError):
            pad_vector([1.0] * 4, 3)
        self.assertEqual(pad_vector([1.0, 2.0], 0), [1.0, 2.0])

    def test_zoekvraag_krijgt_dezelfde_breedte(self):
        resp = {"data": [{"embedding": [0.2] * 1024}]}
        out = pad_embedding_response(resp, 1536)
        self.assertEqual(len(out["data"][0]["embedding"]), 1536)
        same = pad_embedding_response({"data": [{"embedding": [1.0]}]}, None)
        self.assertEqual(same["data"][0]["embedding"], [1.0])

    def test_pipeline_schrijft_kolombreedte(self):
        Embedding = self.env["ai.embedding"]
        rec = Embedding.create({
            "name": "stuk",
            "embedding_model": Embedding._fields[
                "embedding_model"].selection[0][0],
            "content": "Hoe boek ik een creditnota?",
        })
        cls = type(Embedding)
        with patch.object(cls, "_daadit_vector_width",
                          return_value=1536), \
                patch.object(cls, "_daadit_call_mistral_embeddings",
                             return_value=[[0.3] * 1024]):
            rec._daadit_run_embedding_pipeline()
        stored = rec.embedding_vector
        if isinstance(stored, str):
            stored = ast.literal_eval(stored)
        self.assertEqual(len(stored), 1536)
        self.assertFalse(rec.has_embedding_generation_failed)
