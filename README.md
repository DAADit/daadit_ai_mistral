# DAADit AI — Mistral Provider (product repo)

Product repository for the `daadit_ai_mistral` Odoo module: Mistral AI as an
LLM provider for Odoo 19 Enterprise's built-in AI features (chat, embeddings,
tool calling), with the DAADit governance guardrails on top.

## Layout

```
daadit_ai_mistral/    ← the Odoo module (this is what gets deployed)
```

The module sits in a subdirectory (not at the repo root) so this repo can be
consumed directly by Odoo.sh — as a git submodule of a deployment repo, or by
copying the folder into a deployment branch. Odoo.sh scans for directories
containing a `__manifest__.py`.

## Versioning

- The source of truth for the version is `daadit_ai_mistral/__manifest__.py`
  (`19.0.<feature>.<minor>.<patch>` — see `CHANGELOG.md` in the module for
  the scheme and history).
- Every release is tagged `v<version>` on `main`.
- `main` is always deployable; feature work goes through branches + PRs.

## Deployment targets

| Target | How |
|---|---|
| DAADit production (`daadit.group`) | via `adriedaadit/daadit` monorepo — sync the module folder from a tagged release |
| Customer environments (e.g. BroStar) | via the customer's Odoo.sh deploy repo — sync the module folder from a tagged release |

When syncing to a deployment repo, take a **tagged release**, never an
untagged `main` snapshot, so the version deployed is always traceable back to
a release here.

### Prefer a submodule over a copied folder

Copying the folder is what created the 2026-08-01 split: the deploy repo grew
its own 19.0.6.x line while this repo was at 19.0.7.x, so product work never
reached production and a deploy-side fix nearly got wiped by the next sync.
A submodule makes that second line impossible — upgrading becomes moving the
pin, and the deployed revision is always a release here.

`tools/adopt_as_submodule.sh` performs the one-off conversion inside a
deployment repo:

```bash
# in the deployment repo, on a fresh branch
./adopt_as_submodule.sh --release v19.0.8.0.0
```

It refuses to run while the copied folder differs from the release you pin to
and writes the difference to `deploy-only.diff` — that difference is
deploy-only work which must be PR'd here and re-tagged first, otherwise the
conversion deletes it. The submodule is mounted next to the old folder
(default `submodules/daadit_ai_mistral`) because this repo's root already
holds `daadit_ai_mistral/`; Odoo.sh scans recursively for manifests, so no
addons-path change is needed.

## Relation to sibling modules

Per DAADit policy each LLM provider lives in its own module/repo — never mix
providers. Siblings: `daadit_ai_claude` (Anthropic), `daadit_ai_copilot`
(Azure OpenAI / M365), `daadit_ai_m365_grounding` (Graph grounding, not a
provider). The scheduling framework (`daadit_ai_agent_schedule`) consumes
this module's threadlocal exhaustion signals but is a separate product.

## Governance

The module implements the Fase 0 guardrails from the internal
"Governance & guardrails" knowledge article (id 173): hard cap of 10
LLM-calls per run, hallucinated-toolname bail-out, token logging per call
(`daadit_ai_mistral.usage`), PII field-blocklist, per-agent model
allow/block lists, and a base-URL allowlist.
