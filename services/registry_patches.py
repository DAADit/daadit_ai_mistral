# -*- coding: utf-8 -*-
"""Defensive registry patching for closed-source Enterprise ``ai`` lookups.

We don't know the exact method name on stock ``ai.agent`` / ``ai.embedding``
that raises::

    UserError("No embedding model found for the selected provider")

…or the equivalent for the forward provider lookup. So at registry hook time
we scan the merged class's MRO for two patterns and patch both:

1. **Methods whose bytecode references the error string.**
   We can't know the name in advance, but Python keeps every literal string
   from a function as a constant in ``__code__.co_consts`` (recursively for
   nested closures). If we find ``"No embedding model found"`` in a method's
   constants, that method is the one that raises — we shadow it on the merged
   class with a wrapper that returns ``'mistral-embed'`` when the agent is on
   a Mistral model and otherwise calls the original through.

2. **Class-level dict mappings that look like provider→model lookups.**
   Stock probably has something like
   ``{'openai': 'text-embedding-3-small', 'google': 'gemini-embedding-001'}``
   somewhere on the class body. We add ``'mistral': 'mistral-embed'`` in
   place, so any caller doing ``MAP[provider]`` or ``MAP.get(provider)``
   now succeeds.

Both are best-effort; if neither finds a target the install still works,
the original UserError will continue firing, and the fallback
``_get_embedding_model_for_provider`` candidate overrides remain in
``ai_agent.py`` for the case where the method name turns out to be one
of those.

This module has no Odoo imports — it's pure Python, called from
``_register_hook`` on the model classes.
"""
import logging
from typing import Callable, Iterable, List, Optional, Tuple

_logger = logging.getLogger(__name__)


ERROR_MARKERS: Tuple[str, ...] = (
    "No embedding model found",
    "No provider found for the selected",
)

# Provider-name keys we expect to see in a stock provider→model dict.
_PROVIDER_KEYS = {"openai", "google", "openai_provider", "google_provider"}


def _scan_code_for_markers(code, markers: Iterable[str]) -> bool:
    """Recursively scan a code object's constants for any of ``markers``.

    Walks nested ``code`` objects so closures and inner lambdas are covered.
    """
    try:
        consts = code.co_consts
    except AttributeError:
        return False
    for const in consts:
        if isinstance(const, str):
            for m in markers:
                if m in const:
                    return True
        elif hasattr(const, "co_consts"):  # nested code (closures, comprehensions)
            if _scan_code_for_markers(const, markers):
                return True
    return False


def discover_lookup_methods(cls, *, our_module_marker: str = "daadit_ai_mistral") -> List[Tuple[str, type]]:
    """Return ``[(method_name, defining_base), ...]`` for every method in
    ``cls.__mro__`` whose bytecode contains the error markers.

    Skips classes whose ``__module__`` contains ``our_module_marker`` so
    we don't recurse into our own overrides.

    Robust against ``werkzeug.local.LocalProxy`` attributes (and other
    descriptor-protocol surprises): each per-attribute step is wrapped
    so a single misbehaving attr never aborts the whole scan.
    """
    found: List[Tuple[str, type]] = []
    seen_names = set()
    for base in cls.__mro__:
        try:
            base_module = getattr(base, "__module__", "") or ""
        except Exception:  # noqa: BLE001
            continue
        if our_module_marker in base_module:
            continue
        try:
            attrs_iter = list(vars(base).items())
        except Exception:  # noqa: BLE001
            continue
        for attr_name, attr_value in attrs_iter:
            if attr_name.startswith("__") or attr_name in seen_names:
                continue
            try:
                if not callable(attr_value):
                    continue
                code = getattr(attr_value, "__code__", None)
            except Exception:  # noqa: BLE001
                # werkzeug LocalProxy and similar lazy objects can raise
                # RuntimeError on attribute access when the bound object
                # isn't available (e.g. ``request`` outside a request).
                continue
            if code is None:
                continue
            if _scan_code_for_markers(code, ERROR_MARKERS):
                seen_names.add(attr_name)
                found.append((attr_name, base))
    return found


def discover_provider_dicts(cls, *, our_module_marker: str = "daadit_ai_mistral") -> List[Tuple[type, str, dict]]:
    """Return ``[(base_cls, attr_name, dict_obj), ...]`` for every class-level
    dict on ``cls.__mro__`` that looks like a provider→embedding-model map.

    Heuristic: at least one key in ``_PROVIDER_KEYS`` AND at least one value
    looks like an embedding model id (contains ``embedding`` or starts with
    ``text-`` / ``gemini-``).
    """
    found: List[Tuple[type, str, dict]] = []
    for base in cls.__mro__:
        base_module = getattr(base, "__module__", "") or ""
        if our_module_marker in base_module:
            continue
        for attr_name, attr_value in vars(base).items():
            if attr_name.startswith("__"):
                continue
            if not isinstance(attr_value, dict) or not attr_value:
                continue
            keys = {k for k in attr_value.keys() if isinstance(k, str)}
            if not (keys & _PROVIDER_KEYS):
                continue
            string_values = [v for v in attr_value.values() if isinstance(v, str)]
            if not string_values:
                continue
            looks_like_embedding = any(
                "embedding" in v.lower()
                or v.startswith(("text-", "gemini-"))
                for v in string_values
            )
            if looks_like_embedding:
                found.append((base, attr_name, attr_value))
    return found


def install_method_overrides(
    cls,
    method_names: Iterable[str],
    *,
    is_target_record: Callable,
    target_return_value: object,
    log_label: str = "daadit_ai_mistral",
) -> List[str]:
    """Shadow the named methods on ``cls`` with a wrapper that returns
    ``target_return_value`` when ``is_target_record(record)`` is truthy and
    otherwise delegates through the next class in MRO that defines the same
    name.

    ``is_target_record`` is called with ``self`` (the model record) and
    decides whether this call should be intercepted.

    Returns the list of method names actually patched (skipping ones that
    were already patched on a prior hook run).
    """
    patched: List[str] = []
    for name in method_names:
        sentinel_attr = "_daadit_patched_" + name
        if getattr(cls, sentinel_attr, False):
            continue

        def _make_override(method_name: str):
            def _override(self, *args, **kwargs):
                # Find the next non-daadit class in MRO that defines this
                # method, so we can call the original implementation.
                original: Optional[Callable] = None
                for b in type(self).__mro__:
                    b_mod = getattr(b, "__module__", "") or ""
                    if "daadit_ai_mistral" in b_mod:
                        continue
                    fn = b.__dict__.get(method_name)
                    if fn is not None and not getattr(fn, "_daadit_patch", False):
                        original = fn
                        break

                if original is None:
                    # Nothing to delegate to — must intercept ourselves.
                    if is_target_record(self):
                        return target_return_value
                    raise AttributeError(
                        "%s: no original implementation of %s in MRO"
                        % (log_label, method_name)
                    )

                # Try the original first so non-Mistral callers see normal
                # behavior. If it raises the targeted UserError or KeyError,
                # and this record is one we're meant to handle, swap in our
                # value.
                try:
                    return original(self, *args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    is_target_err = any(m in msg for m in ERROR_MARKERS)
                    if not is_target_err:
                        # KeyError on a provider dict surfaces as the bare
                        # provider name — treat that as in-scope too.
                        if isinstance(exc, KeyError) and "mistral" in msg.lower():
                            is_target_err = True
                    if is_target_err and is_target_record(self):
                        _logger.info(
                            "%s: intercepted %s (%s); returning %r",
                            log_label, method_name, type(exc).__name__,
                            target_return_value,
                        )
                        return target_return_value
                    raise

            _override.__name__ = method_name
            _override.__qualname__ = "%s.%s" % (cls.__name__, method_name)
            _override._daadit_patch = True
            return _override

        setattr(cls, name, _make_override(name))
        setattr(cls, sentinel_attr, True)
        patched.append(name)
    return patched


def patch_provider_dicts(
    dict_specs: Iterable[Tuple[type, str, dict]],
    *,
    new_key: str = "mistral",
    new_value: str = "mistral-embed",
    log_label: str = "daadit_ai_mistral",
) -> List[str]:
    """Add ``{new_key: new_value}`` to every dict in ``dict_specs`` if absent.

    Returns the human-readable list of paths that were modified, for logging.
    """
    modified: List[str] = []
    for base, attr_name, dict_obj in dict_specs:
        if dict_obj.get(new_key) == new_value:
            continue
        dict_obj[new_key] = new_value
        path = "%s.%s.%s" % (base.__module__, base.__name__, attr_name)
        modified.append(path)
        _logger.info(
            "%s: patched provider dict %s → added %r=%r",
            log_label, path, new_key, new_value,
        )
    return modified


# ============================================================================
# GLOBAL SCAN — for callables that live outside any Odoo-registered model
# ============================================================================
#
# Stock Enterprise sometimes implements provider→embedding lookup as a free
# function in a service/helper module (e.g. ``odoo.addons.ai.services.*``)
# rather than as a method on ``ai.agent`` or ``ai.embedding``. Those won't
# show up in ``type(record).__mro__`` because they're not part of the
# registered model class hierarchy.
#
# This pair walks ``sys.modules`` for any module whose name contains the
# given prefix (default: ``odoo.addons.ai``), inspects every class method
# and module-level function for the error markers in its bytecode, and
# wraps each match. The wrapper:
#
#   - Inspects args/kwargs for any object that looks "Mistral-flavored"
#     (a record with ``llm_model``/``embedding_model`` set to a Mistral
#     value, or a kwarg ``provider="mistral"`` or ``model="mistral-..."``).
#   - If Mistral context: short-circuits and returns the target value
#     (``"mistral-embed"``) BEFORE the original runs, so the original
#     never raises.
#   - Otherwise calls the original and, if it raises a target-marker
#     exception while args looked Mistral-flavored, returns the target
#     value as a fallback.
#
# This is broader and more invasive than the class-MRO scan. It's gated
# behind the prefix filter to keep startup cost bounded.


def _looks_mistral(args, kwargs, mistral_predicate, mistral_string_predicate) -> bool:
    """Return True if any positional or keyword arg suggests a Mistral
    record/provider/model is in scope.

    ``mistral_predicate`` — callable taking the record-like object,
    returns True if it's a Mistral record (uses caller's domain logic).

    ``mistral_string_predicate`` — callable taking a model-id string,
    returns True if it's a Mistral model id.
    """
    candidates = list(args) + list(kwargs.values())
    for c in candidates:
        if c is None:
            continue
        # Direct string check (provider name or model id)
        if isinstance(c, str):
            if c == "mistral" or mistral_string_predicate(c):
                return True
            continue
        # Record-like — has llm_model or embedding_model attribute
        try:
            llm = getattr(c, "llm_model", None)
            if llm and mistral_string_predicate(llm):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            em = getattr(c, "embedding_model", None)
            if em and mistral_predicate(em):
                return True
        except Exception:  # noqa: BLE001
            pass
    # Provider/model kwargs
    if kwargs.get("provider") == "mistral":
        return True
    model_kw = kwargs.get("model")
    if isinstance(model_kw, str) and mistral_string_predicate(model_kw):
        return True
    return False


def discover_in_loaded_modules(
    *,
    module_prefix: str = "odoo.addons.ai",
    our_module_marker: str = "daadit_ai_mistral",
):
    """Walk ``sys.modules`` and yield ``(parent, attr_name, callable)``
    tuples for every method/function whose bytecode references one of
    the error markers.

    ``parent`` is either the class (for methods) or the module (for
    module-level functions). ``setattr(parent, attr_name, wrapper)`` will
    install a replacement.
    """
    import sys

    found = []
    seen_codes = set()

    for module_name, module in list(sys.modules.items()):
        if module is None:
            continue
        if not module_name or module_prefix not in module_name:
            continue
        if our_module_marker in module_name:
            continue
        try:
            module_vars = list(vars(module).items())
        except Exception:  # noqa: BLE001
            continue

        for attr_name, attr in module_vars:
            # Wrap each attribute inspection — werkzeug LocalProxy
            # attributes (e.g. ``odoo.http.request``) raise RuntimeError
            # on attribute access outside a request context. ``getattr``
            # with a default doesn't catch RuntimeError. A single rogue
            # attribute must not abort the whole scan.
            try:
                # Module-level free function
                if callable(attr) and not isinstance(attr, type):
                    code = getattr(attr, "__code__", None)
                    if code is None or id(code) in seen_codes:
                        continue
                    if _scan_code_for_markers(code, ERROR_MARKERS):
                        seen_codes.add(id(code))
                        found.append((module, attr_name, attr))
                # Class — walk its methods
                elif isinstance(attr, type):
                    # Only consider classes actually defined in this module
                    if attr.__module__ != module_name:
                        continue
                    try:
                        cls_vars = list(vars(attr).items())
                    except Exception:  # noqa: BLE001
                        continue
                    for method_name, method in cls_vars:
                        try:
                            if not callable(method):
                                continue
                            code = getattr(method, "__code__", None)
                        except Exception:  # noqa: BLE001
                            continue
                        if code is None or id(code) in seen_codes:
                            continue
                        if _scan_code_for_markers(code, ERROR_MARKERS):
                            seen_codes.add(id(code))
                            found.append((attr, method_name, method))
            except Exception:  # noqa: BLE001
                # Truly unexpected error during a single attr — log and
                # keep going.
                _logger.debug(
                    "discover_in_loaded_modules: skipped %s.%s due to "
                    "unexpected exception", module_name, attr_name,
                    exc_info=True,
                )
                continue
    return found


def install_module_overrides(
    found,
    *,
    target_return_value,
    is_mistral_string_predicate,
    is_mistral_record_predicate,
    log_label: str = "daadit_ai_mistral",
) -> List[str]:
    """Wrap each callable in ``found`` with a Mistral-aware short-circuit.

    The wrapper logic:
      * If args/kwargs look Mistral-flavored, return ``target_return_value``
        immediately (don't call the original).
      * Otherwise run the original. If it raises one of the target errors
        AND args looked Mistral-flavored, return the target value as
        fallback. Otherwise re-raise.

    Returns the list of patched paths for logging.
    """
    patched: List[str] = []
    for parent, attr_name, original in found:
        sentinel = "_daadit_patched_" + attr_name
        if getattr(parent, sentinel, False):
            continue

        # Skip already-wrapped functions (shouldn't happen due to sentinel,
        # but defense in depth).
        if getattr(original, "_daadit_patch", False):
            continue

        def _make_wrapper(orig, name, parent_obj):
            def _wrapper(*args, **kwargs):
                ctx_is_mistral = _looks_mistral(
                    args, kwargs,
                    is_mistral_record_predicate,
                    is_mistral_string_predicate,
                )
                if ctx_is_mistral:
                    _logger.debug(
                        "%s: short-circuited %s.%s → %r",
                        log_label,
                        getattr(parent_obj, "__name__", repr(parent_obj)),
                        name, target_return_value,
                    )
                    return target_return_value
                try:
                    return orig(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    if any(m in str(exc) for m in ERROR_MARKERS):
                        # Caller didn't hand us obvious Mistral args, but
                        # the failure is the one we're guarding against.
                        # Re-check looser conditions (single-arg providers,
                        # etc.) and fall through to value if Mistral-shaped.
                        if ctx_is_mistral:
                            _logger.info(
                                "%s: caught %s in %s.%s → returning %r",
                                log_label, type(exc).__name__,
                                getattr(parent_obj, "__name__", repr(parent_obj)),
                                name, target_return_value,
                            )
                            return target_return_value
                    raise
            _wrapper.__name__ = name
            _wrapper.__qualname__ = "%s.%s" % (
                getattr(parent_obj, "__name__", "?"), name,
            )
            _wrapper._daadit_patch = True
            _wrapper._daadit_original = orig
            return _wrapper

        try:
            wrapped = _make_wrapper(original, attr_name, parent)
            setattr(parent, attr_name, wrapped)
            setattr(parent, sentinel, True)
            path = "%s.%s.%s" % (
                getattr(parent, "__module__", "?"),
                getattr(parent, "__name__", "?"),
                attr_name,
            )
            patched.append(path)
            _logger.info("%s: wrapped %s", log_label, path)
        except (TypeError, AttributeError) as exc:
            _logger.warning(
                "%s: could not patch %s.%s: %s",
                log_label, parent, attr_name, exc,
            )
    return patched
