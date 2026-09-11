-- Preserve record bytes and hashes. Only scope printed ordinals by extraction
-- observation so a corrected parse can coexist with the original observation.
DO $$
DECLARE constraint_name TEXT;
BEGIN
    SELECT conname INTO STRICT constraint_name
    FROM pg_constraint
    WHERE conrelid = 'citeprism.citation_references'::regclass
      AND contype = 'u'
      AND pg_get_constraintdef(oid) = 'UNIQUE (tenant_id, document_version_id, ordinal)';
    EXECUTE format('ALTER TABLE citeprism.citation_references DROP CONSTRAINT %I', constraint_name);
END $$;
CREATE UNIQUE INDEX citation_reference_observation_ordinal
ON citeprism.citation_references (
    tenant_id, document_version_id,
    (COALESCE(bibliographic_metadata ->> 'extraction_revision', 'legacy')), ordinal
);
