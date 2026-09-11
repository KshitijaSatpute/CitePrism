# Phase 3 — human review workspace

Release: `4.0.0a1`. Phase 3 turns the verified citation-evidence engine into a usable review product. It does not change a model proposal into a scientific or misconduct verdict.

## Delivered product slice

| Capability | Phase 3 behavior |
|---|---|
| HTTP boundary | Versioned FastAPI routes under `/api/v1`, generated OpenAPI at `/api/openapi.json`, strict request contracts and stable error status codes. |
| Manuscript portfolio | Content-addressed uploads, duplicate reuse, existing Phase 2 manuscript import in local mode, audit history and current workload counts. |
| Audit execution | Bounded thread workers, atomic single-worker claim, persisted progress, cancellation intent, retry as a new audit, and interrupted-job recovery after restart. Phase 2 provider checkpoints supply stage-level resume behavior. |
| Review queue | Search and filter by priority/status, consequence-first sorting, reference context and clear system-vs-evidence language. |
| Evidence view | Side-by-side submitted bibliography/claim evidence and resolved source/quotation evidence. Source quotation grounding is explicitly separated from human interpretation. |
| Human decisions | Confirm, dismiss, escalate or request more evidence. Decisions require rationale, use idempotency keys, reject stale concurrent edits, and supersede instead of overwrite. |
| Corrections | Metadata, merge, split and reassignment proposal contracts. The current UI creates metadata proposals; every proposal preserves the submitted artifact. Acceptance/rejection is a separate event. |
| Activity | Tenant-scoped append-only workflow events with a SHA-256 hash chain and verification endpoint. The Phase 1 PostgreSQL trust ledger remains the evidence-provenance authority. |
| Web client | Strict TypeScript React client, responsive desktop/mobile layout, manuscript library, metrics, review drawer, evidence cards, activity timeline, focus states and keyboard-accessible native controls. |
| Deployment | Local Windows scripts, deterministic npm lock, multi-stage web image, non-root API image, read-only containers, dropped Linux capabilities, CSP and exact CORS origins. |

## Start on Windows

Keep the `.env` file from the successful Phase 2 run. From `C:\Users\keyur\Citeprism`:

```powershell
& .\scripts\setup_phase3.ps1
```

This creates `.venv-phase3`, installs the Python API/runtime packages, performs a clean npm install, and creates `.env` only if it does not already exist. Configure the Google/Hugging Face/OpenAlex values in `.env`, then verify:

```powershell
& .\scripts\verify_phase3.ps1 -Python .\.venv-phase3\Scripts\python.exe -WithPostgres
```

Start both applications:

```powershell
& .\scripts\start_phase3.ps1 -Python .\.venv-phase3\Scripts\python.exe
```

Open `http://127.0.0.1:5173`. The API schema is at `http://127.0.0.1:8000/api/docs`. Stop both with **Ctrl+C** in the terminal that ran the script.

Existing Phase 2 manuscripts are copied into the local workspace index on first start. Their PDFs and evidence artifacts are not modified. If an audit is interrupted, start the application again; the job returns to the queue and the Phase 2 checkpoints prevent completed provider steps from being repeated when their method/configuration fingerprints still match.

## Local workflow

1. Select an imported manuscript or choose **New manuscript**.
2. Run an audit. The progress banner remains visible while the background job works.
3. Open **Review queue** and start with high/moderate items.
4. Inspect the **Evidence** comparison before deciding. A located quote proves attribution to the stored source snapshot, not scientific correctness.
5. Record a disposition and rationale. A second edit creates a superseding decision; it never overwrites the first.
6. Use **Propose correction** for a bibliography change. This creates a proposal and never edits the submitted PDF or evidence artifact.
7. Inspect **Activity** and `/api/v1/activity/verification` before export or handoff.

## API surface

The core endpoints are:

- `POST /api/v1/manuscripts` — validated PDF upload.
- `GET /api/v1/manuscripts` — tenant portfolio.
- `POST /api/v1/manuscripts/{id}/audits` — queue a run.
- `GET /api/v1/jobs/{id}` — machine-readable state/progress.
- `POST /api/v1/jobs/{id}/cancel` and `/retry` — lifecycle commands.
- `GET /api/v1/audits/{id}/review` — the UI/export review projection plus current human state.
- `GET /api/v1/audits/{id}/references/{reference_id}` — scoped evidence comparison.
- `GET /api/v1/review-cases` — cross-manuscript queue with `priority`, `status`, `query`, and `limit` filters.
- `POST /api/v1/audits/{id}/findings/{finding_id}/decisions` — idempotent append-only review decision.
- `POST /api/v1/audits/{id}/corrections` — non-destructive proposal.
- `GET /api/v1/activity` and `/api/v1/activity/verification` — workflow history and hash-chain check.

Decision and correction POST requests require an `Idempotency-Key` header. Updating an existing finding decision also supplies its current decision ID; a stale browser receives HTTP 409 and must refresh.

## Authentication and tenant boundary

`CITEPRISM_AUTH_MODE=local` is intentionally convenient for one trusted developer workstation. It accepts local identity defaults and **must not** be internet-exposed.

Production-like environments set:

```text
CITEPRISM_AUTH_MODE=trusted_proxy
CITEPRISM_PROXY_SECRET=<secret-manager-value>
```

An identity-aware reverse proxy must authenticate the user, remove any inbound `X-CitePrism-*` headers, and inject verified tenant, actor, roles and the proxy secret. The API validates the secret, UUID tenant claim and role on every operation. Every repository read/write contains the tenant key; cross-tenant reads return not found.

This is an application control, not a government authorization. OIDC/JWT validation, enterprise directory integration, key rotation, centralized policy, SIEM export, retention/legal hold, malware scanning, accessibility certification, supply-chain attestations and an authorization package remain release gates.

## Deployment preview

After setting `.env`:

```powershell
docker compose -f compose.phase3.yml up --build
```

Open `http://127.0.0.1:8080`. Both published ports bind to loopback. The compose file is a hardened development preview, not a production topology. Before a release, resolve image tags to approved immutable digests and produce platform-specific hashed Python locks/SBOMs in CI.

## Acceptance gates

`verify_phase3.ps1` runs all Phase 2 evidence/trust regressions, the Phase 3 workflow/API tests, OpenAPI generation, clean npm installation, strict TypeScript checking and the Vite production build. With `-WithPostgres`, the Phase 1/2 live tenant and append-only constraints also run against disposable PostgreSQL 16.

Phase 3 offline tests cover duplicate upload reuse, tenant isolation, job single-claim/restart recovery, artifact publication, decision idempotency and optimistic concurrency, correction preservation, hash-chain tamper detection, endpoint contracts and cross-tenant HTTP denial.

## Honest boundary and Phase 4

Phase 3 is a coherent multi-user product skeleton, not a finished government product. The next large slice should replace process-local execution with a durable queue/service, make PostgreSQL the workspace store, add real OIDC/RBAC and team assignment, support full-text/OCR evidence, add reference merge/split interaction, build calibrated labeled evaluation datasets, and instrument latency/cost/quality SLOs. Procurement readiness later requires NIST control mapping, Section 508/WCAG evaluation, threat testing, disaster recovery evidence and target-agency authorization.
