-- CitePrism database schema export
-- Read-only sqlite_master DDL only; no table rows are included.

-- SOURCE: database/citeprism.db
-- table: api_cache (table: api_cache)
CREATE TABLE api_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cache_key TEXT NOT NULL UNIQUE,
                    api_type TEXT NOT NULL,
                    request_data TEXT,
                    response_data TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

-- table: documents (table: documents)
CREATE TABLE documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pdf_filename TEXT NOT NULL UNIQUE,
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
                );

-- table: processing_logs (table: processing_logs)
CREATE TABLE processing_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    error TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                );

-- table: sqlite_sequence (table: sqlite_sequence)
CREATE TABLE sqlite_sequence(name,seq);

