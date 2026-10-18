# Phase 4 — enterprise runtime and evidence acquisition

Release: `5.0.0a1`. Phase 3 was a coherent product skeleton running in one
process on one workstation. Phase 4 makes it a system several people can operate
at once: durable state, a real queue, real identity, evidence acquisition that
handles the documents reviewers actually receive, and instrumentation that says
what it costs and how well it works.

Nothing here changes the epistemic claim. An automated proposal is still an
unreviewed model output, and a located quotation still proves attribution to a
stored snapshot rather than the truth of an interpretation.

![Phase 4 architecture](../architecture/phase4-architecture.svg)

## Delivered slice

| Capability | Phase 4 behaviour |
|---|---|
| Workspace persistence | PostgreSQL store with the same interface as the Phase 3 SQLite store. Organizations, audits, jobs, decisions, corrections, assignments, repairs, exports and telemetry, with database-level append-only triggers on the activity and decision tables. SQLite remains for one-workstation use and gained the same capabilities. |
| Durable queue | Lease-based claims with `FOR UPDATE SKIP LOCKED`, priority, retry backoff, heartbeat renewal, expiry reclamation and a dead-letter state. Workers are stateless and scale horizontally. |
| Object storage | Content-addressed manuscripts, evidence artifacts and export packages on local disk or any S3-compatible endpoint (AWS S3, MinIO, Ceph RGW). Digests are verified on read, and path traversal is rejected at key construction. |
| Identity | Three modes: `local`, `trusted_proxy` (unchanged from Phase 3) and `oidc`, where the API verifies bearer tokens against the issuer's published JWKS with an algorithm allow-list and bounded key refresh. |
| RBAC | Eighteen named permissions across six roles (`viewer`, `reviewer`, `lead_reviewer`, `operator`, `org_admin`, `administrator`). Each route names the permission it needs; no route hand-rolls a role check. |
| Organizations and assignment | Membership with directory-group mapping, live per-reviewer open-case counts, and assignment or release of individual review items. |
| Evidence acquisition | Scanned PDFs are detected and OCR'd rather than rejected. Open-access full text is retrieved from arXiv, PMC and Unpaywall where it exists, so "insufficient evidence" describes the paper rather than our retrieval. |
| Source resolution | OpenAlex, Crossref and Semantic Scholar are queried together, merged on DOI, and ranked by the existing rule-based resolver. Provider corroboration and disagreement are both recorded as evidence. |
| Automatic repair | The three system issues each map to a targeted re-run. A repair always creates a new audit and never edits a stored artifact. |
| Signed exports | PDF, HTML, JSON or a reproducible bundle, with an Ed25519 detached signature over a manifest listing every file digest. |
| Insight modules | Literature-search candidates and author overlap are exposed through the API and rendered in React, each carrying its own scope caveat. |
| Telemetry and SLOs | Per-stage latency, provider token counts and estimated cost, six service objectives and queue depth. |
| Evaluation | A labeled JSONL dataset format, precision/recall/F1 per label, triage precision and recall, and calibration measured with Brier score, expected calibration error and a reliability table. |

## Choosing a profile

**Single workstation (default).** SQLite, local object storage, an in-process
worker, `local` authentication. This is the Phase 3 experience with every Phase
4 feature available. Nothing needs to be installed beyond the Python packages.

**Durable runtime.** PostgreSQL, S3-compatible storage, separate worker
processes, `oidc` or `trusted_proxy` authentication. Set
`CITEPRISM_WORKSPACE_BACKEND=postgres` and `CITEPRISM_RUN_EMBEDDED_WORKER=false`.

The application code is identical in both. Only configuration differs.

## Start on Windows

```powershell
& .\scripts\setup_phase4.ps1 -GenerateSigningKey
```

This creates `.venv-phase4`, installs the Python and npm dependencies, copies
`env.example` to `.env` if it does not exist, and optionally generates an
Ed25519 export signing key under `secrets\`.

Verify before running anything:

```powershell
& .\scripts\verify_phase4.ps1 -Python .\.venv-phase4\Scripts\python.exe
```

Start the workspace:

```powershell
& .\scripts\start_phase4.ps1 -Python .\.venv-phase4\Scripts\python.exe
```

Open `http://127.0.0.1:5173`. The API schema is at
`http://127.0.0.1:8000/api/docs`.

## Moving to the durable runtime

Bring up PostgreSQL and MinIO, then apply the workspace schema:

```powershell
docker compose --project-directory . -f deploy/compose/compose.phase4.yml up -d workspace-db objects provision
```

In `.env`:

```text
CITEPRISM_WORKSPACE_BACKEND=postgres
CITEPRISM_WORKSPACE_DATABASE_URL=postgresql://citeprism_workspace_app:citeprism-workspace-dev-only@127.0.0.1:55433/citeprism_workspace
CITEPRISM_OBJECT_BACKEND=s3
CITEPRISM_S3_BUCKET=citeprism
CITEPRISM_S3_ENDPOINT_URL=http://127.0.0.1:9000
CITEPRISM_RUN_EMBEDDED_WORKER=false
```

Then run the API with two separate workers:

```powershell
& .\scripts\start_phase4.ps1 -Python .\.venv-phase4\Scripts\python.exe -Workers 2
```

Or run the whole topology in containers:

```powershell
docker compose --project-directory . -f deploy/compose/compose.phase4.yml up --build
docker compose --project-directory . -f deploy/compose/compose.phase4.yml up --scale worker=3
```

### Migrations

Migrations run under an administrator DSN and are checksum-guarded: editing an
applied migration is refused rather than silently reapplied. The runtime role
never runs migrations and receives least-privilege grants — `UPDATE` on
operational tables, `INSERT`/`SELECT` only on `activity`, `decisions` and
`telemetry_events`.

```powershell
python scripts\migrate_workspace.py `
    --database-url postgresql://citeprism_admin:...@127.0.0.1:55433/citeprism_workspace `
    --runtime-role citeprism_workspace_app
```

## The durable queue

A worker leases the highest-priority runnable job, renews the lease on a
heartbeat while the audit runs, and finishes or releases it. Three properties
follow, and each is covered by a test:

* **Single winner.** `FOR UPDATE SKIP LOCKED` means N workers on N machines
  never run the same audit twice. The SQLite store takes the write lock for the
  whole claim transaction to get the same guarantee.
* **Crash safety.** A worker that dies stops heart-beating. Its lease expires,
  the job returns to the queue with its attempt count intact, and it becomes a
  dead letter once the retry budget is spent rather than looping forever.
* **Failure triage.** A transient provider outage is retried with exponential
  backoff. A deterministic failure — an encrypted PDF, a malformed artifact —
  fails immediately, because retrying it three times only wastes the provider
  budget and the reviewer's time.

Inspect the queue at `GET /api/v1/queue`, or in the **Operations** tab.

## Identity and authorization

`local` accepts local identity defaults and **must not** be internet-exposed. It
now defaults to the `org_admin` role, because it describes one trusted developer
on one machine.

For OIDC:

```text
CITEPRISM_AUTH_MODE=oidc
CITEPRISM_OIDC_ISSUER=https://login.example.org/realms/citeprism
CITEPRISM_OIDC_AUDIENCE=citeprism-api
CITEPRISM_OIDC_TENANT_CLAIM=org_id
CITEPRISM_OIDC_ROLE_MAP=Editorial-Leads=lead_reviewer,Editorial-Staff=reviewer
```

The API verifies signature, issuer, audience, expiry and not-before on every
request. There is no unverified-decode path anywhere in the module, the `alg`
header is checked against an asymmetric allow-list before use, and an unknown
key identifier triggers at most one JWKS refetch per cooldown window so token
spraying cannot amplify requests against the identity provider.

An authenticated account whose directory groups map to nothing gets `viewer`:
it can read, and it can change nothing.

### Roles

| Role | Adds |
|---|---|
| `viewer` | Read manuscripts, review projections, exports and membership. |
| `reviewer` | Record decisions, propose corrections, run repairs, create exports. |
| `lead_reviewer` | Accept or reject corrections; assign work; run evaluations. |
| `operator` | Upload manuscripts; run and cancel audits. |
| `org_admin` | Manage membership, and everything both reviewer paths allow. |
| `administrator` | Every permission — within one tenant, never across tenants. |

Tenant isolation is enforced separately, by the tenant key on every repository
statement. A cross-tenant identifier resolves to "not found".

## Evidence acquisition

**OCR.** Every upload is assessed for an extractable text layer. A scan is
detected as `needs_ocr`; with `CITEPRISM_ENABLE_OCR=true` and tesseract
installed, pages are rasterized and recognized. OCR'd pages are labelled
`text_source: ocr` with a mean confidence, and the artifact records that
character offsets on those pages refer to recognized text, not to the
publisher's original layer. A quotation located in OCR output carries
recognition error and needs closer human checking — the data says so, not just
the documentation.

**Full text.** With `CITEPRISM_ENABLE_FULL_TEXT=true`, open-access locations
from Semantic Scholar, arXiv, PMC and Unpaywall are retrieved over HTTPS only,
size-capped, and truncated before any trailing reference list so a quotation
cannot be matched inside the cited paper's own bibliography. Licensed content is
never scraped. Where no open location exists, the snapshot stays an abstract and
is labelled `kind: abstract`.

**Resolution.** Candidates from all three indexes are merged on DOI, or on
normalized title and year when no DOI exists — a conservative rule, because a
false split costs one duplicate candidate while a false merge would hide a
genuine alternative. Provider agreement is recorded on each candidate as
evidence; it deliberately does not inflate the match strength, since three
indexes copying one upstream error is not three pieces of evidence.

## Repairing system issues

`build_review` marks a finding `system_issue=True` when the cause is CitePrism's
failure rather than anything about the manuscript. Each maps to a repair:

| System issue | Repair | Cost |
|---|---|---|
| `assessment_error` | Re-run assessment against stored snapshots | Cheapest — reuses parsing and enrichment |
| `identity_unresolved` (provider unavailable) | Re-resolve identity across every index | Moderate — re-runs enrichment |
| `bibliography_review` (linking failure) | Re-extract with the current matcher | Highest — re-runs the model parse |

Selecting several repairs at once forces the union of the required stages, so
three issues cost one audit rather than three. A repair that has already been
attempted twice stops offering itself automatically and asks for human review.

Every repair creates a new audit. The original audit, its artifact and its
decisions stay readable for comparison.

## Signed review packages

An export bundles `review.json`, `review.html`, `review.pdf` and a
`manifest.json` carrying each file's SHA-256 and a detached signature over the
manifest. Bundles use fixed timestamps, so exporting the same review twice
produces identical bytes and two packages can be compared directly.

Ed25519 is the default: verification needs only the public key, which can be
published. HMAC is available where key management is not possible, but it proves
integrity only to holders of the shared secret, and the manifest says so. If no
key is configured an ephemeral one is generated and the key id is suffixed
`-ephemeral`, because such a signature cannot attribute the package to a
deployment.

To verify a package:

1. Recompute each file's SHA-256 and compare with `manifest.json`.
2. Serialize the manifest without its `signature` field as compact JSON with
   sorted keys, and verify the signature over those bytes.

## Telemetry, SLOs and cost

Six objectives, measured over a configurable window:

| Objective | Default target |
|---|---|
| Audit execution, p95 | ≤ 900 s |
| Queue wait, p95 | ≤ 60 s |
| Audits completing without runtime failure | ≥ 98% |
| References resolved to a source identity | ≥ 85% |
| Findings caused by system issues | ≤ 5% |
| Provider cost per completed audit | ≤ $1.50 |

The system-issue ratio is the one to watch. A high value means reviewers are
spending their attention triaging our failures instead of citations.

Cost is estimated from recorded token counts against a documented price table
(`src/telemetry/pricing.py`), overridable with `CITEPRISM_PRICING_TABLE`. Every
figure carries the table version, so a number can always be traced to the rates
that produced it. It is an estimate, not an invoice.

Telemetry records stage names, durations, token counts and outcome codes. It
never records prompt text, response bodies or credentials, and a telemetry
failure never fails an audit.

## Evaluation and calibration

Datasets are JSON Lines, one line per annotated reference, small enough for a
domain expert to produce in a spreadsheet. Two annotators labelling the same
item is recorded rather than averaged away, and reported as Cohen's kappa.

```bash
python scripts/run_evaluation.py \
    --dataset data/evaluation/gold_set.jsonl \
    --artifacts data/scored \
    --output data/evaluation/report.json
```

The runner never re-runs the pipeline. It reads artifacts the pipeline already
produced and projects them through the same `build_review` the UI uses, so an
evaluation measures what reviewers actually see rather than an internal value
that never reaches the screen.

Calibration is the point. The resolver documents that rank strength is not a
calibrated probability; measuring its Brier score and expected calibration error
is how that claim gets tested instead of assumed. A system that says "resolved"
and is right 60% of the time is worse than useless to a reviewer who reads
"resolved" as certainty, and accuracy alone hides that.

`data/evaluation/example_gold_set.jsonl` shows the format. Replace it with real
annotations before drawing any conclusion.

## Acceptance gates

`verify_phase4.ps1` runs the entire Phase 3 gate first — Phase 4 must not break
it — then Phase 4 compilation, configuration validation, the four offline test
suites, OpenAPI contract generation, and the React clean install, strict
TypeScript check and production build. With `-WithPostgres` it also starts a
disposable PostgreSQL, applies the workspace migrations and runs the PostgreSQL
store tests.

Offline tests cover: single-winner leasing, lease expiry through requeue to dead
letter, heartbeat ownership, retry backoff, membership role normalization,
assignment workload and inactive-member refusal, repair planning and idempotence,
export signing and tamper detection, object-store digest verification and
traversal rejection, RBAC across every role and permission, all three
authentication modes, OCR classification, full-text scoping, multi-provider merge
and outage handling, insight caveats, and the classification and calibration
metrics.

## Honest boundary and what Phase 5 needs

Phase 4 is an operable multi-user system. It is not an authorized government
product, and this document is not a claim of compliance.

Still outstanding: NIST 800-53 control mapping and an authorization package;
Section 508 and WCAG 2.2 evaluation by an accessibility specialist; independent
threat modelling and penetration testing; disaster-recovery and
restore-from-backup evidence; SIEM export, retention schedules and legal hold;
key rotation procedures for signing and database credentials; malware scanning
on upload; supply-chain attestations with SBOMs and hashed, platform-specific
Python locks; and a real labelled evaluation corpus with enough annotators to
support a generalization claim. The example gold set in this repository supports
none.

Two limits are structural rather than schedule items. Abstract-only assessment
remains the fallback wherever open full text does not exist, which is most of
the licensed literature. And every automated output remains a proposal for a
person to weigh — that is a design commitment, not a gap to be closed.
