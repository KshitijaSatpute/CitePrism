"""
CitePrism Pipeline Orchestrator - Updated Version
==================================================
End-to-end pipeline with intelligent caching, per-stage force reprocess,
and Streamlit progress bar integration.

Changes from v1:
- Integrated Gemini extractor wrapper
- Added progress_bar parameter throughout
- Better error handling and logging
- Cross-platform path handling
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional
import numpy as np
from src.risk_detection.risk_detector import run_risk_detection
import time
from datetime import datetime, timezone
from src.features.reference_risk_summary import ReferenceRiskSummarizer
from src.utils.safe_io import (
    atomic_write_json,
    atomic_copy_file,
    content_addressed_pdf_name,
    sha256_file,
    validate_pdf,
)

from src.evidence import SCHEMA_VERSION, EXTRACTION_VERSION, RESOLVER_VERSION, SCORING_VERSION
from src.evidence.text import digest_json
from src.evidence.review import build_review
from src.evidence.providers import ProviderFailure

logger = logging.getLogger(__name__)


def configuration_fingerprint(config, stage):
    fields = {"parsing": ("LLM_PROVIDER", "GOOGLE_MODEL", "OPENAI_MODEL", "OPENAI_COMPATIBLE_MODEL",
                         "MAX_TOKENS", "PDF_EXTRACTOR", "ENABLE_OCR", "OCR_ENGINE", "OCR_DPI",
                         "OCR_LANGUAGES"),
              "enrichment": ("RESOLUTION_PROVIDERS", "SUPPLEMENTARY_PROVIDERS", "ENABLE_FULL_TEXT_ACQUISITION",
                             "ENABLE_UNPAYWALL", "OFFLINE_MODE"),
              # Thresholds and weights are deliberately absent: they only affect the
              # deterministic analysis layer, which is recomputed every audit.
              "scoring": ("ASSESSMENT_PROVIDER", "HF_MODEL", "HF_BATCH_SIZE", "GOOGLE_MODEL",
                          "OPENAI_MODEL", "OPENAI_COMPATIBLE_MODEL", "ENABLE_TOPIC_SIMILARITY",
                          "SIMILARITY_BACKEND", "EMBEDDING_MODEL")}
    return digest_json({k: getattr(config, k, None) for k in fields[stage]})


def current_artifact(path, section, version, fingerprint):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return (value.get("schema_version") == SCHEMA_VERSION and
                value.get(section, {}).get("method_version") == version and
                value.get(section, {}).get("configuration_fingerprint") == fingerprint)
    except (OSError, ValueError, TypeError):
        return False



TRANSIENT_ASSESSMENT_ERRORS = {"timeout", "service_unavailable", "rate_limit", "provider_error", "empty_response"}


def interrupted_stage(path, stage):
    """Whether a stored stage result was cut short by an outage and should be retried.

    "Run the check again" after a database or model outage must actually retry the
    references that failed. Lookups that succeeded stay cached, so only the failed
    ones cost a new request.
    """
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if stage == "enrichment":
        # Only references an outage left unsettled. A reference resolved by OpenAlex while
        # Semantic Scholar was rate-limited is finished; re-running it (and every model
        # assessment after it) would cost a full audit for nothing.
        return any((ref.get("resolution") or {}).get("status") == "provider_unavailable"
                   or ((ref.get("resolution") or {}).get("provider_errors")
                       and (ref.get("resolution") or {}).get("status") != "resolved")
                   for ref in value.get("enriched_references") or [])
    return any(assessment.get("status") == "unavailable"
               and assessment.get("error_code") in TRANSIENT_ASSESSMENT_ERRORS
               for ref in value.get("scored_references") or [] for assessment in ref.get("claim_assessments") or [])


def merge_stage_result(results: Dict, stage_result: Dict) -> None:
    """Merge a stage contract without discarding earlier pipeline evidence."""
    for key in ("stages_completed", "stages_skipped", "errors"):
        for value in stage_result.get(key, []):
            if value not in results[key]:
                results[key].append(value)

    results["file_paths"].update(stage_result.get("file_paths", {}))
    results["timings"].update(stage_result.get("timings", {}))

    for key, value in stage_result.items():
        if key not in {
            "stages_completed", "stages_skipped", "errors", "file_paths", "timings"
        }:
            results[key] = value


# Custom JSON encoder for numpy types
class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        return super().default(obj)


class PipelineOrchestrator:
    """
    Orchestrates the complete CitePrism pipeline with intelligent caching.

    Pipeline Stages:
    1. PDF Upload & Registration
    2. Parsing (Gemini LLM)
    3. Enrichment (OpenAlex API)
    4. Scoring (Embeddings + LLM)
    """

    def __init__(self, db_manager, config, trust_kernel=None):
        """Initialize pipeline orchestrator."""
        self.db = db_manager
        self.config = config
        self.trust_kernel = trust_kernel

        # Use forward slashes or Path constructor for cross-platform compatibility
        self.data_dirs = {
            'raw_pdfs': Path(config.RAW_PDFS_DIR),
            'parsed': Path(config.PARSED_DIR),
            'enriched': Path(config.ENRICHED_DIR),
            'scored': Path(config.SCORED_DIR),
        }

        for dir_path in self.data_dirs.values():
            dir_path.mkdir(parents=True, exist_ok=True)

        logger.info("Pipeline orchestrator initialized")

    def process_document(
        self,
        pdf_path: Path,
        progress_bar=None,
        force_parse: bool = False,
        force_enrich: bool = False,
        force_score: bool = True,
        run_missing_citations: bool = False,
        original_filename: Optional[str] = None,
        author_orcids: Optional[list] = None,
    ) -> Dict:
        """
        Complete end-to-end processing pipeline with per-stage force options.

        Args:
            pdf_path: Path to PDF file
            progress_bar: Streamlit progress bar object (optional)
            force_parse: If True, re-parse even if cached
            force_enrich: If True, re-enrich even if cached
            force_score: If True, re-score even if cached

        Returns:
            Dictionary with processing results
        """
        results = {
            'success': True,
            'document_id': None,
            'stages_completed': [],
            'stages_skipped': [],
            'file_paths': {},
            'errors': [],
            'timings': {}
        }

        total_start = time.perf_counter()

        try:
            # Stage 0: Register PDF
            if progress_bar:
                progress_bar.progress(0.05, text="Registering PDF in database...")

            stage_start = time.perf_counter()
            register_result = self._stage_register_pdf(pdf_path, original_filename=original_filename)
            merge_stage_result(results, register_result)
            results['timings']['register_pdf_sec'] = round(time.perf_counter() - stage_start, 2)
            logger.info(f"[TIMING] Register PDF: {results['timings']['register_pdf_sec']} sec")

            if not results['success']:
                results['timings']['total_pipeline_sec'] = round(time.perf_counter() - total_start, 2)
                return results

            document_id = results['document_id']
            status = self.db.get_document_status(document_id)

            force_parse = force_parse or not current_artifact(status.get('parsed_path'), 'extraction_metadata',
                EXTRACTION_VERSION, configuration_fingerprint(self.config, 'parsing'))
            # The expected version must be the one the *selected* enricher writes;
            # comparing against the OpenAlex-only constant re-ran every audit.
            from src.enrichers.phase4_enricher import expected_enrichment_version
            force_enrich = force_enrich or force_parse or not current_artifact(status.get('enriched_path'),
                'enrichment_metadata', expected_enrichment_version(self.config),
                configuration_fingerprint(self.config, 'enrichment')) \
                or interrupted_stage(status.get('enriched_path'), 'enrichment')
            force_score = force_score or force_enrich or not current_artifact(status.get('scored_path'),
                'scoring_metadata', SCORING_VERSION, configuration_fingerprint(self.config, 'scoring')) \
                or interrupted_stage(status.get('scored_path'), 'scoring')

            # Stage 1: Parsing
            parsing_start = time.perf_counter()
            if force_parse or not status['status_parsed']:
                logger.info(f"[Doc {document_id}] Stage 1: Parsing PDF with Gemini LLM...")
                parse_result = self._stage_parse(
                    document_id,
                    Path(results['file_paths']['pdf']),
                    force_reprocess=force_parse,
                    progress_bar=progress_bar
                )
                merge_stage_result(results, parse_result)

                if not parse_result['success']:
                    results['timings']['parsing_sec'] = round(time.perf_counter() - parsing_start, 2)
                    logger.info(f"[TIMING] Parsing stage failed after: {results['timings']['parsing_sec']} sec")
                    results['timings']['total_pipeline_sec'] = round(time.perf_counter() - total_start, 2)
                    return results

                parsed_path = self._normalize_path(parse_result['file_paths'].get('parsed'))
            else:
                logger.info(f"[Doc {document_id}] Stage 1: Skipped (already parsed)")
                results['stages_skipped'].append('parsing')
                parsed_path = self._normalize_path(status['parsed_path'])
                results['file_paths']['parsed'] = parsed_path

                if progress_bar:
                    progress_bar.progress(0.40, text="✓ Using cached parsed data...")

            results['timings']['parsing_sec'] = round(time.perf_counter() - parsing_start, 2)
            logger.info(f"[TIMING] Parsing stage: {results['timings']['parsing_sec']} sec")
            status = self.db.get_document_status(document_id)

            # Stage 2: Enrichment
            enrichment_start = time.perf_counter()

            if force_enrich or not status['status_enriched']:
                if not parsed_path:
                    raise RuntimeError("Parsed path is missing before enrichment stage.")

                logger.info(f"[Doc {document_id}] Stage 2: Enriching with OpenAlex...")

                if progress_bar:
                    progress_bar.progress(0.45, text="Starting metadata enrichment...")

                enrich_result = self._stage_enrich(
                    document_id,
                    parsed_path,
                    force_reprocess=force_enrich,
                    progress_bar=progress_bar
                )
                merge_stage_result(results, enrich_result)

                if not enrich_result['success']:
                    results['timings']['enrichment_sec'] = round(time.perf_counter() - enrichment_start, 2)
                    logger.info(f"[TIMING] Enrichment stage failed after: {results['timings']['enrichment_sec']} sec")
                    results['timings']['total_pipeline_sec'] = round(time.perf_counter() - total_start, 2)
                    return results
            else:
                logger.info(f"[Doc {document_id}] Stage 2: Skipped (already enriched)")
                results['stages_skipped'].append('enrichment')
                enriched_path = self._normalize_path(status['enriched_path'])
                results['file_paths']['enriched'] = enriched_path

                if progress_bar:
                    progress_bar.progress(0.75, text="✓ Using cached enriched data...")

            results['timings']['enrichment_sec'] = round(time.perf_counter() - enrichment_start, 2)
            logger.info(f"[TIMING] Enrichment stage: {results['timings']['enrichment_sec']} sec")

            status = self.db.get_document_status(document_id)

            # Stage 3: Scoring, Stage 4: Risk Detection, Stage 5: Missing Citations
            scoring_group_start = time.perf_counter()

            if force_score or not status['status_scored']:
                logger.info(f"[Doc {document_id}] Stage 3: Scoring (Embeddings + LLM)...")

                if progress_bar:
                    progress_bar.progress(0.80, text="Computing relevance scores...")

                score_result = self._stage_score(
                    document_id,
                    status['enriched_path'],
                    progress_bar=progress_bar
                )
                merge_stage_result(results, score_result)

                if not score_result['success']:
                    results['timings']['scoring_group_sec'] = round(time.perf_counter() - scoring_group_start, 2)
                    logger.info(f"[TIMING] Scoring group failed after: {results['timings']['scoring_group_sec']} sec")
                    results['timings']['total_pipeline_sec'] = round(time.perf_counter() - total_start, 2)
                    return results

                # Fetch the most up-to-date path after scoring completes
                status = self.db.get_document_status(document_id)
                scored_path = Path(self._normalize_path(status['scored_path']))



                # Stage 4: Risk & Self-Citation Detection
                if progress_bar:
                    progress_bar.progress(0.90, text="Applying Risk & Self-Citation Logic...")

                logger.info(f"[Doc {document_id}] Stage 4: Applying Risk Detection to {scored_path}")
                if not run_risk_detection(scored_path):
                    raise RuntimeError("Risk detection did not complete successfully")
                results['stages_completed'].append('risk_detection')

                # Stage 5: Missing Citations AI
                # Optional because this makes an extra LLM call and slows down normal audit.
                if run_missing_citations:
                    missing_start = time.perf_counter()

                    if progress_bar:
                        progress_bar.progress(
                            0.95, text="Generating unverified literature-search candidates..."
                        )

                    logger.info(f"[Doc {document_id}] Stage 5: Generating Missing Citations")
                    from src.features.citation_suggester import generate_missing_citations

                    if not generate_missing_citations(scored_path, self.config):
                        raise RuntimeError(
                            "Literature-search candidate generation did not complete successfully"
                        )
                    results['stages_completed'].append('literature_search_candidates')

                    results['timings']['missing_citations_sec'] = round(
                        time.perf_counter() - missing_start, 2
                    )

                    logger.info(
                        f"[TIMING] Missing citation generation: "
                        f"{results['timings']['missing_citations_sec']} sec"
                    )

                else:
                    logger.info(
                        f"[Doc {document_id}] Stage 5: Skipped Missing Citation Generation "
                        f"(disabled for faster audit)"
                    )

                    results['stages_skipped'].append('missing_citations')

                    if progress_bar:
                        progress_bar.progress(
                            0.95,
                            text="✓ Skipped missing citation suggestions (fast mode)"
                        )

            else:
                logger.info(f"[Doc {document_id}] Stage 3, 4 & 5: Skipped (already processed)")
                results['stages_skipped'].append('scoring')
                scored_path = self._normalize_path(status['scored_path'])
                results['file_paths']['scored'] = scored_path

                if progress_bar:
                    progress_bar.progress(0.95, text="✓ Using cached scored and analyzed data...")

            # Stage 5b: Evidence analysis — hybrid relevance, metadata consistency,
            # self-citation patterns and literature gaps. Deterministic and cheap,
            # so it runs for new and cached scoring alike.
            analysis_start = time.perf_counter()
            if progress_bar:
                progress_bar.progress(0.96, text="Analysing relevance, self-citation and literature gaps...")
            analysis_result = self._stage_analysis(scored_path, author_orcids=author_orcids)
            merge_stage_result(results, analysis_result)
            results['timings']['analysis_sec'] = round(time.perf_counter() - analysis_start, 2)

            # Stage 6: Reference Risk Summary
            # Run this for both newly scored and cached audits so older scored
            # files can receive the new summary without repeating LLM scoring.
            reference_risk_start = time.perf_counter()

            if progress_bar:
                progress_bar.progress(
                    0.98,
                    text="Generating reference risk summary..."
                )

            reference_risk_result = self._stage_reference_risk_summary(scored_path)
            merge_stage_result(results, reference_risk_result)

            results['timings']['reference_risk_summary_sec'] = round(
                time.perf_counter() - reference_risk_start,
                2
            )
            logger.info(
                f"[TIMING] Reference Risk Summary: "
                f"{results['timings']['reference_risk_summary_sec']} sec"
            )

            if not reference_risk_result['success']:
                results['timings']['scoring_group_sec'] = round(
                    time.perf_counter() - scoring_group_start,
                    2
                )
                results['timings']['total_pipeline_sec'] = round(
                    time.perf_counter() - total_start,
                    2
                )
                return results

            results['timings']['scoring_group_sec'] = round(time.perf_counter() - scoring_group_start, 2)
            logger.info(f"[TIMING] Scoring + risk + missing citation group: {results['timings']['scoring_group_sec']} sec")

            if self.trust_kernel is not None:
                from uuid import UUID

                receipt = self.trust_kernel.capture_legacy_scored_artifact(
                    UUID(results["trust"]["document_version_id"]), Path(scored_path)
                )
                results["trust_capture"] = receipt.model_dump(mode="json")
                results["stages_completed"].append("trust_capture")

            # Final success
            results['timings']['total_pipeline_sec'] = round(time.perf_counter() - total_start, 2)
            logger.info(f"[TIMING] Total Pipeline: {results['timings']['total_pipeline_sec']} sec")
            logger.info(f"[TIMING] Timing summary: {results['timings']}")

            if progress_bar:
                progress_bar.progress(1.0, text="Processing finished. Open Review to inspect evidence and unresolved items.")

            logger.info(f"[Doc {document_id}] [SUCCESS] Pipeline completed!")
        except Exception as e:
            logger.error(f"Pipeline failed: {e}", exc_info=True)
            results['success'] = False
            results['errors'].append(str(e))
            results['timings']['total_pipeline_sec'] = round(time.perf_counter() - total_start, 2)
            if progress_bar:
                progress_bar.progress(0.0, text=f"❌ Pipeline failed: {str(e)[:50]}...")

        return results

    def _normalize_path(self, path_str: Optional[str]) -> Optional[str]:
        """Normalize path string to use forward slashes and convert to Path object."""
        if not path_str:
            return None
        # Replace backslashes with forward slashes and create Path object
        normalized = Path(path_str.replace('\\', '/'))
        return str(normalized)

    def _stage_register_pdf(self, pdf_path: Path, original_filename=None) -> Dict:
        """Stage 0: Register PDF in database."""
        try:
            pdf_path = Path(pdf_path).resolve()
            original_filename = original_filename or pdf_path.name
            validate_pdf(
                pdf_path,
                max_bytes=getattr(self.config, 'MAX_PDF_BYTES', 50 * 1024 * 1024),
            )
            digest = sha256_file(pdf_path)
            raw_pdfs_dir = self.data_dirs['raw_pdfs'].resolve()
            dest_path = raw_pdfs_dir / content_addressed_pdf_name(pdf_path.name, digest)

            if not dest_path.exists():
                atomic_copy_file(pdf_path, dest_path)
                logger.info(f"Stored PDF as content-addressed artifact: {dest_path}")
            elif sha256_file(dest_path) != digest:
                raise RuntimeError(f"Stored PDF failed integrity check: {dest_path}")
            pdf_path = dest_path

            document_id, is_new = self.db.register_pdf(
                pdf_path, original_filename=original_filename
            )

            trust = None
            if self.trust_kernel is not None:
                trust = self.trust_kernel.ingest_pdf(
                    pdf_path, original_filename=original_filename
                ).model_dump(mode="json")

            return {
                'success': True,
                'document_id': document_id,
                'trust': trust,
                'is_new_document': is_new,
                'stages_completed': ['registration'],
                'file_paths': {'pdf': str(pdf_path)}
            }

        except Exception as e:
            logger.error(f"Failed to register PDF: {e}")
            return {
                'success': False,
                'errors': [f"Registration failed: {str(e)}"]

            }

    def _stage_parse(
        self,
        document_id: int,
        pdf_path: Path,
        force_reprocess: bool = False,
        progress_bar=None
    ) -> Dict:
        """Stage 1: Parse PDF with Gemini LLM."""
        try:
            # Import the self-contained extractor
            from src.extractors.gemini_extractor import GeminiExtractor

            extractor = GeminiExtractor(self.config)

            output_filename = f"{pdf_path.stem}_parsed.json"
            output_path = self.data_dirs['parsed'] / output_filename

            # Cache key for this PDF
            cache_key = ":".join(
                [
                    sha256_file(pdf_path),
                    EXTRACTION_VERSION,
                    configuration_fingerprint(self.config, "parsing"),
                    str(getattr(self.config, 'LLM_PROVIDER', 'google')),
                    str(
                        getattr(self.config, 'OPENAI_MODEL', '')
                        if getattr(self.config, 'LLM_PROVIDER', 'google') == 'openai'
                        else getattr(self.config, 'GOOGLE_MODEL', '')
                    ),
                ]
            )

            # Extract with caching support
            logger.info("Extracting text from PDF and parsing with Gemini LLM...")
            parsed_data = extractor.extract_with_cache(
                pdf_path=pdf_path,
                cache_key=cache_key,
                db_manager=self.db,
                progress_bar=progress_bar,
                force_reprocess=force_reprocess
            )

            parsed_data["extraction_metadata"]["configuration_fingerprint"] = configuration_fingerprint(self.config, "parsing")
            atomic_write_json(output_path, parsed_data)

            # Update database
            metadata = parsed_data.get('metadata', {})
            self.db.update_parsed_status(
                document_id=document_id,
                parsed_path=str(output_path),
                title=metadata.get('title'),
                authors=json.dumps(metadata.get('authors', [])),
                num_refs=len(parsed_data.get('references_list', []))
            )

            logger.info(f"[SUCCESS] Parsing completed: {output_path}")

            return {
                'success': True,
                'stages_completed': ['parsing'],
                'file_paths': {'parsed': str(output_path)}
            }

        except Exception as e:
            logger.error(f"Parsing failed: {e}", exc_info=not isinstance(e, ProviderFailure))
            self.db.log_error(document_id, 'parsing', str(e))
            if isinstance(e, ProviderFailure):
                return {'success': False, 'errors': [str(e)], 'provider_failure': {
                    'code': e.code, 'provider': e.provider, 'retryable': e.retryable,
                    'http_status': e.status_code, 'attempts': e.attempts}}
            return {'success': False, 'errors': [str(e)]}

    def _stage_enrich(
        self,
        document_id: int,
        parsed_path: str,
        force_reprocess: bool = False,
        progress_bar=None
    ) -> Dict:
        """Stage 2: Enrich with OpenAlex API."""
        try:
            # Phase 4 selects the multi-provider enricher when extra scholarly
            # indexes or full-text acquisition are enabled; otherwise this is
            # byte-for-byte the verified Phase 2 OpenAlex path.
            from src.enrichers.phase4_enricher import build_enricher

            enricher = build_enricher(self.config, self.db)

            # Normalize the path before using it
            parsed_path_normalized = self._normalize_path(parsed_path)
            parsed_path_obj = Path(parsed_path_normalized)

            with open(parsed_path_obj, 'r', encoding='utf-8') as f:
                parsed_data = json.load(f)

            output_filename = parsed_path_obj.name.replace('_parsed', '_enriched')
            output_path = self.data_dirs['enriched'] / output_filename

            # Enrich references (with force option)
            enriched_data = enricher.enrich_references(
                parsed_data,
                force=force_reprocess
            )

            # Update progress during enrichment (if we have reference count)
            num_refs = len(parsed_data.get('references_list', []))
            if progress_bar and num_refs > 0:
                # Show incremental progress (this is approximate)
                progress_bar.progress(
                    0.70,
                    text=f"Enriched metadata for {num_refs} references..."
                )

            enriched_data["enrichment_metadata"]["configuration_fingerprint"] = configuration_fingerprint(self.config, "enrichment")
            atomic_write_json(output_path, enriched_data)

            summary = enriched_data.get('enrichment_summary', {})
            self.db.update_enriched_status(
                document_id=document_id,
                enriched_path=str(output_path),
                success_count=summary.get('successfully_enriched', 0),
                total_count=summary.get('total_references', 0)
            )

            logger.info(f"[SUCCESS] Enrichment completed: {output_path}")

            return {
                'success': True,
                'stages_completed': ['enrichment'],
                'file_paths': {'enriched': str(output_path)},
                'metadata': {
                    'enriched_count': summary.get('successfully_enriched', 0)
                }
            }

        except Exception as e:
            logger.error(f"Enrichment failed: {e}", exc_info=True)
            self.db.log_error(document_id, 'enrichment', str(e))
            return {
                'success': False,
                'errors': [f"Enrichment failed: {str(e)}"]
            }

    def _stage_score(
        self,
        document_id: int,
        enriched_path: str,
        progress_bar=None
    ) -> Dict:
        """Stage 3: Score references (embeddings + LLM)."""
        try:
            from src.scorers.relevance_scorer import RelevanceScorer

            scorer = RelevanceScorer(self.config, self.db)

            # Normalize the path before using it
            enriched_path_normalized = self._normalize_path(enriched_path)
            enriched_path_obj = Path(enriched_path_normalized)

            with open(enriched_path_obj, 'r', encoding='utf-8') as f:
                enriched_data = json.load(f)

            output_filename = enriched_path_obj.name.replace('_enriched', '_scored')
            output_path = self.data_dirs['scored'] / output_filename

            # Score references
            if progress_bar:
                progress_bar.progress(0.85, text="Computing relevance scores with LLM...")

            scored_data = scorer.score_references(enriched_data)

            if progress_bar:
                progress_bar.progress(0.95, text="Finalizing audit report...")

            scored_data["scoring_metadata"]["configuration_fingerprint"] = configuration_fingerprint(self.config, "scoring")
            atomic_write_json(output_path, scored_data, encoder=NumpyEncoder)

            scoring_meta = scored_data.get('scoring_metadata', {})
            self.db.update_scored_status(
                document_id=document_id,
                scored_path=str(output_path),
                embedding_model=scoring_meta.get('embedding_model'),
                llm_model=scoring_meta.get('llm_model')
            )

            logger.info(f"[SUCCESS] Scoring completed: {output_path}")

            return {
                'success': True,
                'stages_completed': ['scoring'],
                'file_paths': {'scored': str(output_path)}
            }

        except Exception as e:
            logger.error(f"Scoring failed: {e}", exc_info=True)
            self.db.log_error(document_id, 'scoring', str(e))
            return {
                'success': False,
                'errors': [f"Scoring failed: {str(e)}"]
            }

    def _stage_analysis(self, scored_path, author_orcids=None) -> Dict:
        """Recompute the deterministic analysis layers on the scored artifact."""
        from src.evidence.analysis import analyze_artifact

        path = Path(self._normalize_path(str(scored_path)))
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            return {'success': False, 'errors': [f"Analysis could not read the scored artifact: {error}"]}
        if data.get('schema_version') != SCHEMA_VERSION:
            return {'success': True, 'stages_skipped': ['analysis']}

        gap_analyzer, enricher = None, None
        if getattr(self.config, 'ENABLE_GAP_ANALYSIS', True) and not getattr(self.config, 'OFFLINE_MODE', False):
            from src.enrichers.phase4_enricher import build_enricher
            from src.evidence.literature_gaps import LiteratureGapAnalyzer
            enricher = build_enricher(self.config, self.db)
            gap_analyzer = LiteratureGapAnalyzer(self.config, self.db, transport=enricher,
                                                 clients=getattr(enricher, 'clients', {}))
        analyzed = analyze_artifact(data, self.config, author_orcids=author_orcids,
                                    gap_analyzer=gap_analyzer)
        if enricher is not None and enricher.recorder.records:
            runs = analyzed.get('provider_runs', []) + enricher.recorder.records
            analyzed['provider_runs'] = list({call['id']: call for call in runs}.values())
        atomic_write_json(path, analyzed, encoder=NumpyEncoder)
        counts = analyzed['review']['counts']
        return {'success': True, 'stages_completed': ['analysis'],
                'analysis': {key: counts.get(key, 0) for key in (
                    'potentially_irrelevant', 'self_citations', 'metadata_inconsistencies',
                    'possibly_missing_works', 'verified_suggestions')}}

    def _stage_reference_risk_summary(self, scored_path) -> Dict:
        """
        Generate and persist the manuscript-level Reference Risk Summary.

        The summarizer operates on existing scoring and risk-detection results;
        it does not make an additional LLM call.
        """
        try:
            scored_path_obj = Path(self._normalize_path(str(scored_path)))

            if not scored_path_obj.exists():
                raise FileNotFoundError(
                    f"Scored audit file not found: {scored_path_obj}"
                )

            with open(scored_path_obj, 'r', encoding='utf-8') as f:
                scored_data = json.load(f)

            if scored_data.get("schema_version") == SCHEMA_VERSION:
                scored_data["review"] = build_review(scored_data)
                scored_data.pop("reference_risk_summary", None)
                atomic_write_json(scored_path_obj, scored_data, encoder=NumpyEncoder)
                return {"success": True, "stages_completed": ["evidence_review"],
                        "file_paths": {"scored": str(scored_path_obj)},
                        "processing_status": scored_data["review"]["processing_status"],
                        "review_counts": scored_data["review"]["counts"]}

            # Support the common list names used across CitePrism versions.
            candidate_keys = (
                'scored_citations',
                'scored_references',
                'citation_scores',
                'reference_scores',
                'results',
                'references',
                'enriched_references',
            )

            scored_items = None
            source_key = None

            for key in candidate_keys:
                value = scored_data.get(key)
                if isinstance(value, list):
                    scored_items = value
                    source_key = key
                    break

            # Some versions keep the audit list inside another results object.
            if scored_items is None:
                for parent_key in ('audit_results', 'scoring_results', 'data'):
                    parent = scored_data.get(parent_key)
                    if not isinstance(parent, dict):
                        continue

                    for key in candidate_keys:
                        value = parent.get(key)
                        if isinstance(value, list):
                            scored_items = value
                            source_key = f"{parent_key}.{key}"
                            break

                    if scored_items is not None:
                        break

            if scored_items is None:
                raise KeyError(
                    "Could not find a scored citation/reference list. "
                    f"Checked keys: {', '.join(candidate_keys)}"
                )

            summarizer = ReferenceRiskSummarizer(
                relevant_threshold=getattr(
                    self.config,
                    'RELEVANT_THRESHOLD',
                    70,
                ),
                borderline_threshold=getattr(
                    self.config,
                    'BORDERLINE_THRESHOLD',
                    40,
                ),
                old_reference_years=getattr(
                    self.config,
                    'OLD_REFERENCE_YEARS',
                    15,
                ),
            )

            reference_risk_summary = summarizer.summarize(
                scored_items=scored_items,
                current_year=datetime.now(timezone.utc).year,
            )

            # Record which structure was summarized for reproducibility.
            reference_risk_summary['source_list'] = source_key
            reference_risk_summary['generated_at'] = datetime.now(
                timezone.utc
            ).isoformat(timespec='seconds')

            scored_data['reference_risk_summary'] = reference_risk_summary

            atomic_write_json(scored_path_obj, scored_data, encoder=NumpyEncoder)

            logger.info(
                "[SUCCESS] Reference Risk Summary generated: "
                f"{reference_risk_summary.get('risk_level', 'Unknown')} risk "
                f"(score={reference_risk_summary.get('risk_score', 'n/a')})"
            )

            return {
                'success': True,
                'stages_completed': ['reference_risk_summary'],
                'file_paths': {'scored': str(scored_path_obj)},
                'reference_risk_summary': reference_risk_summary,
            }

        except Exception as e:
            logger.error(
                f"Reference Risk Summary failed: {e}",
                exc_info=True,
            )
            return {
                'success': False,
                'errors': [
                    f"Reference Risk Summary failed: {str(e)}"
                ],
            }

    def get_document_files(self, document_id: int) -> Dict[str, Optional[str]]:
        """Get all file paths for a document."""
        status = self.db.get_document_status(document_id)

        if not status:
            return {}

        # Normalize all paths before returning
        return {
            'pdf': self._normalize_path(status.get('pdf_path')),
            'parsed': self._normalize_path(status.get('parsed_path')),
            'enriched': self._normalize_path(status.get('enriched_path')),
            'scored': self._normalize_path(status.get('scored_path'))
        }

    def check_stage_completion(self, document_id: int) -> Dict[str, bool]:
        """Check which stages are completed for a document."""
        status = self.db.get_document_status(document_id)

        if not status:
            return {
                'parsed': False,
                'enriched': False,
                'scored': False
            }

        return {
            'parsed': bool(status['status_parsed']),
            'enriched': bool(status['status_enriched']),
            'scored': bool(status['status_scored'])
        }
