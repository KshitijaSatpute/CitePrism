"""Identity-first enrichment with retained alternatives and source snapshots.

This module owns the enrichment *loop* — cache lookup, bounded concurrency,
ordering, and the extraction-status guard — for every enricher. Subclasses only
change how one reference is looked up (`_lookup`) and what makes a cached
outcome reusable (`cache_namespace`).

Concurrency is safe because each worker thread gets its own enricher fork with
its own `CallRecorder` and HTTP session, so provider-call artifacts are never
attributed to the wrong reference. Cache reads and writes stay on the calling
thread. Outbound calls are paced by `src.evidence.ratelimit`.
"""

import copy
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from src.evidence import RESOLVER_VERSION
from src.evidence import ratelimit
from src.evidence.identity import resolve_candidates
from src.evidence.providers import CallRecorder, ProviderFailure, utc_now
from src.evidence.scholarly import query_title
from src.evidence.text import digest_json, digest_text
from src.utils.reference_matching import normalize_doi

logger = logging.getLogger(__name__)

CACHE_MAX_AGE = timedelta(days=7)
# OpenAlex returns these on every work record. They power the literature-gap
# analysis without any additional request, so they are retained for the
# selected work only (never for every candidate, which would bloat artifacts).
GRAPH_FIELDS = ("referenced_works", "related_works", "cited_by_count", "type")


def reconstruct_abstract(inverted_index):
    if not inverted_index:
        return None
    positions = {}
    try:
        for word, offsets in inverted_index.items():
            for offset in offsets:
                if offset in positions or not isinstance(offset, int) or offset < 0:
                    return None
                positions[offset] = word
        if sorted(positions) != list(range(len(positions))):
            return None
        return ' '.join(positions[i] for i in range(len(positions)))
    except (TypeError, ValueError):
        return None


def graph_fields(work):
    """Citation-graph metadata kept for the selected work."""
    if not isinstance(work, dict):
        return {}
    graph = {key: work.get(key) for key in GRAPH_FIELDS if work.get(key) is not None}
    for key in ('referenced_works', 'related_works'):
        if key in graph:
            graph[key] = [str(item) for item in graph[key] if item][:400]
    open_access = work.get('open_access') or {}
    if open_access.get('oa_url'):
        graph['open_access_url'] = open_access['oa_url']
    if work.get('id'):
        graph['openalex_id'] = str(work['id'])
    return graph


def offline_outcome():
    return {'resolution': {'status': 'offline_mode', 'candidates': [],
                           'resolver_version': RESOLVER_VERSION,
                           'note': 'Offline mode: no scholarly index was queried.'},
            'external_metadata': {}, 'source_snapshot': None}


class OpenAlexEnricher:
    """OpenAlex identity resolution with a DOI-keyed Crossref abstract fallback."""

    def __init__(self, config, db_manager):
        self.config, self.db = config, db_manager
        self.call_directory = Path(getattr(config, 'DATA_DIR', 'data')) / 'debug' / 'metadata_calls'
        self.recorder = CallRecorder(self.call_directory)
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'CitePrism/5.0 evidence-review (mailto:{})'.format(
            getattr(config, 'OPENALEX_EMAIL', '') or 'unset')

    # ------------------------------------------------------------------
    # extension points
    # ------------------------------------------------------------------
    def method_version(self):
        return RESOLVER_VERSION

    def cache_namespace(self):
        """Everything that changes a lookup's result belongs in the cache key."""
        return {'resolver': RESOLVER_VERSION, 'providers': ['openalex'], 'graph': 1}

    def fork(self):
        """A copy with its own recorder and HTTP session, for one worker thread.

        An injected transport (an instance-level `_get_json`, as offline replays
        and tests use) is carried over, so a fork never silently reaches the
        network when its parent was told not to.
        """
        clone = type(self)(self.config, self.db)
        if '_get_json' in self.__dict__:
            clone._get_json = self.__dict__['_get_json']
        return clone

    def offline(self):
        return bool(getattr(self.config, 'OFFLINE_MODE', False))

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    def _get_json(self, provider, url, params):
        # API credentials are supplied to transport only and never recorded in artifacts.
        transport = dict(params)
        if provider == 'openalex':
            if getattr(self.config, 'OPENALEX_API_KEY', None):
                transport['api_key'] = self.config.OPENALEX_API_KEY
            email = getattr(self.config, 'OPENALEX_EMAIL', None)
            if email and '@' in str(email) and 'example.com' not in str(email):
                transport.setdefault('mailto', email)

        def invoke():
            ratelimit.acquire(provider)
            response = self.session.get(url, params=transport, timeout=(10, 35))
            response.raise_for_status()
            return response.json()

        raw = self.recorder.call(provider=provider, model='metadata-api', purpose='reference_resolution',
            prompt_version=RESOLVER_VERSION, request={'url': url, 'params': params}, invoke=invoke,
            decode=lambda value: (json.dumps(value, ensure_ascii=False), None))
        return json.loads(raw)

    def _openalex_works(self, parsed):
        """Raw OpenAlex work records: exact DOI first, then a title search."""
        doi = normalize_doi(parsed.get('doi')).strip()
        works = []
        if doi:
            works = self._get_json('openalex', 'https://api.openalex.org/works',
                                   {'filter': f'doi:https://doi.org/{doi}', 'per-page': 5}).get('results', [])
        if not works and parsed.get('title'):
            works = self._get_json('openalex', 'https://api.openalex.org/works',
                                   {'search': query_title(parsed), 'per-page': 10}).get('results', [])
        return works

    # ------------------------------------------------------------------
    # one reference
    # ------------------------------------------------------------------
    def _lookup(self, parsed):
        call_start = len(self.recorder.records)
        candidates = self._openalex_works(parsed)
        resolution = resolve_candidates(candidates, parsed)
        selected = next((c for c in resolution['candidates'] if c['selected']), None)
        external = dict(selected['metadata']) if selected else {}
        snapshot = None
        if selected:
            work = next(w for w in candidates if str(w.get('id')) == selected['candidate_id'])
            external['graph'] = graph_fields(work)
            abstract = reconstruct_abstract(work.get('abstract_inverted_index'))
            source_provider = 'openalex'
            source_url = 'https://api.openalex.org/works/' + quote(str(work['id']).rsplit('/', 1)[-1])
            retrieval_errors = []
            # DOI-keyed Crossref fallback cannot silently attach an arXiv title search hit.
            if not abstract and normalize_doi(external.get('doi')):
                source_url = 'https://api.crossref.org/works/' + quote(normalize_doi(external['doi']), safe='')
                try:
                    response = self._get_json('crossref', source_url, {})
                    message = response.get('message', {})
                    if normalize_doi(message.get('DOI')) == normalize_doi(external['doi']):
                        raw = message.get('abstract')
                        if raw:
                            from src.evidence.scholarly import strip_markup
                            abstract = strip_markup(raw)
                            source_provider = 'crossref'
                except ProviderFailure as error:
                    retrieval_errors.append({'provider': 'crossref', 'error_code': error.code})
            if abstract:
                snapshot = {'text': abstract, 'text_sha256': digest_text(abstract), 'kind': 'abstract',
                            'provider': source_provider, 'url': source_url, 'retrieved_at': utc_now(),
                            'candidate_id': selected['candidate_id'], 'doi': external.get('doi')}
                snapshot['snapshot_id'] = digest_json(snapshot)
            external.update({'abstract': abstract, 'abstract_source': source_provider if abstract else None,
                             'abstract_retrieval_errors': retrieval_errors, 'match_method': resolution['method'],
                             'match_confidence': None, 'is_scraped': False, 'providers': ['openalex'],
                             'provider_agreement': 1, 'evidence_kind': 'abstract' if abstract else None})
        return {'resolution': resolution, 'external_metadata': external, 'source_snapshot': snapshot,
                'lookup_observed_at': utc_now(), 'lookup_calls': self.recorder.records[call_start:]}

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------
    def _cache_key(self, parsed):
        return digest_json({**self.cache_namespace(), 'parsed': parsed})

    @staticmethod
    def _cacheable(outcome):
        """Outcomes shaped by an outage are never reused: the next run must ask again."""
        resolution = (outcome or {}).get('resolution') or {}
        return not (resolution.get('status') == 'provider_unavailable' or resolution.get('provider_errors')
                    or resolution.get('printed_doi_status') == 'unknown')

    def _cached(self, key):
        outcome = self.db.get_cached_response('openalex', key)
        if not outcome or not self._cacheable(outcome):
            return None
        try:
            observed = datetime.fromisoformat(outcome.get('lookup_observed_at'))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
        return None if observed < datetime.now(timezone.utc) - CACHE_MAX_AGE else outcome

    def _concurrency(self):
        raw = getattr(self.config, 'ENRICHMENT_CONCURRENCY', None) or os.getenv(
            'CITEPRISM_ENRICHMENT_CONCURRENCY', '4')
        try:
            return max(1, min(16, int(raw)))
        except (TypeError, ValueError):
            return 4

    def _run_lookups(self, pending):
        """Execute lookups in parallel. `pending` is [(index, parsed)]."""
        results = {}
        if not pending:
            return results
        local = threading.local()

        def worker(item):
            index, parsed = item
            enricher = getattr(local, 'enricher', None)
            if enricher is None:
                enricher = local.enricher = self.fork()
            try:
                return index, enricher._lookup(parsed), None
            except ProviderFailure as error:
                return index, None, error

        workers = self._concurrency()
        if workers == 1 or len(pending) == 1:
            local.enricher = self
            for item in pending:
                index, outcome, error = worker(item)
                results[index] = (outcome, error)
            return results
        with ThreadPoolExecutor(max_workers=min(workers, len(pending)),
                                thread_name_prefix='citeprism-enrich') as pool:
            for index, outcome, error in pool.map(worker, pending):
                results[index] = (outcome, error)
        return results

    def enrich_references(self, parsed_data, force=False):
        result = copy.deepcopy(parsed_data)
        result['manuscript_metadata'] = parsed_data.get('metadata', {})
        result['enriched_references'] = []
        references = parsed_data.get('references_list', [])
        cache_hits = 0
        reused_calls = []

        outcomes, keys, pending = {}, {}, []
        for index, ref in enumerate(references):
            parsed = ref.get('parsed', {})
            keys[index] = self._cache_key(parsed)
            if self.offline():
                outcomes[index] = offline_outcome()
                continue
            if not parsed.get('title') and not parsed.get('doi'):
                outcomes[index] = {'resolution': {'status': 'insufficient_metadata', 'candidates': [],
                                   'resolver_version': RESOLVER_VERSION},
                                   'external_metadata': {}, 'source_snapshot': None}
                continue
            cached = None if force else self._cached(keys[index])
            if cached:
                outcomes[index] = cached
                cache_hits += 1
            else:
                pending.append((index, parsed))

        for index, (outcome, error) in self._run_lookups(pending).items():
            if error is not None:
                outcomes[index] = {'resolution': {'status': 'provider_unavailable', 'candidates': [],
                                   'error_code': error.code, 'resolver_version': RESOLVER_VERSION},
                                   'external_metadata': {}, 'source_snapshot': None}
            else:
                outcomes[index] = outcome
                if self._cacheable(outcome):
                    self.db.cache_api_response('openalex', keys[index], outcome)

        for index, ref in enumerate(references):
            outcome = copy.deepcopy(outcomes[index])
            reused_calls.extend(outcome.pop('lookup_calls', []))
            if ref.get('extraction_status') != 'located':
                # Search alternatives remain useful; an ambiguous printed entry cannot be auto-selected.
                outcome['resolution']['status'] = 'extraction_needs_review'
                outcome['resolution']['selected_candidate_id'] = None
                for candidate in outcome['resolution'].get('candidates', []):
                    candidate['selected'] = False
                outcome['external_metadata'], outcome['source_snapshot'] = {}, None
            result['enriched_references'].append({
                'ref_id': ref.get('ref_id', f'ref_{index+1}'), 'reference_uid': ref.get('reference_uid'),
                'original_data': ref, **outcome, 'enrichment_status': outcome['resolution']['status'],
                'consistency_status': ('Review metadata differences'
                                       if outcome['resolution'].get('metadata_differences')
                                       else 'See identity evidence')})
        refs = result['enriched_references']
        result['enrichment_metadata'] = {'method_version': self.method_version(), 'generated_at': utc_now(),
                                         'concurrency': self._concurrency(),
                                         'offline_mode': self.offline()}
        result['enrichment_summary'] = {'total_references': len(refs), 'cache_hits': cache_hits,
            'lookups_performed': len(pending),
            'successfully_enriched': sum(r['resolution']['status'] == 'resolved' for r in refs),
            'unresolved': sum(r['resolution']['status'] != 'resolved' for r in refs)}
        all_calls = result.get('provider_runs', []) + reused_calls + self.recorder.records
        result['provider_runs'] = list({call['id']: call for call in all_calls}.values())
        return result
