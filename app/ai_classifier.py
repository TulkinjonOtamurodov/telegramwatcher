"""Optional AI classification layer.

The watcher runs perfectly without this module doing anything: when
``ai.enabled`` is ``false`` in ``watcher_settings.json`` (the default),
:func:`classify_message` returns a cheap "allow everything" result and no
network call is made.

Turning ``ai.enabled`` on without registering a backend falls back to a free,
rule-based categoriser, so the ``{{category}}`` and ``{{severity}}`` template
placeholders become useful immediately. To plug in a real model later, call
:func:`register_backend` from your own module with a coroutine matching
:data:`Backend`:

    from app.ai_classifier import register_backend

    async def my_backend(text, context):
        ...  # call your provider here
        return {"important": True, "category": "FUEL", "severity": "HIGH"}

    register_backend(my_backend)

The pipeline is: rules/keywords -> AI analysis -> category -> severity ->
action required -> send alert. Only the middle step is optional.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Mapping

from app.logging_config import get_logger

logger = get_logger("ai")

CATEGORIES: tuple[str, ...] = (
    "CLAIMS",
    "SAFETY",
    "FUEL",
    "INSURANCE",
    "MAINTENANCE",
    "DRIVER",
    "LEGAL",
    "PERMITS",
    "GENERAL",
)

SEVERITIES: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

#: Signature every backend must satisfy.
Backend = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]

#: Timeout applied to any registered backend, so a slow model cannot stall the
#: watcher's event loop behind a single message.
BACKEND_TIMEOUT_SECONDS = 20.0

_backend: Backend | None = None
_warned_missing_backend = False

# Cheap keyword -> category map used by the built-in fallback categoriser.
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "CLAIMS": ("claim", "claims", "damage", "cargo claim", "freight claim"),
    "SAFETY": ("safety", "accident", "incident", "injury", "camera", "police", "crash"),
    "FUEL": ("fuel", "fuel card", "card", "diesel", "def", "pump"),
    "INSURANCE": ("insurance", "coverage", "policy", "adjuster", "deductible"),
    "MAINTENANCE": ("breakdown", "repair", "shop", "tire", "trailer", "engine", "tow"),
    "DRIVER": ("driver", "mvr", "psp", "termination", "terminate", "lease", "hire"),
    "LEGAL": ("attorney", "lawsuit", "citation", "subpoena", "court", "legal"),
    "PERMITS": ("permit", "dot", "inspection", "violation", "oos", "ifta", "scale"),
}

# Terms that push a message up the severity scale.
_HIGH_SIGNALS = ("accident", "injury", "lawsuit", "attorney", "oos", "police", "crash")
_MEDIUM_SIGNALS = ("claim", "violation", "citation", "inspection", "breakdown", "damage")

#: Returned whenever classification is switched off. ``important`` stays True so
#: that a disabled AI layer never suppresses an alert the rules already matched.
DISABLED_RESULT: dict[str, Any] = {
    "enabled": False,
    "important": True,
    "category": "GENERAL",
    "severity": "LOW",
    "summary": "",
    "requires_action": False,
    "unit": None,
}


def register_backend(backend: Backend | None) -> None:
    """Install (or clear, with ``None``) the coroutine that performs analysis."""
    global _backend, _warned_missing_backend
    _backend = backend
    _warned_missing_backend = False
    logger.info(
        "AI backend %s", "registered" if backend else "cleared (rule-based fallback)"
    )


def has_backend() -> bool:
    return _backend is not None


def _normalize(result: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce a backend's output into the documented shape."""
    category = str(result.get("category") or "GENERAL").upper()
    if category not in CATEGORIES:
        category = "GENERAL"

    severity = str(result.get("severity") or "LOW").upper()
    if severity not in SEVERITIES:
        severity = "LOW"

    unit = result.get("unit")
    return {
        "enabled": True,
        "important": bool(result.get("important", True)),
        "category": category,
        "severity": severity,
        "summary": str(result.get("summary") or "").strip(),
        "requires_action": bool(result.get("requires_action", False)),
        "unit": str(unit) if unit not in (None, "") else None,
    }


def _rule_based(text: str, context: Mapping[str, Any]) -> dict[str, Any]:
    """Free fallback used when AI is enabled but no backend is registered."""
    lowered = (text or "").lower()
    hits = [hit.lower() for hit in context.get("keyword_hits", []) or []]
    haystack = f"{lowered} {' '.join(hits)}"

    category = "GENERAL"
    best_score = 0
    for name, terms in _CATEGORY_KEYWORDS.items():
        score = sum(1 for term in terms if term in haystack)
        if score > best_score:
            best_score, category = score, name

    if any(term in haystack for term in _HIGH_SIGNALS):
        severity = "HIGH"
    elif any(term in haystack for term in _MEDIUM_SIGNALS):
        severity = "MEDIUM"
    else:
        severity = "LOW"

    reasons = context.get("reasons", []) or []
    requires_action = severity in ("HIGH", "CRITICAL") or "mention" in reasons

    return _normalize(
        {
            "important": True,
            "category": category,
            "severity": severity,
            "summary": "",
            "requires_action": requires_action,
        }
    )


async def classify_message(text: str, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Classify one message.

    ``context`` carries whatever the watcher already knows -- ``enabled``,
    ``reasons``, ``keyword_hits``, ``group``, ``sender`` -- so a backend does not
    have to re-derive it.

    Never raises: any backend failure degrades to the rule-based result so a
    broken model can never stop an alert from being delivered.
    """
    global _warned_missing_backend

    ctx: Mapping[str, Any] = context or {}

    if not ctx.get("enabled", False):
        return dict(DISABLED_RESULT)

    if _backend is None:
        if not _warned_missing_backend:
            logger.warning(
                "ai.enabled is true but no backend is registered; "
                "using the built-in rule-based classifier"
            )
            _warned_missing_backend = True
        return _rule_based(text, ctx)

    try:
        raw = await asyncio.wait_for(_backend(text, ctx), timeout=BACKEND_TIMEOUT_SECONDS)
        return _normalize(raw or {})
    except asyncio.TimeoutError:
        logger.warning("AI backend timed out after %.0fs; falling back to rules", BACKEND_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("AI backend raised; falling back to rules")
    return _rule_based(text, ctx)
