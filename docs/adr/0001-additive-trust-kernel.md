# ADR 0001: Add an opt-in trust ledger before replacing the application

Status: implemented as an alpha foundation.

The Phase 0 Streamlit/SQLite system is usable and its regression suite passes.
Replacing the UI, persistence and scoring together would make failure attribution
hard. This phase adds a ledger through a small service boundary while preserving
the default workflow.

Decisions: immutable typed provenance records; PostgreSQL instead of a JSON-only
generic store; source/artifact bytes in content-addressed storage; explicit
unknowns rather than invented confidence; per-tenant append transactions and audit
chains; separate migration/runtime privileges. Local and S3 adapters share the
same write contract. The in-memory repository is for tests only.

Consequences: two persistence systems exist temporarily. The ledger is not the
user-facing read model. A future outbox is needed for durable cross-system jobs.
Hash chains require independently retained anchors against privileged rewrites.
Reference identity must acquire extraction-run scope before re-parsing workflows.
The legacy importer does not backfill verified source spans or validated claims.
An API/UI rewrite is deferred until the evidence contracts and evaluation gates
are exercised with real data.
