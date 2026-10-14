"""Offline replay of the supplied manuscript. Provider outputs are explicitly simulated."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

from src.evidence import SCHEMA_VERSION
from src.evidence.extraction import pdf_source, anchor_manuscript
from src.enrichers.openalex_enricher import OpenAlexEnricher
from src.evidence.scholarly import query_title
from src.scorers.relevance_scorer import RelevanceScorer

FIXTURES = Path(__file__).parent / 'fixtures' / 'evidence'
PDF = FIXTURES / 'underwater_manuscript.pdf'


def legacy():
    return json.loads((FIXTURES / 'legacy_underwater_result.json').read_text(encoding='utf-8'))


def parsed():
    value = legacy()
    result = anchor_manuscript({'metadata': value['manuscript_metadata'],
        'references_list': [r['original_data'] for r in value['scored_references']]}, pdf_source(PDF))
    result['schema_version'] = SCHEMA_VERSION
    return result


class Cache:
    def __init__(self):
        self.values = {}
    def get_cached_response(self, kind, key):
        return copy.deepcopy(self.values.get((kind, key)))
    def cache_api_response(self, kind, key, value):
        self.values[kind, key] = copy.deepcopy(value)


def work_from_external(external):
    words = (external.get('abstract') or '').split()
    inverted = {}
    for i, word in enumerate(words):
        inverted.setdefault(word, []).append(i)
    return {**external, 'publication_year': external.get('year'), 'abstract_inverted_index': inverted}


def enriched(root):
    value = legacy()
    # Keyed by the query the enricher sends: the printed title with PDF-spaced hyphens rejoined.
    works = {query_title(r['original_data']['parsed']): [work_from_external(r['external_metadata'])]
        for r in value['scored_references'] if r['external_metadata'].get('id')}
    enricher = OpenAlexEnricher(SimpleNamespace(DATA_DIR=root), Cache())
    enricher._get_json = lambda provider, url, params: {'results': works.get(params.get('search'), [])} if provider == 'openalex' else {}
    return enricher.enrich_references(parsed())


class GroundedProvider:
    """Echoes assigned source strings; does not claim fresh scientific validation."""
    def judge_batch(self, tasks):
        return [{'task_id': task['task_id'], 'decision': 'context_only',
                 'claim_quote': task['manuscript_context'],
                 'source_quotes': [task['source_text'][:140]],
                 'rationale': 'Offline regression proposal: the supplied abstract provides context.'} for task in tasks]


def scored(root, provider=None):
    scorer = RelevanceScorer(SimpleNamespace(DATA_DIR=root, HF_BATCH_SIZE=5), Cache())
    scorer.llm_scorer = provider or GroundedProvider()
    result = scorer.score_references(enriched(root))
    result['test_fixture'] = {'mode': 'offline_replay', 'live_provider_calls': False}
    return result
