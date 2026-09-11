# CitePrism — setting up on a new machine

This gets CitePrism (Phase 4) running from the handoff bundle. Budget about
40 minutes, most of it dependency downloads.

The bundle contains **no credentials**. API keys arrive separately.

---

## 1. Prerequisites

Install these before unzipping anything.

| Tool | Version | Why | Where |
|---|---|---|---|
| Python | **3.12.x** | The project pins 3.12 | python.org/downloads |
| Node.js | 20 LTS or newer | Builds the React reviewer client | nodejs.org |
| Git | any recent | Optional, but you'll want history eventually | git-scm.com |
| Docker Desktop | latest | Only for PostgreSQL + MinIO (Section 7) | docker.com |
| Tesseract OCR | 5.x | Only if scanned PDFs are needed (Section 8) | github.com/UB-Mannheim/tesseract |

Python 3.13 is **not** interchangeable here — some pinned wheels have no 3.13
build yet. During the Windows Python install, tick **"Add python.exe to PATH"**.

Check:

```powershell
py -3.12 --version     # Python 3.12.x
node --version         # v20 or higher
npm --version
```

If `py -3.12` reports "can't find", Python 3.12 isn't installed even if another
version is.

---

## 2. Unpack

```powershell
# Verify the bundle first — compare against the SHA-256 you were sent.
Get-FileHash "$env:USERPROFILE\Downloads\CitePrism_Handoff_<date>.zip" -Algorithm SHA256

Expand-Archive -Path "$env:USERPROFILE\Downloads\CitePrism_Handoff_<date>.zip" `
               -DestinationPath "$env:USERPROFILE\CitePrism" -Force
cd "$env:USERPROFILE\CitePrism"
Get-ChildItem -Recurse | Unblock-File
```

That last line matters. Windows marks everything from a downloaded zip as
blocked, and PowerShell refuses to run blocked scripts.

Sanity check — all three should print `True`:

```powershell
Test-Path .\pipeline_orchestrator.py, .\src\workspace\postgres_store.py, .\web\src\phase4.tsx
```

---

## 3. Allow scripts to run

Once per machine, in a **normal** (non-admin) PowerShell:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Or prefix each command with `powershell -ExecutionPolicy Bypass -File .\scripts\...`.

---

## 4. Install dependencies

```powershell
.\scripts\setup_phase4.ps1 -GenerateSigningKey
```

This creates `.venv-phase4`, installs Python and npm dependencies, copies
`env.example` to `.env`, and generates an Ed25519 export signing key into
`secrets\export-signing.key`.

The key is **new and machine-local**. Review packages Janvi exports will be
signed by her key, not Keyur's. That's correct — signing keys should not be
shared. If both machines must produce packages verifiable against one key,
that's a deliberate decision to make together, not a default.

Takes 5–15 minutes. If it fails on `npm ci`, see Section 10.

---

## 5. Add credentials

Open `.env` and fill in the values Keyur sends separately:

```ini
GOOGLE_API_KEY="..."          # required — manuscript parsing and assessment
HF_API_TOKEN="..."            # required — relevance scoring
OPENALEX_EMAIL="janvi@..."    # required — her own email, not Keyur's
OPENAI_API_KEY="..."          # only if LLM_PROVIDER=openai
SEMANTIC_SCHOLAR_API_KEY=""   # optional — raises rate limits
CROSSREF_MAILTO="janvi@..."   # optional — Crossref's faster polite pool
```

`OPENALEX_EMAIL` and `CROSSREF_MAILTO` should be **her** address. They identify
the caller for rate-limiting; reusing someone else's is impolite and shares a
quota.

Then point the export signing key at the file setup created:

```ini
CITEPRISM_EXPORT_SIGNING_KEY=secrets/export-signing.key
```

Leave everything else at defaults for now. Defaults are the local profile:
SQLite, local file storage, `local` auth, OCR off.

**Never commit `.env` or `secrets\`.** Both are already in `.gitignore`.

---

## 6. Verify

```powershell
.\scripts\verify_phase4.ps1 -Python .\.venv-phase4\Scripts\python.exe
```

Expect roughly **237 tests, 0 failures, ~17 skipped**. The skips are PostgreSQL
integration tests that need a live database — normal until Section 7.

If anything fails, stop and send the output to Keyur rather than continuing.

Then start it:

```powershell
.\scripts\start_phase4.ps1 -Python .\.venv-phase4\Scripts\python.exe
```

- Reviewer client: http://127.0.0.1:5173
- API docs: http://127.0.0.1:8000/api/docs

`Ctrl+C` stops both.

**At this point the project is working.** Sections 7–8 are optional.

---

## 7. Optional — durable runtime (PostgreSQL + MinIO)

Only needed to work on the Phase 4 durable queue, object storage, or the
PostgreSQL workspace. Needs Docker Desktop running.

```powershell
docker compose -f compose.phase4.yml up -d workspace-db objects provision
```

Then in `.env`:

```ini
CITEPRISM_WORKSPACE_BACKEND=postgres
CITEPRISM_WORKSPACE_DATABASE_URL=postgresql://citeprism_app:citeprism-app-dev-only@127.0.0.1:55433/citeprism_workspace_dev
CITEPRISM_OBJECT_BACKEND=s3
CITEPRISM_S3_BUCKET=citeprism-dev
CITEPRISM_S3_ENDPOINT_URL=http://127.0.0.1:9000
```

Migrations apply automatically on the next `start_phase4.ps1`. To run the
PostgreSQL tests too:

```powershell
$env:CITEPRISM_TEST_WORKSPACE_URL = "postgresql://postgres:postgres@127.0.0.1:55433/citeprism_workspace_test"
.\scripts\verify_phase4.ps1 -Python .\.venv-phase4\Scripts\python.exe -WithPostgres
```

Those credentials are development-only and hardcoded in the compose file. They
must never reach anything real.

---

## 8. Optional — OCR for scanned PDFs

Install Tesseract from the UB Mannheim build, then:

```ini
CITEPRISM_ENABLE_OCR=true
CITEPRISM_OCR_LANGUAGES=eng
```

Make sure `tesseract.exe` is on PATH (`tesseract --version` should answer).
Without it, CitePrism reports "this document needs OCR" instead of failing
silently — which is the intended behaviour, just less useful.

---

## 9. Daily use

```powershell
cd "$env:USERPROFILE\CitePrism"
.\scripts\start_phase4.ps1 -Python .\.venv-phase4\Scripts\python.exe
```

Before committing anything:

```powershell
.\scripts\verify_phase4.ps1 -Python .\.venv-phase4\Scripts\python.exe
```

Getting this into git, once she has repo access:

```powershell
git init
git add -A
git commit -m "CitePrism Phase 4: enterprise runtime and evidence acquisition"
git remote add origin <repository-url>
git push -u origin main
```

`.gitignore` already excludes `.env`, `secrets\`, `data\`, `.venv-*` and
`node_modules\`. Run `git status` once before the first commit and confirm
neither `.env` nor `secrets/export-signing.key` appears.

---

## 10. Troubleshooting

**`running scripts is disabled on this system`**
Section 3, and `Get-ChildItem -Recurse | Unblock-File`.

**`py -3.12` not recognised**
Python 3.12 isn't installed. `py --list` shows what is.

**`npm ci` fails with a lockfile error**
`cd web; npm install; cd ..` — regenerates `package-lock.json`. Mention it to
Keyur, since the lockfile then differs from his.

**Tests fail with `ModuleNotFoundError`**
The venv wasn't used. Always pass `-Python .\.venv-phase4\Scripts\python.exe`.

**Port 8000 or 5173 already in use**
`.\scripts\start_phase4.ps1 -ApiPort 8001 -WebPort 5174`. If the API port
changes, update `CITEPRISM_CORS_ORIGINS` in `.env` to match the web port.

**`Config validation failed`**
The message names each bad setting. Usually a missing API key or a typo in a
`CITEPRISM_*` value. Compare against `env.example`.

**Docker containers won't start**
Docker Desktop must be running first. Ports 55433 and 9000 must be free.

---

## What was NOT sent

Deliberately absent — request separately, or generate locally:

- `.env` — API keys. Ask Keyur over a secure channel, not email or chat.
- `secrets\export-signing.key` — Section 4 generates a new one.
- `data\` — uploaded manuscripts and evidence artifacts, except the evaluation
  gold sets, which are included.
- `.venv-phase4\`, `web\node_modules\` — rebuilt by setup.
- Local SQLite databases — recreated on first run.

Everything needed to build and run is in the bundle. Nothing that identifies a
person or authenticates as one is.
