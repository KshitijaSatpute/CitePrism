"""Plain-language audit assistant.

Answers questions about one completed audit for readers who are not citation
specialists. Every answer is built from the audit's review projection — the
same data the review workspace shows — so it cannot report a problem the
audit did not find. A language model is optional and only rephrases those
facts for questions the built-in topics do not cover; references it mentions
are checked against the audit before they are shown.
"""

from src.assistant.engine import AuditAssistant, classify_question

__all__ = ["AuditAssistant", "classify_question"]
