"""Phase 2 evidence contracts and deterministic review policy."""

SCHEMA_VERSION = "evidence-v3"
EXTRACTION_VERSION = "page-anchors-v4.2"  # 4.0: author–year, footnotes, chapters; 4.2: hardening, nearest-year conflicts
RESOLVER_VERSION = "identity-v6"  # v4: subtitles, chapter DOIs; v5: multi-word surnames, one subtitle only; v6: specialist-index outages, rejoined hyphens in queries
SCORING_VERSION = "grounded-assessment-v4"
POLICY_VERSION = "evidence-review-v6"  # v5: split reference entries; v6: incomplete searches, incomplete author lists
