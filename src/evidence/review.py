"""One versioned review projection for the UI, exports, and audit capture."""

import html
import json
from collections import Counter

from src.evidence import SCHEMA_VERSION
from src.evidence.contracts import ReviewProjection, ReviewReference, ReviewFinding
from src.evidence.text import digest_text, repaired_title
from src.evidence.relevance import needs_relevance_review

EXTRACTION_MESSAGES = {
    'unassigned_model_reference_observations': 'Some parsed references could not be linked to their printed bibliography labels. Reprocess the manuscript with the updated reference matcher.',
    'missing_or_duplicate_model_entry': 'A single parsed reference could not be linked to this printed entry.',
    'duplicate_printed_reference_label': 'The same reference number is printed more than once.',
    'bibliography_fragment': 'The extracted entry has no usable title; inspect its printed text.',
    'bibliography_boundary_ambiguous': 'This entry may continue into an adjacent numbered entry.',
    'title_not_grounded_in_entry': 'The model-generated title could not be located in this printed entry.',
    'authors_not_grounded_in_entry': 'Some model-generated authors could not be located in this printed entry.',
    'year_not_grounded_in_entry': 'The model-generated year could not be located in this printed entry.',
    'references_boundary_unresolved': 'The start of the bibliography could not be located.',
    'reference_entries_need_location_review': 'Reference locations need checking against the manuscript.',
    'multiple_bibliographies': 'The PDF contains several reference lists; each list was matched only with citations in its own part.',
    'citation_year_mismatches': 'Some citations give a different year than the reference list.',
    'model_unavailable_rule_based_parse': 'No AI model was available, so the reference list was read by rule: titles may be shortened and the paper\'s own title and authors were taken from the PDF.',
}


# A work cited by at least this many of the manuscript's own references is raised as
# possibly missing; fewer is a lead shown in the insights view only.
STRONG_SUPPORT = 3


def extraction_message(code):
    return EXTRACTION_MESSAGES.get(code, str(code).replace('_', ' ').capitalize())


def status_label(code):
    return {'extraction_needs_review': 'Bibliography needs review',
            'identity_unresolved': 'Source identity unresolved',
            'grounded_proposal': 'Quoted model proposal; awaiting review',
            'grounding_failed': 'Quotation check failed',
            'assessment_unavailable': 'Assessment unavailable',
            'provider_unavailable': 'Source provider unavailable',
            'not_assessed': 'Not assessed', 'not_found': 'No matching source found',
            'source_unavailable': 'Source text unavailable',
            'offline_mode': 'Not resolved (offline mode)',
            'unrelated': 'Source does not address the claim',
            'relevant': 'Relevant', 'borderline': 'Borderline relevance',
            'potentially_irrelevant': 'Potentially irrelevant',
            'low_topical_similarity': 'Low topical similarity', 'not_scored': 'Not scored',
            'topic_only': 'Topic matches; claims not checked',
            'legacy_unverified': 'Earlier result; reprocessing needed'}.get(
                code, str(code).replace('_', ' ').capitalize())


PROVIDER_NAMES = {'openalex': 'OpenAlex', 'crossref': 'Crossref', 'semantic_scholar': 'Semantic Scholar',
                  'dblp': 'dblp', 'europe_pmc': 'Europe PMC', 'datacite': 'DataCite'}
OUTAGE_REASONS = {'rate_limit': 'was limiting requests (too many in a short time)',
                  'service_unavailable': 'was temporarily unavailable', 'timeout': 'did not answer in time',
                  'authentication': 'rejected the configured API key',
                  'provider_error': 'returned an error'}
FIELD_NAMES = {'doi': 'DOI', 'title': 'Title', 'year': 'Year', 'first_author': 'First author',
               'authors': 'Authors', 'venue': 'Venue'}


# Databases CitePrism calls without an API key: for them a refusal (401/403) is a firewall,
# security software or proxy in between, not a key problem.
KEYLESS = {'dblp', 'europe_pmc', 'datacite', 'crossref'}
# Failures that asking again usually cures.
TRANSIENT = {'rate_limit', 'service_unavailable', 'timeout', 'provider_error'}


def _outage_parts(errors):
    """“dblp returned an error (HTTP 500)”, once per database and reason."""
    parts = []
    for error in errors:
        reason = OUTAGE_REASONS.get(error.get('error_code'), 'returned an error')
        if error.get('error_code') == 'authentication' and error.get('provider') in KEYLESS:
            reason = 'refused the request (a firewall, security software or proxy may be blocking it)'
        if reason == 'returned an error' and error.get('http_status'):
            reason += f" (HTTP {error['http_status']})"
        parts.append(f"{PROVIDER_NAMES.get(error.get('provider'), str(error.get('provider')))} {reason}")
    return list(dict.fromkeys(parts))


def _names(providers):
    """General indexes first, in the order the report introduces them."""
    order = list(PROVIDER_NAMES)
    ranked = sorted(providers, key=lambda p: order.index(p) if p in order else len(order))
    names = [PROVIDER_NAMES.get(p, str(p)) for p in ranked]
    if len(names) < 2:
        return ''.join(names)
    return ', '.join(names[:-1]) + ' and ' + names[-1]


def provider_outage(resolution):
    """Which database failed and why, in plain words."""
    parts = _outage_parts(resolution.get('provider_errors') or [])
    if not parts:
        return 'A scholarly database did not respond, so this reference could not be looked up.'
    return ('; '.join(parts) + '. Without its answer CitePrism will not say the work is missing. '
            'This is a temporary technical problem, not a problem with the reference.')


def not_found_detail(resolution, status):
    """What was searched and what was not, for a reference no database could match."""
    failed = {e.get('provider') for e in resolution.get('provider_errors') or []}
    searched = [p for p in resolution.get('providers_queried') or [] if p not in failed]
    where = f' in {_names(searched)}' if searched else ' in the scholarly databases'
    if status == 'unresolved':
        text = (f'Possible matches were found{where}, but none agrees closely enough with the printed title, '
                'authors and year to be sure it is the cited work.')
    else:
        text = f'No matching work was found{where}.'
    if resolution.get('search_incomplete'):
        missing = [e for e in resolution.get('provider_errors') or []
                   if e.get('provider') in resolution['search_incomplete']]
        several = len({e.get('provider') for e in missing}) > 1
        passing = all(e.get('error_code') in TRANSIENT for e in missing)
        text += (f" {'; '.join(_outage_parts(missing))}, so {'those indexes were' if several else 'that index was'} "
                 'not searched; ' + ('running the check again completes the search.' if passing else
                                     'if this happens again, see “Database errors” in the README.'))
    if status == 'unresolved':
        return text
    return text + (' Many journals, and most books, reports and websites, are not in these databases, so this '
                   'does not show that the work is missing.')


def describe_difference(d):
    """One metadata difference as a reader-facing sentence."""
    field = FIELD_NAMES.get(d.get('field'), str(d.get('field')).replace('_', ' ').capitalize())
    detail = d.get('detail') or ''
    if len(detail) > 1 and detail[0].isupper() and detail[1].islower():
        detail = detail[0].lower() + detail[1:]     # mid-sentence: "— the years differ by 8"
    if d.get('field') == 'authors' or d.get('candidate') in (None, ''):
        return f"{field}: {detail or ('printed ' + str(d.get('as_cited')))}".strip()
    printed = d.get('as_cited')
    if d.get('field') == 'title':
        printed = repaired_title(printed, d.get('candidate'))  # as printed, not as the text layer spaced it
    text = f"{field}: printed “{printed}”, the database record has “{d.get('candidate')}”"
    return text + (f" — {detail}" if detail else '')


def describe_differences(differences):
    """Each difference as its own sentence: “… differ by 4. Venue: …”, never “4.; Venue”."""
    sentences = [describe_difference(d).rstrip() for d in differences]
    return ' '.join(text if text.endswith(('.', '?', '!')) else text + '.' for text in sentences if text)


def assessment_message(code):
    return {
        'invalid_response': 'The model replied, but its response format could not be read.',
        'invalid_response_schema': 'The model response did not match the required assessment format.',
        'missing_task_id': 'The model did not return an answer for this citation use.',
        'duplicate_task_id': 'The model returned multiple answers for the same citation use.',
        'claim_not_at_target_citation': 'The proposed claim could not be located at the assigned manuscript citation.',
        'quote_not_in_own_source': 'A proposed quotation could not be located in the assigned source abstract.',
        'authentication': 'The assessment provider credentials or access permissions need attention.',
        'dependency_unavailable': 'The assessment provider dependency is unavailable.',
        'rate_limit': 'The assessment provider reached its request or quota limit.',
        'service_unavailable': 'The assessment provider was temporarily unavailable.',
        'timeout': 'The assessment provider did not respond within the time limit.',
        'model_unavailable': 'The configured assessment model is not available (it may have been retired); set a current model.',
        'invalid_request': 'The assessment provider rejected the request; the model name or settings may be wrong.',
        'empty_response': 'The assessment model returned an empty answer.',
        'provider_error': 'The assessment provider returned an error.',
        'unknown_task_id': 'The model answered for a citation use that was not asked about.',
    }.get(code, 'Assessment could not be accepted; inspect its recorded response and source evidence.') + (
        f' (code: {code})' if code else '')


def assessment_diagnostics(data):
    """Interpretation checks are separate from a provider's successful API call."""
    return [{'reference': str(ref.get('ref_id', 'Unknown reference')),
             'task_id': item.get('task_id'), 'status': item.get('status', 'unavailable'),
             'decision': item.get('decision') if item.get('status') == 'grounded_proposal' else 'not_assessed',
             'provider_call_id': item.get('provider_call_id'), 'error_code': item.get('error_code'),
             'detail': ('Claim and source quotation checks passed; interpretation needs human review.'
                        if item.get('status') == 'grounded_proposal' else assessment_message(item.get('error_code')))}
            for ref in data.get('scored_references', []) for item in ref.get('claim_assessments', [])]


def _split_groups(data):
    groups = (data.get('extraction_metadata') or {}).get('bibliography_ambiguities') or []
    return [g for g in groups if g.get('kind') == 'possible_split_entry' and len(g.get('reference_ids') or []) == 2]


def split_renumbering(data):
    """{entry after a split pair: (first, second)} when the text cites the pair's second number
    but never the entry after it — the signature of a list renumbered by the split."""
    refs = data.get('scored_references', [])
    order = [str(r.get('ref_id')) for r in refs]
    shifted = {}
    for group in _split_groups(data):
        first, second = (str(item) for item in group['reference_ids'])
        if second in order and order.index(second) + 1 < len(order):
            following = refs[order.index(second) + 1]
            cited = refs[order.index(second)].get('citation_count', 0)
            if cited and not following.get('citation_count'):
                shifted[str(following.get('ref_id'))] = (first, second)
    return shifted


def split_entry_note(ref_id, data):
    """For an entry that may continue into its neighbour, the plain diagnosis — and, when the
    text cites the second part but never the entry after it, the likely renumbering."""
    group = next((g for g in _split_groups(data) if ref_id in [str(i) for i in g['reference_ids']]), None)
    if not group:
        return None
    first, second = (str(item) for item in group['reference_ids'])
    note = (f'Entries {first} and {second} look like one reference broken in two: {first} has authors but no title '
            f'or year, and {second} carries the year and title. A list numbered automatically (for example by Word) '
            'splits an entry this way when it contains a line break.')
    following = next((after for after, pair in split_renumbering(data).items() if pair == (first, second)), None)
    if following:
        note += (f' The text cites {second} but never {following}: if the list was renumbered by the '
                 f'split, the text\'s {second} probably means what is now {following}.')
        assessed = any(r.get('claim_assessments') for r in data.get('scored_references', [])
                       if str(r.get('ref_id')) == second)
        if assessed:
            note += (f' CitePrism checked the sentences citing {second} against the work printed as {second}; if '
                     f'they mean {following}, that check concerns the wrong work.')
    return note + ' Merge the two entries and check the numbers cited after them.'


def build_review(data):
    modern = data.get('schema_version') == SCHEMA_VERSION
    references, findings = [], []
    renumbered = split_renumbering(data)
    for ordinal, ref in enumerate(data.get('scored_references', [])):
        original = ref.get('original_data', {})
        parsed = original.get('parsed', {})
        uid = ref.get('reference_uid') or digest_text(f"legacy:{ordinal}:{ref.get('ref_id')}")
        identity = ref.get('resolution', {}).get('status', 'legacy_unverified')
        evidence = ref.get('evidence_status', 'legacy_unverified')
        ref_findings = []
        def add(category, priority, summary, explanation, action, system=False):
            identifier = digest_text(uid + ':' + category)
            ref_findings.append(identifier)
            findings.append(ReviewFinding(id=identifier, reference_id=uid, category=category,
                priority=priority, summary=summary, explanation=explanation, action=action,
                source_locations=original.get('source_locations', []), system_issue=system))
        if not modern:
            add('legacy_unverified', 'informational', 'Earlier result needs reprocessing',
                'This result predates source anchoring and quote validation.', 'Run a new audit to obtain the current evidence checks.')
        else:
            split = None
            if original.get('extraction_status') != 'located':
                issues = original.get('extraction_issues', ['Entry location unavailable'])
                linking_issue = 'missing_or_duplicate_model_entry' in issues
                split = split_entry_note(str(ref.get('ref_id')), data) if 'bibliography_boundary_ambiguous' in issues \
                    else None
                add('split_entry' if split and not linking_issue else 'bibliography_review', 'moderate',
                    'Parsed reference could not be linked' if linking_issue else
                    'One reference split into two entries' if split else 'Check the printed reference entry',
                    split or ' '.join(extraction_message(code) for code in issues),
                    'Reprocess with the updated reference matcher, then inspect any remaining ambiguous labels.'
                    if linking_issue else 'Compare the entry with the manuscript page; preserve the printed numbering.',
                    linking_issue)
            if identity == 'offline_mode':
                add('identity_unresolved', 'informational', 'Not resolved in offline mode',
                    'This deployment runs without network access, so no scholarly index was queried.',
                    'Resolve the source manually, or run the audit with network access.')
            elif identity != 'resolved':
                resolution = ref.get('resolution') or {}
                doi_record = resolution.get('printed_doi_record')
                if doi_record:
                    found = doi_record.get('title') or 'an untitled record'
                    add('printed_doi_conflict', 'moderate', 'The printed DOI belongs to a different work',
                        f'The DOI in the reference list leads to “{found}”'
                        + (f" ({doi_record['year']})" if doi_record.get('year') else '')
                        + ', which does not match the printed title or authors.',
                        'Look the work up at the publisher and correct the DOI or the reference.')
                elif identity == 'provider_unavailable':
                    add('identity_unresolved', 'moderate', 'Not checked: a scholarly database did not respond',
                        provider_outage(resolution),
                        'Run the audit again later. If it keeps happening, see “Database errors” in the README.',
                        True)
                elif split:
                    pass        # half an entry cannot be found; the split finding says so and how to merge it
                else:
                    add('identity_unresolved', 'moderate',
                        'No certain match in scholarly databases' if identity == 'unresolved'
                        else 'Not found in scholarly databases' if identity == 'not_found'
                        else 'Source identity remains unresolved',
                        not_found_detail(resolution, identity) if identity in ('not_found', 'unresolved')
                        else f'Resolution state: {status_label(identity)}. Search candidates require identity checks.',
                        'Inspect the author, title, year and DOI evidence before using a candidate abstract.')
            consistency = ref.get('metadata_consistency')
            differences = (consistency or {}).get('differences')
            if differences is None:
                differences = ref.get('resolution', {}).get('metadata_differences', [])
            notes = [d for d in differences if d.get('severity') == 'info']
            differences = [d for d in differences if d.get('severity') != 'info']
            if notes:
                add('author_list_shorter', 'informational', 'The database record lists fewer authors',
                    ' '.join(d.get('detail') or '' for d in notes),
                    'No action needed unless the names the database leaves out look doubtful.')
            if differences:
                major = any(d.get('severity', 'major') == 'major' for d in differences)
                fabricated = (consistency or {}).get('status') == 'unresolved_unregistered_doi'
                add('metadata_difference', 'high' if fabricated else 'moderate' if major else 'low',
                    ('doi.org has no record of the printed DOI, and no matching work was found' if fabricated
                     else 'Details differ from the published record'),
                    describe_differences(differences),
                    ('Confirm that this reference exists as cited before relying on it.' if fabricated
                     else 'Check the original publication and correct the bibliography if appropriate.'))
            year_conflicts = original.get('citation_year_mismatches') or ref.get('citation_year_mismatches') or []
            if year_conflicts:
                listed = year_conflicts[0].get('listed_year')
                listed = {'no_date': 'no year (o. J. / n.d.)', 'in_press': 'in press'}.get(listed, listed)
                # One entry per way of citing it, with every page: “Axelsson, 2023” (pp. 13, 17).
                pages_by_form: dict = {}
                for conflict in year_conflicts:
                    pages = pages_by_form.setdefault(conflict['cited_as'], [])
                    page = (conflict.get('source_locations') or [{}])[0].get('page')
                    if page is not None and page not in pages:
                        pages.append(page)
                cited = '; '.join(
                    f"“{form}”" + ((f" (p. {pages[0]})" if len(pages) == 1 else
                                    f" (pp. {', '.join(str(p) for p in pages)})") if pages else '')
                    for form, pages in pages_by_form.items())
                add('citation_year_mismatch', 'moderate', 'Cited with a different year than the reference list gives',
                    f'Cited as {cited}; the reference list gives {listed}. One of the two is likely a typo, '
                    'or a different edition is meant.',
                    'Check the publication and correct the year in the text or in the reference list.')
            if not parsed.get('doi'):
                add('doi_omitted_in_manuscript', 'informational', 'DOI is omitted in the manuscript',
                    'This describes the bibliography entry; it does not mean the work has no DOI.',
                    'Use a resolved source identifier when updating citation formatting.')
            if identity == 'resolved' and not ref.get('external_metadata', {}).get('doi'):
                add('identifier_unavailable', 'informational', 'No DOI returned for the resolved work',
                    'Some works have no DOI; the available metadata does not establish one.',
                    'Retain another persistent identifier or check the publication.')
            if ref.get('citation_count', 0) == 0:
                pair = renumbered.get(str(ref.get('ref_id')))
                add('not_cited', 'low', 'Listed but never cited in the text',
                    'The supported citation patterns did not locate a use of this entry. This is not a relevance score.'
                    + (f' Entries {pair[0]} and {pair[1]} look like one reference split in two; if so, the text\'s '
                       f'{pair[1]} probably cites this entry, and it is cited after all.' if pair else ''),
                    'Check whether the entry is uncited or uses an unsupported citation format.')
            if evidence == 'source_unavailable':
                add('source_unavailable', 'informational', 'Source text is unavailable',
                    'Identity was resolved but an abstract could not be retrieved.',
                    'Obtain the cited source before assessing its support for the claim.')
            if evidence in {'grounding_failed', 'assessment_unavailable'}:
                codes = dict.fromkeys(a.get('error_code') for a in ref.get('claim_assessments', [])
                                      if a.get('status') != 'grounded_proposal')
                add('assessment_error', 'moderate', 'Assessment requires another attempt',
                    ' '.join(assessment_message(code) for code in codes) or assessment_message(None),
                    'Inspect the diagnostic code and rerun assessment. No support verdict was accepted.', True)
            if ref.get('external_metadata', {}).get('is_retracted') is True:
                add('retraction_reported', 'high', 'Metadata provider reports a retraction',
                    'This is a provider-reported status and needs verification against the publication notice.',
                    'Inspect the retraction notice and assess its effect on the cited claim.')
            decisions = {a.get('decision') for a in ref.get('claim_assessments', []) if a.get('status') == 'grounded_proposal'}
            source_kind = (ref.get('source_snapshot') or {}).get('kind') or 'abstract'
            source_noun = 'full-text passages' if source_kind == 'full_text' else 'abstract'
            if 'contradicts' in decisions:
                add('possible_contradiction', 'high', 'Model proposes a contradiction',
                    f'Quoted text was located in the assigned {source_noun}; the interpretation has not been reviewed by a person.',
                    'Read the manuscript claim beside the source and verify the interpretation.')
            relevance = ref.get('relevance') or {}
            irrelevance_raised = needs_relevance_review(relevance)
            if irrelevance_raised:
                parts = [f"Hybrid relevance {relevance.get('RS_final')} ({relevance.get('basis', 'n/a').replace('_', ' ')})"]
                if relevance.get('RS_llm') is not None:
                    parts.append(f"grounded model relevance {relevance['RS_llm']}")
                if relevance.get('RS_embed') is not None:
                    parts.append(f"topic similarity {relevance['RS_embed']}")
                if relevance.get('unrelated_uses'):
                    parts.append(f"{relevance['unrelated_uses']} citation use(s) judged unrelated to the cited {source_noun}")
                add('potentially_irrelevant', 'moderate', 'Reference may not be relevant to the claim it supports',
                    '; '.join(parts) + '. Scores combine quotation-grounded model proposals with topic similarity and use uncalibrated default thresholds.',
                    'Read each citing sentence beside the source and decide whether the citation is appropriate.')
            elif relevance.get('label') == 'low_topical_similarity':
                add('low_topical_similarity', 'informational', 'Low topical similarity to the manuscript',
                    f"Topic similarity {relevance.get('RS_embed')} with no grounded claim assessment available. "
                    'Topic distance alone is common for methods, data and tooling citations.',
                    'No action unless the citing context also looks unrelated.')
            if decisions & {'context_only', 'insufficient_evidence'} and not irrelevance_raised:
                add('limited_support', 'low', f'Available {source_noun} do not establish the claim'
                    if source_kind == 'full_text' else 'Available abstract does not establish the claim',
                    'The model found context or insufficient evidence. ' + (
                        'Only selected passages were assessed; other parts of the paper may support the claim.'
                        if source_kind == 'full_text' else
                        'An abstract cannot establish absence of support in the full paper.'),
                    'Review the full source before drawing a conclusion.')
            self_citation = ref.get('self_citation') or {}
            if self_citation.get('questionable_reasons'):
                reasons = {'not_cited_in_text': 'never cited in the text',
                           'potentially_irrelevant': 'may not support the claim it is attached to'}
                add('questionable_self_citation', 'moderate', 'Self-citation needs a relevance check',
                    'This reference shares an author with the manuscript '
                    f"({'ORCID-confirmed' if self_citation.get('tier') == 'orcid' else 'matching surname and initial'}) and is "
                    + ' and '.join(reasons[r] for r in self_citation['questionable_reasons']) + '. '
                    'Self-citation itself is normal; this flags only its use here.',
                    'Confirm the citation is needed for the claim; otherwise suggest removing it.')
        matched_title = (ref.get('external_metadata') or {}).get('title') if identity == 'resolved' else None
        references.append(ReviewReference(id=uid, marker=str(ref.get('ref_id', ordinal + 1)),
            title=repaired_title(parsed.get('title'), matched_title) or ('Reference metadata unavailable' if
                'missing_or_duplicate_model_entry' in original.get('extraction_issues', [])
                else 'Incomplete bibliography entry'), identity_status=identity,
            evidence_status=evidence, citation_count=ref.get('citation_count', 0),
            claim_assessment=ref.get('claim_assessment', 'not_assessed'),
            quote_count=sum(len(a.get('validated_quotes', [])) for a in ref.get('claim_assessments', [])),
            topic_similarity=ref.get('RS_embed') if modern else None, finding_ids=ref_findings,
            relevance_score=(ref.get('relevance') or {}).get('RS_final') if modern else None,
            relevance_label=(ref.get('relevance') or {}).get('label', 'not_scored') if modern else 'not_scored',
            relevance_basis=(ref.get('relevance') or {}).get('basis', 'none') if modern else 'none',
            llm_relevance=(ref.get('relevance') or {}).get('RS_llm') if modern else None,
            self_citation=(ref.get('self_citation') or {}).get('tier', 'none') if modern else 'none',
            metadata_severity=(ref.get('metadata_consistency') or {}).get('severity', 'none') if modern else 'none',
            evidence_kind=(ref.get('source_snapshot') or {}).get('kind') if modern else None,
            year=printed_year(parsed, original), authors=printed_authors(parsed)))
    if modern:
        analysis_findings(data, findings)
        document_findings(data, references, findings)
    counts = {'total_references': len(references), 'citation_occurrences': len(data.get('citations_in_text', [])),
        'resolved_references': sum(r.identity_status == 'resolved' for r in references),
        'unresolved_references': sum(r.identity_status != 'resolved' for r in references),
        'grounded_proposals': sum(r.evidence_status == 'grounded_proposal' for r in references),
        'unassessed_references': sum(r.evidence_status != 'grounded_proposal' for r in references),
        'references_to_review': sum(bool(r.finding_ids) for r in references),
        # References with something a reader should look at: excludes notes for
        # information and technical issues, which say nothing about the paper.
        'attention_references': len({f.reference_id for f in findings if f.reference_id
                                      and f.priority != 'informational' and not f.system_issue}),
        'system_issues': sum(f.system_issue for f in findings), 'findings': len(findings),
        'reviewer_decisions': 0,
        # From the findings, so the count always matches the items a reviewer sees.
        'potentially_irrelevant': len({f.reference_id for f in findings if f.category == 'potentially_irrelevant'}),
        'borderline_relevance': sum(r.relevance_label == 'borderline' for r in references),
        # "Scored" means the citing claims were judged; a topic score alone is counted apart.
        'relevance_scored': sum(r.relevance_basis in ('hybrid', 'llm_only') for r in references),
        'topic_only': sum(r.relevance_label == 'topic_only' for r in references),
        'self_citations': sum(r.self_citation in ('orcid', 'name_initial') for r in references),
        'metadata_inconsistencies': sum(r.metadata_severity in ('major', 'minor') for r in references),
        'full_text_sources': sum(r.evidence_kind == 'full_text' for r in references),
        # The same threshold as the review item, so the count matches what is raised;
        # weaker leads are counted separately.
        'possibly_missing_works': sum(c.get('support_count', 0) >= STRONG_SUPPORT for c in
                                      ((data.get('literature_gap_analysis') or {}).get('cocitation') or {}).get('candidates') or []),
        'missing_work_leads': len(((data.get('literature_gap_analysis') or {}).get('cocitation') or {}).get('candidates') or []),
        'verified_suggestions': ((data.get('literature_gap_analysis') or {}).get('counts') or {}).get('verified_suggestions', 0)}
    priorities = Counter(f.priority for f in findings)
    counts.update({f'{level}_priority_findings': priorities[level] for level in ('high', 'moderate', 'low', 'informational')})
    assessments = assessment_diagnostics(data)
    counts.update(assessment_tasks=len(assessments),
                  grounded_assessments=sum(a['status'] == 'grounded_proposal' for a in assessments),
                  unavailable_assessments=sum(a['status'] == 'unavailable' for a in assessments),
                  grounding_failures=sum(a['status'] == 'grounding_failed' for a in assessments))
    full_text = counts['full_text_sources']
    ocr = bool((data.get('source_document') or {}).get('ocr', {}).get('pages_applied'))
    limitations = ['All assessments are unreviewed model proposals, not findings of misconduct.',
                  'Quote validation proves attribution to the supplied source text, not that the interpretation is correct.',
                  (f'{full_text} of {len(references)} references were assessed against passages selected from open-access full text; '
                   'the rest against abstracts only.' if full_text else
                   'Sources were assessed against retrieved abstracts only; no open-access full text was used.'),
                  'Relevance scores combine grounded model proposals with topic similarity using default thresholds; '
                  'validate them with the evaluation harness before relying on the labels.',
                  ('Some manuscript pages were read with OCR; quotations located on those pages carry recognition error.'
                   if ocr else 'Citation locations refer to extracted page text.')]
    if data.get('extraction_metadata', {}).get('warnings'):
        limitations.extend(extraction_message(code) for code in data['extraction_metadata']['warnings'])
    status = 'legacy_unverified' if not modern else ('completed_with_issues' if counts['unresolved_references'] or
             counts['system_issues'] or any(r.evidence_status == 'source_unavailable' for r in references) else 'completed')
    return ReviewProjection(title=data.get('manuscript_metadata', data.get('metadata', {})).get('title') or 'Untitled manuscript',
        processing_status=status, counts=counts, references=references, findings=findings, limitations=limitations,
        analysis=analysis_summary(data) if modern else {}).model_dump(mode='json')


def printed_year(parsed, original):
    """The year as the reference list prints it (2024, 2024a, o. J., i. E.)."""
    label = (original.get('bibliography') or {}).get('year_label')
    year = parsed.get('year')
    value = label or (str(year) if year not in (None, '') else None)
    return str(value)[:12] if value else None


def printed_authors(parsed):
    """Up to three printed author names, surname first, for display and lookup."""
    names = []
    for author in parsed.get('authors') or []:
        name = author.get('display_name', '') if isinstance(author, dict) else str(author or '')
        name = ' '.join(name.split())
        if name:
            names.append(name[:80])
        if len(names) == 3:
            break
    return names


def document_findings(data, references, findings):
    """Whole-document findings about how well the document could be read.

    The reliability gate comes first: when the reference list was not read
    well enough for the per-reference results to mean anything, the audit says
    so instead of presenting dozens of routine review items as findings.
    """
    def add(category, priority, summary, explanation, action, system=False):
        findings.insert(0, ReviewFinding(id=digest_text('manuscript:' + category), reference_id=None,
            category=category, priority=priority, summary=summary, explanation=explanation, action=action,
            system_issue=system))
    metadata = data.get('extraction_metadata') or {}
    lists = metadata.get('bibliographies') or []
    if len(lists) > 1:
        add('multiple_reference_lists', 'moderate', f'This PDF contains {len(lists)} separate reference lists',
            'It looks like an edited book, proceedings or a cumulative thesis. Each list was matched only with the '
            'citations in its own part. The self-citation check compares references with the authors of the whole '
            'document, which is not meaningful for a volume with different authors per chapter.',
            'Check the document chapter by chapter: each chapter then gets its own authors, citations and report.')
    if metadata.get('degraded'):
        add('read_without_model', 'informational', 'The reference list was read without an AI model',
            extraction_message('model_unavailable_rule_based_parse'),
            'Run the check again when an AI model is available to refine titles; the citations found stay the same.')
    total = len(references)
    if total:
        problems = []
        raw = data.get('scored_references') or []
        needs_review = sum((ref.get('original_data') or {}).get('extraction_status') != 'located' for ref in raw)
        if needs_review / total > 0.5:
            problems.append(f'{needs_review} of {total} reference entries could not be read cleanly')
        cited = sum(r.citation_count > 0 for r in references)
        if total >= 5 and cited / total < 0.2:
            problems.append(f'citations were found for only {cited} of {total} references')
        unassigned = int(metadata.get('unassigned_model_entries') or 0)
        if unassigned >= 3 and unassigned > 0.2 * (total + unassigned):
            # Entries the model read but no printed number could hold: the list was cut
            # short, and the count of references checked is too low.
            problems.append(f'{unassigned} references read from the list could not be matched to a printed entry, '
                            f'so only {total} of about {total + unassigned} were checked')
        resolved = sum(r.identity_status == 'resolved' for r in references)
        offline = all(r.identity_status == 'offline_mode' for r in references)
        if total >= 5 and resolved == 0 and not offline:
            problems.append(f'none of the {total} references could be matched to a published record')
        if problems:
            add('extraction_reliability', 'high', 'This audit could not read the document reliably',
                'CitePrism could not read this document well enough for the per-reference results to be trusted: '
                + '; '.join(problems) + '. Typical causes are an unusual citation style, several reference lists in '
                'one PDF, or a scanned document.',
                'Do not rely on the items below. Check whether the PDF is a single paper with one reference list, '
                'split books into chapters, and run the check again.', True)


def analysis_findings(data, findings):
    """Manuscript-level review items: self-citation patterns and literature gaps."""
    def add(category, priority, summary, explanation, action):
        findings.append(ReviewFinding(id=digest_text('manuscript:' + category), reference_id=None,
            category=category, priority=priority, summary=summary, explanation=explanation, action=action))
    self_citation = data.get('self_citation_analysis') or {}
    patterns = self_citation.get('patterns') or []
    if patterns:
        add('self_citation_pattern', 'moderate', 'Self-citation pattern for review',
            ' '.join(p['detail'] for p in patterns) + ' Self-citation is normal scholarship; '
            'these signals ask whether specific self-citations are needed, not whether misconduct occurred.',
            'Review the listed self-citations for relevance to their citing claims.')
    gaps = data.get('literature_gap_analysis') or {}
    candidates = (gaps.get('cocitation') or {}).get('candidates') or []
    strong = [c for c in candidates if c.get('support_count', 0) >= STRONG_SUPPORT]
    if strong:
        top = '; '.join(f"\"{c.get('title')}\" ({c.get('year')}) — cited by {c['support_count']} of your references"
                        for c in strong[:3])
        add('possibly_missing_literature', 'low', 'Frequently co-cited work is not cited',
            f'{len(strong)} work(s) are cited by three or more of this manuscript\'s own references but not by the manuscript: {top}.',
            'Consider whether these works are relevant to the manuscript\'s claims. Their absence may be appropriate.')
    unmatched = [s for s in gaps.get('suggestions') or [] if s.get('verification_status') == 'no_matching_record']
    if unmatched:
        add('unverified_search_leads', 'informational', 'Some generated search leads match no real record',
            f'{len(unmatched)} model-suggested work(s) could not be found in any scholarly index and may not exist.',
            'Do not cite these leads; use only the verified suggestions and co-citation candidates.')


def analysis_summary(data):
    self_citation = data.get('self_citation_analysis') or {}
    gaps = data.get('literature_gap_analysis') or {}
    return {
        'self_citation': {key: self_citation.get(key) for key in
                          ('status', 'reason', 'counts', 'patterns', 'questionable', 'by_author', 'thresholds',
                           'guidance', 'identity_note')}
                         if self_citation else {},
        'literature_gaps': {'status': gaps.get('status'), 'counts': gaps.get('counts', {}),
                            'cocitation': {k: (gaps.get('cocitation') or {}).get(k) for k in
                                           ('status', 'references_with_graph', 'min_support', 'candidates')},
                            'suggestions': gaps.get('suggestions', []), 'limitations': gaps.get('limitations', [])}
                           if gaps else {},
        'versions': {k: v for k, v in (data.get('analysis_metadata') or {}).items() if k.endswith('version')},
    }


def evidence_scope(review):
    """What the assessments were read against, in one sentence."""
    full_text = review['counts'].get('full_text_sources', 0)
    total = review['counts'].get('total_references', 0)
    if full_text:
        return f'{full_text} of {total} sources were assessed against open-access full text; the rest against abstracts.'
    return 'Sources were assessed against abstracts only.'


def report_html(data):
    """Portable UTF-8 export using precisely the projection displayed in the app."""
    review = build_review(data)
    esc = lambda value: html.escape(str(value), quote=True)
    rows = ''.join('<tr>' + ''.join(f'<td>{esc(status_label(r[k]) if k in ("identity_status", "evidence_status", "claim_assessment") else r[k])}</td>' for k in
        ('marker', 'title', 'identity_status', 'evidence_status', 'claim_assessment')) + '</tr>' for r in review['references'])
    titles = {r['id']: r['marker'] + ' · ' + r['title'] for r in review['references']}
    findings = ''.join(f"<li><strong>{esc(titles.get(f['reference_id'], 'Manuscript'))}: {esc(f['summary'])}</strong> — "
                       f"{esc(f['explanation'])}<br>{esc(f['action'])}</li>" for f in review['findings'])
    checks = ''.join(f"<tr><td>{esc(a['reference'])}</td><td>{esc(status_label(a['status']))}</td>"
                     f"<td>{esc(status_label(a['decision']))}</td><td>{esc(a['detail'])}</td></tr>"
                     for a in assessment_diagnostics(data))
    quotes = []
    for ref in data.get('scored_references', []):
        for observation in ref.get('claim_assessments', []):
            if observation.get('status') != 'grounded_proposal':
                continue
            claim = (observation.get('claim_anchor') or {}).get('text', '')
            for quote in observation.get('validated_quotes', []):
                quotes.append(f"<article><h3>{esc(ref.get('ref_id'))}</h3><p>Manuscript: {esc(claim)}</p>"
                              f"<blockquote>{esc(quote['text'])}</blockquote><p>Source snapshot: {esc(ref['source_snapshot']['snapshot_id'])}</p></article>")
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>CitePrism evidence review</title>'
        '<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 24px;color:#182b3c;line-height:1.55}'
        'table{border-collapse:collapse;width:100%}td,th{border:1px solid #cbd5df;padding:10px;text-align:left}'
        'blockquote{border-left:3px solid #166b73;padding:16px;background:#f1f7f8}li{margin:16px 0}code{overflow-wrap:anywhere}</style>'
        f"<h1>{esc(review['title'])}</h1><p>Processing: {esc(review['processing_status'])} · Human review: not reviewed</p>"
        f"<p>{esc(evidence_scope(review))} Model proposals require human review.</p><table><thead><tr>"
        '<th>Reference</th><th>As cited</th><th>Identity</th><th>Evidence</th><th>Proposal</th></tr></thead>'
        f'<tbody>{rows}</tbody></table><h2>Assessment checks</h2>'
        '<p>Provider call success means a response was received. Each assessment must also pass the checks below.</p>'
        '<table><thead><tr><th>Reference</th><th>Assessment</th><th>Proposal</th><th>Detail</th></tr></thead>'
        f'<tbody>{checks}</tbody></table><h2>Review queue</h2><ul>{findings}</ul><h2>Located quotations</h2>' + ''.join(quotes) +
        '<h2>Scope and limitations</h2><ul>' + ''.join(f'<li>{esc(t)}</li>' for t in review['limitations']) + '</ul></html>')


def review_json(data):
    return json.dumps(build_review(data), ensure_ascii=False, indent=2, allow_nan=False)
