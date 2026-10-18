# CitePrism Deployment Guide

Docker Compose configurations for CitePrism development and testing.

Run all commands from the repository root using Docker Compose v2.

## Prerequisites

- Docker Engine and Docker Compose v2
- Configured .env file where required
- Available local service ports

Never commit credentials or private environment files.

## Configurations

- compose/compose.phase3.yml — API and React frontend
- compose/compose.phase4.yml — PostgreSQL, MinIO, API, worker and frontend
- compose/compose.trust.yml — Trust PostgreSQL database
- compose/compose.offline.yml — Ollama offline extension for Phase 4

## Configuration Validation

Run these commands from the repository root:

```powershell
docker compose --project-directory . -f deploy/compose/compose.phase3.yml config
docker compose --project-directory . -f deploy/compose/compose.phase4.yml config
docker compose --project-directory . -f deploy/compose/compose.trust.yml config
docker compose --project-directory . -f deploy/compose/compose.phase4.yml -f deploy/compose/compose.offline.yml config
```

These commands validate Compose configuration resolution but do not start containers or prove runtime correctness.

Docker validation is currently pending because Docker is unavailable locally.

## Starting and Stopping Services

Run these commands from the repository root.

### Phase 3

Start: `docker compose --project-directory . -f deploy/compose/compose.phase3.yml up --build`

Stop: `docker compose --project-directory . -f deploy/compose/compose.phase3.yml stop`

### Phase 4

Start: `docker compose --project-directory . -f deploy/compose/compose.phase4.yml up --build`

Stop: `docker compose --project-directory . -f deploy/compose/compose.phase4.yml stop`

### Trust PostgreSQL

Start: `docker compose --project-directory . -f deploy/compose/compose.trust.yml up -d --wait`

Stop: `docker compose --project-directory . -f deploy/compose/compose.trust.yml stop`

### Offline Mode

Download model on a connected host: `docker compose --project-directory . -f deploy/compose/compose.phase4.yml -f deploy/compose/compose.offline.yml run --rm ollama-pull`

Start: `docker compose --project-directory . -f deploy/compose/compose.phase4.yml -f deploy/compose/compose.offline.yml up --build`

Stop: `docker compose --project-directory . -f deploy/compose/compose.phase4.yml -f deploy/compose/compose.offline.yml stop`

Offline application settings do not enforce network isolation. Use host-level network restrictions for an air-gapped deployment.

## Persistent Storage

- `trust_pgdata`: Trust PostgreSQL data
- `workspace_pgdata`: Workspace PostgreSQL data
- `workspace_objects`: MinIO object storage
- `citeprism-data`: Application data
- `citeprism-database`: Application database files
- `ollama-models`: Downloaded Ollama models

Named volumes are scoped to their Compose project. Preserve project identity to avoid disconnecting existing storage. Do not run docker compose down -v against valuable data.

## Security and Verification

Compose configurations contain development-only credentials and local service settings. Do not expose these credentials in production. Use managed secrets, approved immutable image digests, appropriate authentication and network restrictions.

Phase 4 verification: ./scripts/verify_phase4.ps1 (PowerShell, from the repository root).

Docker configuration and runtime validation must be completed before claiming deployment readiness.
