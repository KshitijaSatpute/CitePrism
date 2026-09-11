-- CitePrism Phase 1. PostgreSQL 16+. Immutable typed records, forced RLS.
-- Apply only through python -m src.trust.cli migrate.
CREATE SCHEMA IF NOT EXISTS citeprism;
CREATE TABLE citeprism.tenants (id UUID PRIMARY KEY);

CREATE TABLE citeprism.documents (
    tenant_id UUID NOT NULL REFERENCES citeprism.tenants(id),
    id UUID NOT NULL,
    record_version SMALLINT NOT NULL CHECK (record_version = 1),
    record_hash TEXT NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    title TEXT CHECK (length(title) BETWEEN 1 AND 1000),
    external_key TEXT CHECK (length(external_key) BETWEEN 1 AND 500),
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL CHECK (length(created_by) BETWEEN 1 AND 200),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, external_key)
);

CREATE TABLE citeprism.document_versions (
    tenant_id UUID NOT NULL REFERENCES citeprism.tenants(id),
    id UUID NOT NULL,
    record_version SMALLINT NOT NULL CHECK (record_version = 1),
    record_hash TEXT NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    document_id UUID NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number >= 1),
    parent_version_id UUID,
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    object_backend TEXT NOT NULL CHECK (object_backend IN ('local','s3')),
    object_key TEXT NOT NULL CHECK (length(object_key) BETWEEN 1 AND 1500),
    object_version_id TEXT CHECK (length(object_version_id) BETWEEN 1 AND 1000),
    original_filename TEXT NOT NULL CHECK (length(original_filename) BETWEEN 1 AND 500),
    media_type TEXT NOT NULL CHECK (media_type = 'application/pdf'),
    size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL CHECK (length(created_by) BETWEEN 1 AND 200),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, content_sha256),
    UNIQUE (tenant_id, document_id, id),
    UNIQUE (tenant_id, document_id, version_number),
    FOREIGN KEY (tenant_id, document_id) REFERENCES citeprism.documents (tenant_id, id),
    FOREIGN KEY (tenant_id, document_id, parent_version_id) REFERENCES citeprism.document_versions (tenant_id, document_id, id),
    CHECK ((version_number = 1 AND parent_version_id IS NULL) OR (version_number > 1 AND parent_version_id IS NOT NULL))
);

CREATE TABLE citeprism.source_spans (
    tenant_id UUID NOT NULL REFERENCES citeprism.tenants(id),
    id UUID NOT NULL,
    record_version SMALLINT NOT NULL CHECK (record_version = 1),
    record_hash TEXT NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    document_version_id UUID NOT NULL,
    page_start INTEGER CHECK (page_start >= 1),
    page_end INTEGER CHECK (page_end >= page_start),
    char_start INTEGER CHECK (char_start >= 0),
    char_end INTEGER CHECK (char_end > char_start),
    text_sha256 TEXT NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    locator JSONB NOT NULL CHECK (jsonb_typeof(locator) = 'object'),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, document_version_id, id),
    FOREIGN KEY (tenant_id, document_version_id) REFERENCES citeprism.document_versions (tenant_id, id),
    CHECK ((page_start IS NULL) = (page_end IS NULL)),
    CHECK ((char_start IS NULL) = (char_end IS NULL)),
    CHECK (page_start IS NOT NULL OR char_start IS NOT NULL)
);

CREATE TABLE citeprism.citation_references (
    tenant_id UUID NOT NULL REFERENCES citeprism.tenants(id),
    id UUID NOT NULL,
    record_version SMALLINT NOT NULL CHECK (record_version = 1),
    record_hash TEXT NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    document_version_id UUID NOT NULL,
    reference_key TEXT NOT NULL CHECK (length(reference_key) BETWEEN 1 AND 300),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    source_span_id UUID,
    raw_text_sha256 TEXT NOT NULL CHECK (raw_text_sha256 ~ '^[0-9a-f]{64}$'),
    bibliographic_metadata JSONB NOT NULL CHECK (jsonb_typeof(bibliographic_metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, document_version_id, id),
    FOREIGN KEY (tenant_id, document_version_id) REFERENCES citeprism.document_versions (tenant_id, id),
    UNIQUE (tenant_id, document_version_id, reference_key),
    UNIQUE (tenant_id, document_version_id, ordinal),
    FOREIGN KEY (tenant_id, document_version_id, source_span_id) REFERENCES citeprism.source_spans (tenant_id, document_version_id, id)
);

CREATE TABLE citeprism.model_runs (
    tenant_id UUID NOT NULL REFERENCES citeprism.tenants(id),
    id UUID NOT NULL,
    record_version SMALLINT NOT NULL CHECK (record_version = 1),
    record_hash TEXT NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    document_version_id UUID NOT NULL,
    purpose TEXT NOT NULL CHECK (length(purpose) BETWEEN 1 AND 200),
    provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 100),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 300),
    prompt_version TEXT NOT NULL CHECK (length(prompt_version) BETWEEN 1 AND 200),
    method_version TEXT NOT NULL CHECK (length(method_version) BETWEEN 1 AND 200),
    input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    output_sha256 TEXT CHECK (output_sha256 ~ '^[0-9a-f]{64}$'),
    configuration JSONB NOT NULL CHECK (jsonb_typeof(configuration) = 'object'),
    status TEXT NOT NULL CHECK (status IN ('started','succeeded','failed')),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    error_code TEXT CHECK (length(error_code) BETWEEN 1 AND 200),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, document_version_id, id),
    FOREIGN KEY (tenant_id, document_version_id) REFERENCES citeprism.document_versions (tenant_id, id),
    CHECK (completed_at IS NULL OR completed_at >= started_at),
    CHECK ((status = 'started' AND completed_at IS NULL AND output_sha256 IS NULL AND error_code IS NULL) OR (status = 'succeeded' AND completed_at IS NOT NULL AND output_sha256 IS NOT NULL AND error_code IS NULL) OR (status = 'failed' AND completed_at IS NOT NULL AND error_code IS NOT NULL))
);

CREATE TABLE citeprism.candidate_matches (
    tenant_id UUID NOT NULL REFERENCES citeprism.tenants(id),
    id UUID NOT NULL,
    record_version SMALLINT NOT NULL CHECK (record_version = 1),
    record_hash TEXT NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    document_version_id UUID NOT NULL,
    reference_id UUID NOT NULL,
    model_run_id UUID,
    provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 100),
    external_id TEXT NOT NULL CHECK (length(external_id) BETWEEN 1 AND 1000),
    match_method TEXT NOT NULL CHECK (length(match_method) BETWEEN 1 AND 200),
    confidence DOUBLE PRECISION CHECK (confidence BETWEEN 0 AND 1),
    selected BOOLEAN NOT NULL,
    metadata JSONB NOT NULL CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, document_version_id, id),
    FOREIGN KEY (tenant_id, document_version_id) REFERENCES citeprism.document_versions (tenant_id, id),
    UNIQUE NULLS NOT DISTINCT (tenant_id, reference_id, model_run_id, provider, external_id),
    FOREIGN KEY (tenant_id, document_version_id, reference_id) REFERENCES citeprism.citation_references (tenant_id, document_version_id, id),
    FOREIGN KEY (tenant_id, document_version_id, model_run_id) REFERENCES citeprism.model_runs (tenant_id, document_version_id, id)
);

CREATE TABLE citeprism.findings (
    tenant_id UUID NOT NULL REFERENCES citeprism.tenants(id),
    id UUID NOT NULL,
    record_version SMALLINT NOT NULL CHECK (record_version = 1),
    record_hash TEXT NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    document_version_id UUID NOT NULL,
    reference_id UUID,
    model_run_id UUID,
    category TEXT NOT NULL CHECK (length(category) BETWEEN 1 AND 200),
    priority TEXT NOT NULL CHECK (priority IN ('informational','low','moderate','high')),
    status TEXT NOT NULL CHECK (status IN ('open','confirmed','dismissed','superseded')),
    summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 2000),
    details JSONB NOT NULL CHECK (jsonb_typeof(details) = 'object'),
    policy_version TEXT NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 200),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, document_version_id, id),
    FOREIGN KEY (tenant_id, document_version_id) REFERENCES citeprism.document_versions (tenant_id, id),
    FOREIGN KEY (tenant_id, document_version_id, reference_id) REFERENCES citeprism.citation_references (tenant_id, document_version_id, id),
    FOREIGN KEY (tenant_id, document_version_id, model_run_id) REFERENCES citeprism.model_runs (tenant_id, document_version_id, id)
);

CREATE TABLE citeprism.evidence (
    tenant_id UUID NOT NULL REFERENCES citeprism.tenants(id),
    id UUID NOT NULL,
    record_version SMALLINT NOT NULL CHECK (record_version = 1),
    record_hash TEXT NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    document_version_id UUID NOT NULL,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('document_version','source_span','reference','candidate_match','model_run','finding')),
    subject_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('source_quote','metadata_match','score_component','model_rationale','author_overlap','policy_check')),
    source_span_id UUID,
    candidate_match_id UUID,
    model_run_id UUID,
    statement TEXT NOT NULL CHECK (length(statement) BETWEEN 1 AND 4000),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, document_version_id, id),
    FOREIGN KEY (tenant_id, document_version_id) REFERENCES citeprism.document_versions (tenant_id, id),
    FOREIGN KEY (tenant_id, document_version_id, source_span_id) REFERENCES citeprism.source_spans (tenant_id, document_version_id, id),
    FOREIGN KEY (tenant_id, document_version_id, candidate_match_id) REFERENCES citeprism.candidate_matches (tenant_id, document_version_id, id),
    FOREIGN KEY (tenant_id, document_version_id, model_run_id) REFERENCES citeprism.model_runs (tenant_id, document_version_id, id)
);

CREATE TABLE citeprism.reviewer_decisions (
    tenant_id UUID NOT NULL REFERENCES citeprism.tenants(id),
    id UUID NOT NULL,
    record_version SMALLINT NOT NULL CHECK (record_version = 1),
    record_hash TEXT NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    finding_id UUID NOT NULL,
    reviewer_id TEXT NOT NULL CHECK (length(reviewer_id) BETWEEN 1 AND 200),
    disposition TEXT NOT NULL CHECK (disposition IN ('confirm','dismiss','escalate','needs_more_evidence')),
    rationale TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 4000),
    supersedes_decision_id UUID,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, finding_id, id),
    UNIQUE (tenant_id, supersedes_decision_id),
    FOREIGN KEY (tenant_id, finding_id) REFERENCES citeprism.findings (tenant_id, id),
    FOREIGN KEY (tenant_id, finding_id, supersedes_decision_id) REFERENCES citeprism.reviewer_decisions (tenant_id, finding_id, id)
);

CREATE TABLE citeprism.audit_events (
    tenant_id UUID NOT NULL REFERENCES citeprism.tenants(id),
    id UUID NOT NULL,
    record_version SMALLINT NOT NULL CHECK (record_version = 1),
    sequence_number BIGINT NOT NULL CHECK (sequence_number >= 1),
    occurred_at TIMESTAMPTZ NOT NULL,
    actor JSONB NOT NULL CHECK (
        jsonb_typeof(actor) = 'object'
        AND actor ? 'actor_type' AND actor ? 'actor_id'
        AND actor->>'actor_type' IN ('human','service','model')
        AND length(actor->>'actor_id') BETWEEN 1 AND 200
    ),
    action TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id UUID NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    previous_hash TEXT NOT NULL CHECK (previous_hash ~ '^[0-9a-f]{64}$'),
    event_hash TEXT NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, sequence_number),
    UNIQUE (tenant_id, aggregate_type, aggregate_id)
);

CREATE FUNCTION citeprism.reject_mutation() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Immutable trust records reject %', TG_OP USING ERRCODE = '55000';
END;
$$;

CREATE FUNCTION citeprism.validate_version_lineage() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    latest citeprism.document_versions%ROWTYPE;
BEGIN
    -- Serialize direct SQL writers on the same tenant just like repository writes.
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text, 0));
    SELECT * INTO latest FROM citeprism.document_versions
    WHERE tenant_id = NEW.tenant_id AND document_id = NEW.document_id
    ORDER BY version_number DESC LIMIT 1;
    IF latest.id IS NULL THEN
        IF NEW.version_number <> 1 OR NEW.parent_version_id IS NOT NULL THEN
            RAISE EXCEPTION 'Version chain must start at 1' USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.version_number <> latest.version_number + 1
       OR NEW.parent_version_id IS DISTINCT FROM latest.id THEN
        RAISE EXCEPTION 'Version must directly follow latest parent' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER version_lineage BEFORE INSERT ON citeprism.document_versions
FOR EACH ROW EXECUTE FUNCTION citeprism.validate_version_lineage();

CREATE FUNCTION citeprism.validate_evidence_subject() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    target_table TEXT;
    found_subject BOOLEAN;
BEGIN
    target_table := CASE NEW.subject_type
        WHEN 'document_version' THEN 'document_versions'
        WHEN 'source_span' THEN 'source_spans'
        WHEN 'reference' THEN 'citation_references'
        WHEN 'candidate_match' THEN 'candidate_matches'
        WHEN 'model_run' THEN 'model_runs'
        WHEN 'finding' THEN 'findings'
        ELSE NULL END;
    IF target_table IS NULL THEN
        RAISE EXCEPTION 'Unknown evidence subject' USING ERRCODE = '23503';
    END IF;
    IF NEW.subject_type = 'document_version' THEN
        SELECT EXISTS(SELECT 1 FROM citeprism.document_versions
            WHERE tenant_id = NEW.tenant_id AND id = NEW.subject_id
              AND id = NEW.document_version_id) INTO found_subject;
    ELSE
        EXECUTE format('SELECT EXISTS(SELECT 1 FROM citeprism.%I
            WHERE tenant_id = $1 AND id = $2 AND document_version_id = $3)', target_table)
            INTO found_subject USING NEW.tenant_id, NEW.subject_id, NEW.document_version_id;
    END IF;
    IF NOT found_subject THEN
        RAISE EXCEPTION 'Evidence subject belongs to another tenant/version or is missing'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER evidence_subject BEFORE INSERT ON citeprism.evidence
FOR EACH ROW EXECUTE FUNCTION citeprism.validate_evidence_subject();

DO $$
DECLARE
    table_name TEXT;
    tenant_column TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'tenants', 'documents', 'document_versions', 'source_spans',
        'citation_references', 'model_runs', 'candidate_matches',
        'findings', 'evidence', 'reviewer_decisions', 'audit_events'
    ] LOOP
        EXECUTE format('CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE
            ON citeprism.%I FOR EACH STATEMENT EXECUTE FUNCTION citeprism.reject_mutation()', table_name);
        EXECUTE format('CREATE TRIGGER reject_truncate BEFORE TRUNCATE
            ON citeprism.%I FOR EACH STATEMENT EXECUTE FUNCTION citeprism.reject_mutation()', table_name);
        EXECUTE format('ALTER TABLE citeprism.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE citeprism.%I FORCE ROW LEVEL SECURITY', table_name);
        tenant_column := CASE WHEN table_name = 'tenants' THEN 'id' ELSE 'tenant_id' END;
        EXECUTE format('CREATE POLICY tenant_isolation ON citeprism.%I
            USING (%I = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)
            WITH CHECK (%I = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)',
            table_name, tenant_column, tenant_column);
    END LOOP;
END;
$$;

CREATE INDEX ix_evidence_subject ON citeprism.evidence (tenant_id, subject_type, subject_id);
CREATE INDEX ix_findings_version ON citeprism.findings (tenant_id, document_version_id);
CREATE INDEX ix_runs_version ON citeprism.model_runs (tenant_id, document_version_id);
CREATE INDEX ix_decisions_finding ON citeprism.reviewer_decisions (tenant_id, finding_id, created_at);
