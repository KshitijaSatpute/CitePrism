"""
CitePrism Configuration
=======================
Centralized configuration for all pipeline components.
"""

import os
from pathlib import Path
from typing import Literal, Optional
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        """Allow non-LLM tooling to inspect configuration without dotenv."""
        return False

# Load environment variables
load_dotenv()

from config.phase4 import Phase4Settings
from config.trust import TrustSettings


class Config(Phase4Settings, TrustSettings):
    """Centralized configuration for CitePrism pipeline."""

    # ============================================================================
    # LLM CONFIGURATION
    # ============================================================================

    # LLM Provider selection
    LLM_PROVIDER: Literal["openai", "google"] = os.getenv("LLM_PROVIDER", "google")

    # API Keys
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
    GOOGLE_API_KEY: Optional[str] = os.getenv("GOOGLE_API_KEY")

    # Model names
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    GOOGLE_MODEL: str = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")

    # Token limits
    MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "32000"))

    # ============================================================================
    # HUGGINGFACE CONFIGURATION
    # ============================================================================

    HF_API_TOKEN: Optional[str] = os.getenv("HF_API_TOKEN")
    HF_MODEL: str = os.getenv("HF_MODEL", "meta-llama/Llama-3.1-8B-Instruct")
    HF_BATCH_SIZE: int = int(os.getenv("HF_BATCH_SIZE", "5"))

    # ============================================================================
    # EMBEDDING CONFIGURATION
    # ============================================================================

    # Embedding model for semantic similarity
    ENABLE_TOPIC_SIMILARITY: bool = os.getenv("ENABLE_TOPIC_SIMILARITY", "false").lower() == "true"
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

    # ============================================================================
    # API CONFIGURATION
    # ============================================================================

    # OpenAlex settings
    OPENALEX_API_KEY: Optional[str] = os.getenv("OPENALEX_API_KEY")
    OPENALEX_EMAIL: str = os.getenv("OPENALEX_EMAIL", "your.email@example.com")
    OPENALEX_RATE_LIMIT: float = float(os.getenv("OPENALEX_RATE_LIMIT", "0.1"))
    ENABLE_PUBLISHER_SCRAPING: bool = (
        os.getenv("ENABLE_PUBLISHER_SCRAPING", "false").lower() == "true"
    )

    # Elsevier settings (used for enrichment)
    ELSEVIER_API_KEY: Optional[str] = os.getenv("ELSEVIER_API_KEY")

    # Springer settings (used for enrichment)
    SPRINGER_API_KEY: Optional[str] = os.getenv("SPRINGER_API_KEY")

    # ============================================================================
    # SCORING CONFIGURATION
    # ============================================================================

    # Hybrid score weights (must sum to 1.0)
    EMBEDDING_WEIGHT: float = float(os.getenv("EMBEDDING_WEIGHT", "0.4"))
    LLM_WEIGHT: float = float(os.getenv("LLM_WEIGHT", "0.6"))

    # Relevance thresholds
    RELEVANT_THRESHOLD: int = int(os.getenv("RELEVANT_THRESHOLD", "70"))
    BORDERLINE_THRESHOLD: int = int(os.getenv("BORDERLINE_THRESHOLD", "40"))

    # Fuzzy matching thresholds
    AUTHOR_SIMILARITY_THRESHOLD: int = int(os.getenv("AUTHOR_SIMILARITY_THRESHOLD", "85"))
    TITLE_SIMILARITY_THRESHOLD: float = float(os.getenv("TITLE_SIMILARITY_THRESHOLD", "0.7"))

    # ============================================================================
    # FILE PATHS
    # ============================================================================

    # Base directories
    BASE_DIR: Path = Path(os.getenv("CITEPRISM_BASE_DIR", str(Path(__file__).parent.parent))).resolve()
    DATA_DIR: Path = BASE_DIR / "data"

    # Data subdirectories
    RAW_PDFS_DIR: Path = DATA_DIR / "raw_pdfs"
    PARSED_DIR: Path = DATA_DIR / "parsed"
    ENRICHED_DIR: Path = DATA_DIR / "enriched"
    SCORED_DIR: Path = DATA_DIR / "scored"

    # System directories
    LOGS_DIR: Path = BASE_DIR / "logs"
    CACHE_DIR: Path = BASE_DIR / "cache"
    DATABASE_DIR: Path = BASE_DIR / "database"

    # Database file
    DATABASE_PATH: Path = DATABASE_DIR / "citeprism.db"

    # Phase 3 operational workspace. Evidence provenance remains in the trust
    # repository; this database owns queues and human-review workflow state.
    WORKSPACE_DATABASE_PATH: Path = Path(
        os.getenv("CITEPRISM_WORKSPACE_DATABASE", str(DATABASE_DIR / "workspace.db"))
    ).resolve()
    WORKSPACE_JOB_WORKERS: int = int(os.getenv("CITEPRISM_JOB_WORKERS", "2"))
    WORKSPACE_AUTH_MODE: str = os.getenv("CITEPRISM_AUTH_MODE", "local")
    WORKSPACE_PROXY_SECRET: Optional[str] = os.getenv("CITEPRISM_PROXY_SECRET")
    WORKSPACE_CORS_ORIGINS: str = os.getenv(
        "CITEPRISM_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
    )

    # ============================================================================
    # LOGGING CONFIGURATION
    # ============================================================================

    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "DEBUG")
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # ============================================================================
    # PROCESSING OPTIONS
    # ============================================================================

    # PDF extraction method
    PDF_EXTRACTOR: Literal["pypdf", "pdfminer"] = "pypdf"

    # Enable caching
    ENABLE_CACHE: bool = os.getenv("ENABLE_CACHE", "true").lower() == "true"

    # Retry settings
    API_RETRY_COUNT: int = int(os.getenv("API_RETRY_COUNT", "3"))
    API_RETRY_DELAY: float = float(os.getenv("API_RETRY_DELAY", "1.0"))

    # ============================================================================
    # VALIDATION
    # ============================================================================

    @classmethod
    def validate(cls):
        """Validate configuration settings."""
        errors = []

        # Check API keys
        if cls.LLM_PROVIDER == "openai" and not cls.OPENAI_API_KEY:
            errors.append("OPENAI_API_KEY is required when using OpenAI")

        if cls.LLM_PROVIDER == "google" and not cls.GOOGLE_API_KEY:
            errors.append("GOOGLE_API_KEY is required when using Google")

        # Check weights sum to 1.0
        if abs((cls.EMBEDDING_WEIGHT + cls.LLM_WEIGHT) - 1.0) > 0.01:
            errors.append(f"EMBEDDING_WEIGHT and LLM_WEIGHT must sum to 1.0 "
                        f"(current: {cls.EMBEDDING_WEIGHT + cls.LLM_WEIGHT})")

        # Check thresholds
        if not (0 <= cls.BORDERLINE_THRESHOLD <= cls.RELEVANT_THRESHOLD <= 100):
            errors.append("Thresholds must be: 0 <= BORDERLINE <= RELEVANT <= 100")

        if not 1 <= cls.WORKSPACE_JOB_WORKERS <= 8:
            errors.append("CITEPRISM_JOB_WORKERS must be between 1 and 8")

        if cls.WORKSPACE_AUTH_MODE not in {"local", "trusted_proxy", "oidc"}:
            errors.append("CITEPRISM_AUTH_MODE must be local, trusted_proxy or oidc")

        if cls.WORKSPACE_AUTH_MODE == "trusted_proxy" and not cls.WORKSPACE_PROXY_SECRET:
            errors.append("CITEPRISM_PROXY_SECRET is required in trusted_proxy mode")

        errors.extend(cls.phase4_errors())

        if errors:
            raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

    @classmethod
    def ensure_directories(cls):
        """Ensure all required directories exist."""
        directories = [
            cls.DATA_DIR,
            cls.RAW_PDFS_DIR,
            cls.PARSED_DIR,
            cls.ENRICHED_DIR,
            cls.SCORED_DIR,
            cls.LOGS_DIR,
            cls.CACHE_DIR,
            cls.DATABASE_DIR,
            cls.DATA_DIR / "workspace_objects",
            cls.DATA_DIR / "evaluation",
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def print_config(cls):
        """Print current configuration (for debugging)."""
        print("=" * 80)
        print("CitePrism Configuration")
        print("=" * 80)
        print(f"LLM Provider: {cls.LLM_PROVIDER}")
        print(f"LLM Model: {cls.GOOGLE_MODEL if cls.LLM_PROVIDER == 'google' else cls.OPENAI_MODEL}")
        print(f"Embedding Model: {cls.EMBEDDING_MODEL}")
        print(f"Score Weights: {cls.EMBEDDING_WEIGHT:.1f} (embed) + {cls.LLM_WEIGHT:.1f} (LLM)")
        print(f"Cache Enabled: {cls.ENABLE_CACHE}")
        print(f"Database: {cls.DATABASE_PATH}")
        print("=" * 80)


    # Upload guardrail for the prototype UI. Production deployments should
    # enforce the same limit at the reverse proxy and object-store boundary.
    MAX_PDF_BYTES: int = int(os.getenv("MAX_PDF_BYTES", str(50 * 1024 * 1024)))


# Entry points explicitly create runtime directories; imports never write data.
