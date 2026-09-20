"""boundary.redact: detection and the personal-class policy, as library objects.

Two halves, usable independently and with no gateway, configuration or network:

    from boundary.redact import Analyzer, Policy

    spans = Analyzer().analyze(pages)            # every entity, per page, half-open offsets
    policy = Policy(spans, allow={"Corner Brook"})
    body = policy.outbound(prompt)               # placeholders, or RedactionRefused
    answer = policy.rehydrate(model_output)      # values back

Built to project 07's specification: PLAN.md section B2.3 and docs/redact.md.
"""

from boundary.redact.analyzer import Analyzer, cut_at_line_break, resolve_overlaps
from boundary.redact.policy import PLACEHOLDER, Leak, Policy, RedactionRefused
from boundary.redact.recognisers import DEFAULT_RECOGNISERS, RegexRecogniser
from boundary.redact.types import EntityType, Recogniser, Span
from boundary.redact.vocabulary import DECISION_VOCABULARY

__all__ = [
    "DECISION_VOCABULARY",
    "DEFAULT_RECOGNISERS",
    "PLACEHOLDER",
    "Analyzer",
    "EntityType",
    "Leak",
    "Policy",
    "Recogniser",
    "RedactionRefused",
    "RegexRecogniser",
    "Span",
    "cut_at_line_break",
    "resolve_overlaps",
]
