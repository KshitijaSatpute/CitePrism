"""Decode complete assessment responses without changing their task identities."""

from src.evidence.providers import strict_json


class AssessmentResponseError(ValueError):
    code = 'invalid_response_schema'


def decode_assessments(raw):
    """Accept the named envelope or a bare list; never repair broken JSON.

    Row schemas, exact task IDs and source/claim quotations are checked by the
    scorer. Unwrapping a list is not evidence acceptance.
    """
    try:
        value = strict_json(raw)
    except (ValueError, TypeError, AttributeError) as error:
        raise AssessmentResponseError('Assessment response is not complete JSON') from error
    rows = value.get('assessments') if isinstance(value, dict) else value
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise AssessmentResponseError('Expected an assessment list or an assessments envelope')
    return rows
