"""Identity-first enrichment with retained alternatives and source snapshots."""

import copy
import json
import logging
from pathlib import Path
from urllib.parse import quote

import requests

from src.evidence import RESOLVER_VERSION
from src.evidence.identity import resolve_candidates
from src.evidence.providers import CallRecorder, ProviderFailure, utc_now
from src.evidence.text import digest_json, digest_text
from src.utils.reference_matching import normalize_doi

logger = logging.getLogger(__name__)


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


class OpenAlexEnricher:
    def __init__(self, config, db_manager):
        self.config, self.db = config, db_manager
        directory = Path(getattr(config, 'DATA_DIR', 'data')) / 'debug' / 'metadata_calls'
        self.recorder = CallRecorder(directory)
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'CitePrism/3.1 evidence-review'

    def _get_json(self, provider, url, params):
        # API credentials are supplied to transport only and never recorded in artifacts.
        transport = dict(params)
        if provider == 'openalex' and getattr(self.config, 'OPENALEX_API_KEY', None):
            transport['api_key'] = self.config.OPENALEX_API_KEY
        def invoke():
            response = self.session.get(url, params=transport, timeout=(10, 35))
            response.raise_for_status()
            return response.json()
        raw = self.recorder.call(provider=provider, model='metadata-api', purpose='reference_resolution',
            prompt_version=RESOLVER_VERSION, request={'url': url, 'params': params}, invoke=invoke,
            decode=lambda value: (json.dumps(value, ensure_ascii=False), None))
        return json.loads(raw)

    def _openalex_candidates(self, parsed):
        doi = normalize_doi(parsed.get('doi')).strip()
        candidates = []
        if doi:
            candidates = self._get_json('openalex', 'https://api.openalex.org/works',
                                        {'filter': f'doi:https://doi.org/{doi}', 'per-page': 5}).get('results', [])
        if not candidates and parsed.get('title'):
            candidates = self._get_json('openalex', 'https://api.openalex.org/works',
                                        {'search': parsed['title'], 'per-page': 10}).get('results', [])
        return candidates

    def _multi_provider_lookup(self, parsed):
        """Phase 4: resolve against every enabled scholarly index, then acquire
        full text when an open-access location exists."""
        from src.evidence.resolution import resolve_with_providers
        from src.evidence.scholarly import build_clients

        call_start = len(self.recorder.records)
        try:
            openalex = self._openalex_candidates(parsed)
        except ProviderFailure as error:
            openalex = []
            logger.info('OpenAlex unavailable during resolution: %s', error.code)
        clients = build_clients(self.config, self.recorder, self.session)
        resolution = resolve_with_providers(parsed, clients, openalex_candidates=openalex)

        selected = next((c for c in resolution['candidates'] if c['selected']), None)
        external = dict(selected['metadata']) if selected else {}
        snapshot = None
        if selected:
            snapshot = self._acquire_snapshot(selected, external, resolution)
            external.update({
                'abstract': (snapshot or {}).get('text') if snapshot else None,
                'abstract_source': (snapshot or {}).get('provider') if snapshot else None,
                'evidence_kind': (snapshot or {}).get('kind') if snapshot else None,
                'match_method': resolution['method'],
                'match_confidence': None,
                'provider_agreement': selected.get('provider_agreement'),
                'is_scraped': False,
            })
        return {'resolution': resolution, 'external_metadata': external,
                'source_snapshot': snapshot, 'lookup_observed_at': utc_now(),
                'lookup_calls': self.recorder.records[call_start:]}

    def _acquire_snapshot(self, selected, external, resolution):
        """Prefer open full text; otherwise keep the abstract, labeled as one."""
        from src.evidence.fulltext import FullTextAcquirer
        from src.evidence.resolution import selected_abstract

        if getattr(self.config, 'ENABLE_FULL_TEXT_ACQUISITION', False):
            try:
                acquirer = FullTextAcquirer(self.config, self.recorder, self.session)
                full_text = acquirer.acquire(selected, external)
                if full_text:
                    return full_text
            except Exception:
                logger.exception('Full-text acquisition failed; falling back to the abstract.')

        abstract = selected_abstract(resolution, [
            {**c.get('metadata', {}), 'id': c['candidate_id'],
             'abstract': c.get('metadata', {}).get('abstract')}
            for c in resolution.get('candidates', [])
        ])
        if not abstract:
            return None
        snapshot = {'text': abstract['text'], 'text_sha256': digest_text(abstract['text']),
                    'kind': 'abstract', 'provider': abstract['provider'],
                    'url': abstract.get('url') or '', 'retrieved_at': utc_now(),
                    'candidate_id': selected['candidate_id'], 'doi': external.get('doi')}
        snapshot['snapshot_id'] = digest_json(snapshot)
        return snapshot

    def _lookup(self, parsed):
        if len(getattr(self.config, 'RESOLUTION_PROVIDERS', ('openalex',))) > 1:
            return self._multi_provider_lookup(parsed)
        call_start = len(self.recorder.records)
        candidates = self._openalex_candidates(parsed)
        resolution = resolve_candidates(candidates, parsed)
        selected = next((c for c in resolution['candidates'] if c['selected']), None)
        external = dict(selected['metadata']) if selected else {}
        snapshot = None
        if selected:
            work = next(w for w in candidates if str(w.get('id')) == selected['candidate_id'])
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
                            from bs4 import BeautifulSoup
                            abstract = BeautifulSoup(raw, 'html.parser').get_text(' ', strip=True)
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
                             'match_confidence': None, 'is_scraped': False})
        return {'resolution': resolution, 'external_metadata': external, 'source_snapshot': snapshot,
                'lookup_observed_at': utc_now(), 'lookup_calls': self.recorder.records[call_start:]}

    def enrich_references(self, parsed_data, force=False):
        result = copy.deepcopy(parsed_data)
        result['manuscript_metadata'] = parsed_data.get('metadata', {})
        result['enriched_references'] = []
        cache_hits = 0
        reused_calls = []
        for index, ref in enumerate(parsed_data.get('references_list', [])):
            parsed = ref.get('parsed', {})
            key = digest_json({'resolver': RESOLVER_VERSION, 'parsed': parsed})
            outcome = None
            if not force:
                outcome = self.db.get_cached_response('openalex', key)
                if outcome:
                    from datetime import datetime, timezone, timedelta
                    observed = outcome.get('lookup_observed_at')
                    try:
                        stale = datetime.fromisoformat(observed) < datetime.now(timezone.utc) - timedelta(days=7)
                    except (TypeError, ValueError):
                        stale = True
                    if stale:
                        outcome = None
                    else:
                        cache_hits += 1
            if not outcome:
                if not parsed.get('title') and not parsed.get('doi'):
                    outcome = {'resolution': {'status': 'insufficient_metadata', 'candidates': [],
                               'resolver_version': RESOLVER_VERSION}, 'external_metadata': {}, 'source_snapshot': None}
                else:
                    try:
                        outcome = self._lookup(parsed)
                        self.db.cache_api_response('openalex', key, outcome)
                    except ProviderFailure as error:
                        outcome = {'resolution': {'status': 'provider_unavailable', 'candidates': [],
                                   'error_code': error.code, 'resolver_version': RESOLVER_VERSION},
                                   'external_metadata': {}, 'source_snapshot': None}
            outcome = copy.deepcopy(outcome)
            reused_calls.extend(outcome.pop("lookup_calls", []))
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
                'consistency_status': 'Review metadata differences' if outcome['resolution'].get('metadata_differences') else 'See identity evidence'})
        refs = result['enriched_references']
        result['enrichment_metadata'] = {'method_version': RESOLVER_VERSION, 'generated_at': utc_now()}
        result['enrichment_summary'] = {'total_references': len(refs), 'cache_hits': cache_hits,
            'successfully_enriched': sum(r['resolution']['status'] == 'resolved' for r in refs),
            'unresolved': sum(r['resolution']['status'] != 'resolved' for r in refs)}
        all_calls = result.get('provider_runs', []) + reused_calls + self.recorder.records
        result['provider_runs'] = list({call['id']: call for call in all_calls}.values())
        return result
