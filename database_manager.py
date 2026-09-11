"""
CitePrism Database Manager
===========================
Tracks processing state, caching, and metadata using SQLite.
Prevents unnecessary API calls by maintaining state.
"""

import sqlite3
import hashlib
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from contextlib import contextmanager
import logging

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Manages SQLite database for CitePrism pipeline tracking.
    
    Tracks:
    - PDF files and their hash (detect duplicates)
    - Processing status (parsed, enriched, scored)
    - File locations for each stage
    - Timestamps and metadata
    """
    
    def __init__(self, db_path: str = "database/citeprism.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()
    
    def _init_database(self):
        """Initialize database schema."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            
            # Main documents table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pdf_filename TEXT NOT NULL UNIQUE,
                    original_filename TEXT,
                    pdf_hash TEXT NOT NULL UNIQUE,
                    title TEXT,
                    authors TEXT,
                    num_references INTEGER,
                    
                    -- Processing status
                    status_parsed BOOLEAN DEFAULT 0,
                    status_enriched BOOLEAN DEFAULT 0,
                    status_scored BOOLEAN DEFAULT 0,
                    
                    -- File locations
                    pdf_path TEXT NOT NULL,
                    parsed_path TEXT,
                    enriched_path TEXT,
                    scored_path TEXT,
                    
                    -- Timestamps
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    parsed_at TIMESTAMP,
                    enriched_at TIMESTAMP,
                    scored_at TIMESTAMP,
                    
                    -- Metadata
                    llm_model TEXT,
                    embedding_model TEXT,
                    processing_notes TEXT
                )
            """)

            existing_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(documents)").fetchall()
            }
            if "original_filename" not in existing_columns:
                cursor.execute("ALTER TABLE documents ADD COLUMN original_filename TEXT")
            
            # Processing logs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processing_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    error TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
                )
            """)
            
            # Cache table for API responses
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS api_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cache_key TEXT NOT NULL UNIQUE,
                    api_type TEXT NOT NULL,
                    request_data TEXT,
                    response_data TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            conn.commit()
            logger.info(f"Database initialized at {self.db_path}")
    
    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row  # Return rows as dictionaries
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()
    
    @staticmethod
    def compute_pdf_hash(pdf_path: Path) -> str:
        """Compute SHA256 hash of PDF file."""
        sha256_hash = hashlib.sha256()
        with open(pdf_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    
    def register_pdf(
        self, pdf_path: Path, original_filename: Optional[str] = None
    ) -> Tuple[int, bool]:
        """
        Register a new PDF or retrieve existing entry.
        
        Returns:
            Tuple of (document_id, is_new)
        """
        pdf_hash = self.compute_pdf_hash(pdf_path)
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Check if PDF already exists (by hash)
            cursor.execute(
                "SELECT id FROM documents WHERE pdf_hash = ?",
                (pdf_hash,)
            )
            existing = cursor.fetchone()
            
            if existing:
                doc_id = existing['id']
                logger.info(f"PDF already exists in database (ID: {doc_id})")
                return doc_id, False
            
            # Insert new document
            cursor.execute("""
                INSERT INTO documents (
                    pdf_filename, original_filename, pdf_hash, pdf_path
                )
                VALUES (?, ?, ?, ?)
            """, (pdf_path.name, original_filename or pdf_path.name, pdf_hash, str(pdf_path)))
            
            doc_id = cursor.lastrowid
            conn.commit()
            
            logger.info(f"New PDF registered (ID: {doc_id})")
            return doc_id, True
    
    def get_document_status(self, document_id: int) -> Optional[Dict]:
        """Get processing status of a document."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM documents WHERE id = ?",
                (document_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def update_parsed_status(self, document_id: int, parsed_path: Path,
                            title: str = None, authors: str = None,
                            num_refs: int = None):
        """Mark document as parsed and store metadata."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE documents
                SET status_parsed = 1,
                    parsed_path = ?,
                    parsed_at = CURRENT_TIMESTAMP,
                    title = COALESCE(?, title),
                    authors = COALESCE(?, authors),
                    num_references = COALESCE(?, num_references)
                WHERE id = ?
            """, (str(parsed_path), title, authors, num_refs, document_id))
            conn.commit()
            
            self._log_processing(document_id, "parsing", "success", 
                               f"Parsed successfully. Refs: {num_refs}")
    
    def update_enriched_status(self, document_id: int, enriched_path: Path,
                              success_count: int = 0, total_count: int = 0):
        """Mark document as enriched."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE documents
                SET status_enriched = 1,
                    enriched_path = ?,
                    enriched_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (str(enriched_path), document_id))
            conn.commit()
            
            self._log_processing(
                document_id, "enrichment", "success",
                f"Enriched {success_count}/{total_count} references"
            )
    
    def update_scored_status(self, document_id: int, scored_path: Path,
                            embedding_model: str = None, llm_model: str = None):
        """Mark document as scored."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE documents
                SET status_scored = 1,
                    scored_path = ?,
                    scored_at = CURRENT_TIMESTAMP,
                    embedding_model = COALESCE(?, embedding_model),
                    llm_model = COALESCE(?, llm_model)
                WHERE id = ?
            """, (str(scored_path), embedding_model, llm_model, document_id))
            conn.commit()
            
            self._log_processing(document_id, "scoring", "success",
                               "Scoring completed")
    
    def _log_processing(self, document_id: int, stage: str, status: str,
                       message: str = None, error: str = None):
        """Log processing event."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO processing_logs (document_id, stage, status, message, error)
                VALUES (?, ?, ?, ?, ?)
            """, (document_id, stage, status, message, error))
            conn.commit()
    
    def log_error(self, document_id: int, stage: str, error: str):
        """Log processing error."""
        self._log_processing(document_id, stage, "failed", error=error)
    
    def get_processing_logs(self, document_id: int) -> List[Dict]:
        """Get all processing logs for a document."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM processing_logs
                WHERE document_id = ?
                ORDER BY timestamp DESC
            """, (document_id,))
            return [dict(row) for row in cursor.fetchall()]
    
    def cache_api_response(self, api_type: str, request_key: str,
                          response_data: Dict):
        """Cache API response to avoid redundant calls."""
        cache_key = f"{api_type}:{request_key}"
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO api_cache (cache_key, api_type, request_data, response_data)
                VALUES (?, ?, ?, ?)
            """, (cache_key, api_type, request_key, json.dumps(response_data)))
            conn.commit()
            
            logger.debug(f"Cached API response: {cache_key}")
    
    def get_cached_response(self, api_type: str, request_key: str) -> Optional[Dict]:
        """Retrieve cached API response."""
        cache_key = f"{api_type}:{request_key}"
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT response_data FROM api_cache
                WHERE cache_key = ?
            """, (cache_key,))
            row = cursor.fetchone()
            
            if row:
                # Update last accessed time
                cursor.execute("""
                    UPDATE api_cache
                    SET last_accessed = CURRENT_TIMESTAMP
                    WHERE cache_key = ?
                """, (cache_key,))
                conn.commit()
                
                logger.debug(f"Cache hit: {cache_key}")
                return json.loads(row['response_data'])
            
            logger.debug(f"Cache miss: {cache_key}")
            return None
    
    def list_all_documents(self) -> List[Dict]:
        """List all documents in database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, pdf_filename,
                       COALESCE(original_filename, pdf_filename) AS display_filename,
                       title, authors, num_references,
                       status_parsed, status_enriched, status_scored,
                       uploaded_at, parsed_at, enriched_at, scored_at
                FROM documents
                ORDER BY uploaded_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]
    
    def delete_document(self, document_id: int) -> bool:
        """Delete a record and its managed artifacts.

        Paths outside this project's data directory are never removed, even if
        a corrupted database row points to them.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT pdf_path, parsed_path, enriched_path, scored_path "
                "FROM documents WHERE id = ?",
                (document_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return False

            artifact_paths = [row[key] for key in row.keys() if row[key]]
            cursor.execute("DELETE FROM processing_logs WHERE document_id = ?", (document_id,))
            cursor.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            conn.commit()

        project_root = self.db_path.resolve().parent.parent
        data_root = (project_root / "data").resolve()
        for stored_path in artifact_paths:
            candidate = Path(stored_path)
            if not candidate.is_absolute():
                candidate = project_root / candidate
            candidate = candidate.resolve()
            if candidate.is_relative_to(data_root):
                candidate.unlink(missing_ok=True)
            else:
                logger.warning("Refused to delete unmanaged artifact path: %s", candidate)

        logger.info(f"Deleted document ID and managed artifacts: {document_id}")
        return True
