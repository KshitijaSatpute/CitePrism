# Phase 1: provenance and persistence foundation

This is an opt-in engineering foundation, not a production authorization or a
new citation-accuracy claim. Phase 0 remains the default application path.

## Delivered slice

- Frozen, extra-forbidden, versioned contracts for documents, revisions, spans,
  references, candidate matches, model/import runs, evidence, findings, decisions,
  and audit events. UUID identifiers and aware UTC timestamps.
- PostgreSQL 16+ typed schema, tenant-qualified relationships, immutable revisions,
  forced row-level security, append-only triggers, serialized checksum migrations,
  separate administrator and runtime roles, and runtime permission checks.
- SHA-256 content-addressed local storage; optional S3 create-only writes with
  checksums, encryption-algorithm checks and version IDs. No overwrite/delete API.
- Atomic entity-plus-audit transactions. Per-tenant audit chains bind immutable
  record digests. Verification reads a consistent repeatable-read snapshot.
- Original upload names retained; optional ledger receipts shown in Streamlit.
  The existing pipeline captures its final scored JSON as an immutable artifact.
- CLI commands, development PostgreSQL Compose file, offline/fake-storage tests,
  opt-in real PostgreSQL tests, buildable wheel, and guarded PowerShell verifier.

## Important semantics

The SQLite database remains the V2 operational index. PostgreSQL is an additive
ledger, not a completed SQLite migration. There is no distributed transaction
between SQLite, the object store and PostgreSQL. A failed operation can leave
an unused object or a V2 registration; trust rows and their audit events commit
together. When enabled, a trust failure makes the pipeline report failure;
it does not silently claim successful ledger capture. Retrying the same bytes
and scored artifact does not create duplicate trust records or events.

A document version identifies exact PDF bytes, not a filename. An optional
external key groups successive revisions. Identical bytes are deduplicated within
one tenant. A different key cannot claim bytes already registered to another key.
The first immutable title/name is retained on identical-byte retries.

The legacy importer records a **successful import**, not a successful observed
provider execution. Unknown model/prompt information remains `unrecorded` or
`legacy-unrecorded`. Missing match confidence stays null. Scores, flags and
rationales remain automated signals, never an accusation or proof of source
support. The exact input JSON snapshot is retained by content digest.

Legacy reference keys and parsed reference metadata are immutable within a PDF
version. Re-scoring unchanged references creates a new import run. Re-parsing the
same PDF with different reference metadata currently raises a conflict; Phase 2
must introduce extraction-run identity instead of overwriting reference history.
Changed order/ordinals also requires that future extraction-run model.

Review decisions append independently of findings; they do not mutate the
finding's original `open` status. The latest unsuperseded decision must be used
by a future read model. Parallel reviewers can make independent decisions; a
specific decision can be superseded only once. Authenticated reviewer identity
and a review UI are not implemented in this slice.

## Local setup

Use Python 3.11, 3.12 or 3.13. Do not reuse the copied virtual environment pointing
at another person's Python installation. For this phase's minimal tests/CLI:

```powershell
py -3.12 -m venv .venv-phase1
.\.venv-phase1\Scripts\python.exe -m pip install -r requirements-trust.txt
```

Check each command's exit code. The full app additionally needs its existing
dependencies; `requirements.in` is the current direct-dependency source. The old
`requirements.txt` remains the supplied environment snapshot, not a regenerated
Phase 1 lock. This release has no production hash-pinned dependency lock.

With Docker Desktop running, execute the verifier through your normally permitted
PowerShell script workflow (no execution-policy change is required by this release):

```powershell
& .\scripts\verify_phase1.ps1 -Python .\.venv-phase1\Scripts\python.exe -WithPostgres
```

It starts a localhost-only development database on port 55432, applies migrations
with an administrator, and uses a separate restricted runtime role for tests.
Published Compose credentials are development fixtures only. Tests append to
fresh UUID tenants and do not delete existing records. The database is left
running. `docker compose -f compose.trust.yml stop` stops it without deleting its
volume. Do not use `down -v` as a troubleshooting shortcut.

If an existing Compose volume lacks the role, inspect it and provision the role
deliberately; initialization scripts only run when PostgreSQL initializes an empty
data directory. Do not erase the volume to silence a migration error.

Without `-WithPostgres`, the deterministic tests run and real database tests visibly
skip unless both test DSNs are already set. A green run with skips is not database
acceptance. Supplied but invalid DSNs/drivers fail, rather than skip.

To enable integration after the live tests pass, add only the runtime settings
from `env.example` to your existing `.env`, set `TRUST_KERNEL_ENABLED=true`, and
restart Streamlit. Never replace your `.env` or run the app using admin credentials.
Configuration is read at process import; hot-changing the flag is not supported.

```powershell
.\.venv-phase1\Scripts\python.exe -m src.trust.cli ingest .\sample.pdf --original-filename sample.pdf
.\.venv-phase1\Scripts\python.exe -m src.trust.cli verify-audit
```

`ingest` prints the document version UUID. `capture <UUID> <scored.json>` imports an
existing final V2 scored artifact. Audit verification checks database entities and
events; it does not reread every stored PDF/JSON object. A future object scrubber
must verify all referenced object versions as a separate job.

## Deployment boundaries

Migration jobs alone receive `TRUST_MIGRATION_DATABASE_URL`; the application receives
only `TRUST_DATABASE_URL` and the expected runtime role. The migration runner does
not fall back to runtime credentials. Schema migrations take an advisory lock and
reject changes to already-applied SQL. Add a new numbered migration for evolution.

The runtime must be LOGIN, NOINHERIT, no memberships, no superuser/BYPASSRLS/role
creation/database creation/replication attributes, no trust-table ownership or
schema CREATE, and SELECT+INSERT only on the trust tables. The repository checks
these conditions on each connection. This costs round trips; there is no connection
pool or throughput benchmark yet. Writes serialize per tenant to sequence the
chain. Evaluate a transactional outbox/partition design before high-volume use.

`TRUST_TENANT_ID` is an operator-provided scope, **not authentication**. The runtime
role can set this transaction variable to any tenant. RLS helps prevent missing
tenant predicates; it does not isolate tenants against a compromised application
holding that role. Independent credentials/databases or an authenticated database
boundary are needed for hostile multi-tenant deployment. PostgreSQL documents
owner/superuser bypass and whole-table-operation limits in its
[row-security documentation](https://www.postgresql.org/docs/16/ddl-rowsecurity.html).

S3 requires a pre-provisioned versioned bucket by default. The SDK's standard
credential provider chain supplies credentials. Configure least-privilege IAM,
bucket policies, approved encryption keys, retention and backup separately. KMS
key identity/permissions are not fully attested by this adapter. Versioning is not
Object Lock. Conditional creates follow the SDK's
[PutObject contract](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html).
No live AWS verification has been performed by the release's offline test suite.

## Next acceptance gates

1. Run the supplied live PostgreSQL suite on Windows/Docker or CI with zero skips.
2. Run a real PDF through the full existing pipeline with the flag off, then on;
   compare outputs and verify its ledger chain. No external provider runs were
   performed during this release's offline verification.
3. Exercise real S3 versioning, approved encryption, IAM denial and recovery.
4. Phase 2: extraction-run identities, source-page anchors, claim/reference matching,
   abstention and a labelled evaluation set with false-positive/false-negative rates.
5. Then build the accessible review workspace, authenticated API, queues,
   observability, deployment hardening and workload-specific security evidence.
