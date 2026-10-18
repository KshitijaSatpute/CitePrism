# CitePrism — AI-Powered Academic Citation Evidence Assurance

<p align="center">
  <img src="Logo.png" alt="CitePrism Logo" width="40%">
</p>

<p align="center">
  <strong>AI-Assisted Academic Citation Auditing & Human Review Platform</strong>
</p>

<p align="center">
  <strong>Python · FastAPI · React · TypeScript · Gemini · Hugging Face · NLP</strong>
</p>

<p align="center">
  <strong>Version 5.4.2 · CitePrism V2.0 on the Phase 4 runtime · Windows, macOS and Linux</strong>
</p>

---

## 🔍 Overview

**CitePrism** is an AI-powered academic citation auditing system designed to help researchers, reviewers, and academic institutions examine the reliability, relevance, and traceability of references in research manuscripts.

The platform combines Natural Language Processing (NLP), Large Language Models (LLMs), scholarly metadata retrieval, semantic similarity, and evidence-grounded assessment to support academic citation review.

CitePrism was originally developed as an M.Sc. Applied Data Science case study and has evolved into a collaborative evidence-assurance platform.

The project focuses on:

- **Academic Manuscript Parsing**
- **Bibliography and In-Text Citation Extraction**
- **Scholarly Source Identification**
- **Citation Relevance Assessment**
- **Metadata Verification**
- **Evidence-Grounded AI Analysis**
- **Human-Supervised Citation Review**
- **Self-Citation Transparency**
- **Audit Trails and Provenance**
- **Exportable Citation Review Reports**

The goal is to help reviewers identify references that require further investigation while preserving transparency, traceability, and human oversight.

> **Responsible AI Principle:** CitePrism produces automated review findings and evidence-based proposals, not final judgments of citation error, bias, research quality, or academic misconduct. Every finding requires appropriate human interpretation.

---

<p align="center">
  <img src="CitePrism.png" alt="CitePrism Graphical Abstract" width="85%">
</p>

---

# 🚀 Quick Start

CitePrism runs on **Windows, macOS and Linux**. Setting it up takes about 15 minutes and needs one free
API key. Commands are shown for Windows PowerShell and for a macOS/Linux terminal; use the one for your
system.

## 1. Install the prerequisites

| Software | Version | Where to get it |
|---|---|---|
| Python | **3.12 recommended** (3.11 and 3.13 also work) | Windows: [python.org](https://www.python.org/downloads/) — tick **Add python.exe to PATH** · macOS: `brew install python@3.12` · Ubuntu/Debian: `sudo apt install python3.12 python3.12-venv` |
| Node.js | 20 or newer (includes npm) | [nodejs.org](https://nodejs.org/) |
| Git | any recent version | [git-scm.com](https://git-scm.com/) |

Close and reopen the terminal after installing, then check:

```powershell
# Windows
py -3.12 --version
node --version
git --version
```

```bash
# macOS / Linux
python3.12 --version
node --version
git --version
```

Each command should print a version number (Node.js `v20` or higher).

## 2. Get the code

```bash
git clone https://github.com/janvimahapadi-hub/CitePrism-V2.git
cd CitePrism-V2
```

Every later command runs inside this `CitePrism-V2` folder.

## 3. Install

**Windows (PowerShell):**

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned   # once per computer; answer Y
.\scripts\setup_phase4.ps1 -GenerateSigningKey
```

**macOS / Linux:**

```bash
./scripts/setup.sh
```

The setup script:

1. creates the Python environment `.venv-phase4` and installs the Python packages;
2. installs the web app's packages (`npm ci` in `web/`);
3. copies `env.example` to `.env`, your private settings file;
4. creates an export-signing key in `secrets/` and points `.env` at it.

`.env` and `secrets/` stay on your computer; git ignores both. The script is safe to run again: it keeps an
existing `.env` and key. Add `-WithEmbeddings` (Windows) or `--with-embeddings` (macOS/Linux) to compute text
similarity locally with Sentence Transformers; it installs PyTorch (about 2 GB). Without it CitePrism uses the
Hugging Face Inference API or a lightweight built-in fallback.

## 4. Add your API key

CitePrism needs a language model to read reference lists and judge citations. The quickest option is a free
Google Gemini key from [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey).

Open `.env` in any text editor (Windows: `notepad .env` · macOS: `open -e .env` · Linux: `nano .env`) and set:

```ini
LLM_PROVIDER="google"
GOOGLE_API_KEY="paste-your-gemini-key-here"
# Use the same Gemini key to judge citations (the default, huggingface, needs an HF_API_TOKEN):
CITEPRISM_ASSESSMENT_PROVIDER=google
# Your own e-mail: scholarly databases answer identified requests more reliably. It is not a password.
OPENALEX_EMAIL="you@example.com"
CROSSREF_MAILTO="you@example.com"
```

Save the file. Groq, OpenAI or a fully offline local model (Ollama) work instead of Gemini; see
[Configuration](#-configuration).

> **Keep keys private.** Never commit `.env` or anything in `secrets/`, and never paste keys into issues or
> chats. Free model plans may keep what you send them, so do not check confidential manuscripts with a free
> key (see [Confidential manuscripts](#1-language-model-gemini-groq-openai-or-a-local-model)).

## 5. Check the installation

```powershell
# Windows
.\scripts\verify_phase4.ps1
```

```bash
# macOS / Linux
./scripts/verify.sh
```

This compiles the code, validates `.env`, runs the test suite and builds the web app. It ends with
**“Phase 4 verification passed.”** (Windows) or **“Verification passed.”** (macOS/Linux). Messages that
PostgreSQL tests were skipped are expected: they need the optional Docker database.

## 6. Start CitePrism

```powershell
# Windows
.\scripts\start_phase4.ps1
```

```bash
# macOS / Linux
./scripts/start.sh
```

Then open **http://127.0.0.1:5173** in your browser. The API documentation is at
http://127.0.0.1:8000/api/docs. Keep the terminal open while you work; **Ctrl+C** stops CitePrism.

## 7. Check your first paper

1. Click **Check a new paper** and choose a PDF. A text-based PDF works best; scanned PDFs need OCR
   (`CITEPRISM_ENABLE_OCR=true` and [Tesseract](https://github.com/tesseract-ocr/tesseract)).
2. Follow the progress. A paper with 10–30 references usually takes a few minutes.
3. Read the summary under **Ask CitePrism** and ask in your own words — *“What should I fix first?”*,
   *“Explain it simply”*, *“Make me a PDF report”*.
4. Open **Review in detail** to see each item with its evidence and record a decision.

A successful start does not prove that every external service is reachable; a completed check does.

## Daily use and updates

```bash
git pull                          # get the latest version
```

Then run the setup script again (it keeps your `.env` and key), the check from step 5, and the start command
from step 6. Without updates, the start command alone is enough.

---

# 🖥️ Using CitePrism

CitePrism runs in your browser at **http://127.0.0.1:5173** (React and TypeScript, backed by FastAPI).
It has two modes, switched at the top of the page.

### Ask CitePrism — for everyone

A guided mode for readers without technical background.

1. **Check a new paper:** drop in a PDF and start the check.
2. **Follow progress** in four plain steps: reading the paper, finding each cited work, checking each
   citation, writing up.
3. **Read the summary:** how many references were checked, which are worth a closer look and why, each
   shown with its reference number.
4. **Talk to it like a chat.** Ask in your own words, several things at once, with typos — for example
   *“I got the results and don't know what to do with the actions — explain it like I'm five and give me a
   PDF with the fixes and the missing citations.”* It keeps the conversation, so *“why?”*, *“the second
   one”*, *“how do I fix it?”* and *“yes please”* refer to what was just said. It can:
   - summarise the results, list what to check first, and explain any reference (by number, author or title);
   - explain the decision buttons and the words used (DOI, self-citation, relevance score …);
   - switch to very simple language on request (*“explain like I'm 5”*) and keep it;
   - make a **plain-language PDF report** with a tick-box to-do list, step-by-step fixes and possibly
     missing works;
   - draft a **courteous message to the authors** listing each point to check, with Copy and e-mail buttons;
   - answer *“Is this paper any good?”* honestly: it checks references, not a paper's quality;
   - decline to call anyone dishonest — it shows what is worth checking, never why it happened.
   Conversations are kept in your browser across reloads; **Start over** clears them.

Answers are built from the audit's own results, so the assistant cannot report a problem the audit did not
find. For questions outside its built-in topics, the configured AI model may rephrase those same results;
such answers are labelled, every number and reference in them is checked against the audit, and anything
that fails the check is replaced by a built-in answer (`CITEPRISM_ASSISTANT_MODEL=off` disables the model).
German questions are answered in German when a model is configured.

If a check stops, the page explains why in plain language (for example, *“The AI service is busy right
now”*) and offers **Try again**; finished steps are reused.

### Books, proceedings and theses

A PDF with several reference lists (an edited volume, conference proceedings, a cumulative thesis) is
recognised when you open it. CitePrism lists the chapters it found — pages, authors, number of references —
and offers **Split into chapters**: each chapter becomes its own manuscript with its own authors, citations
and report, which is what makes the self-citation check meaningful. The original PDF is kept. You can also
check the whole PDF at once; each chapter's citations are then still matched only with its own list.

Author–year reference lists (APA, Harvard, German conventions such as *Vgl. Wenger, 1998* in footnotes and
*(o. J.)* dates) are read by rule, in English, German, French, Spanish, Italian, Dutch and Swedish, so they do
not depend on an AI model. Citations whose year disagrees with the reference list are reported.

### Review in detail — for reviewers and editors

| Section | Purpose |
|---|---|
| Items to check | Everything worth a look, most serious first; record a decision with a reason |
| References | Each reference beside its source: as listed, where it is cited, the published record, and the passage the AI check relied on |
| Self-citation & missing works | Self-citation rate and patterns; works the paper's own references cite often |
| Export | Signed PDF, HTML and JSON review packages |

The **More** menu holds Repairs (re-run steps that failed for technical reasons), Activity (every event
and decision in order), Team (reviewers and workload) and Operations (speed, cost and service targets).

Every reference carries the same mark throughout: its number with a red (serious), ochre (worth a look)
or green (fine) stroke.

---

# 🔑 Configuration

All settings live in `.env`, which the setup script creates from `env.example`. It holds your private keys, so
it is excluded from git and must never be shared. `env.example` documents every setting; the sections below
cover the ones most people need.

## 1. Language model (Gemini, Groq, OpenAI or a local model)

CitePrism needs one language model to read reference lists and judge citations. **Gemini is not
required.** Choose one:

> **Confidential manuscripts.** The model receives the paper's text. Free plans may keep it: Google's
> [Gemini API terms](https://ai.google.dev/terms) for unpaid services say content is used to improve
> Google's products, may be read by human reviewers, and that sensitive or confidential information must
> not be submitted. A paper under peer review is confidential. For real submissions use a paid plan whose
> terms exclude training, or a local model, and set `CITEPRISM_MODEL_DATA_TERMS` (`no_training` or the
> enforced `local_only`). Until then the upload screen names the services and warns people.

| Option | Cost | Settings in `.env` |
|---|---|---|
| Google Gemini | Free tier, key from https://aistudio.google.com/ | `LLM_PROVIDER="google"`, `GOOGLE_API_KEY=...`, `GOOGLE_MODEL="gemini-3.6-flash"` |
| Groq | Free tier with tight per-minute limits — best as a fallback; key from https://console.groq.com/ | `OPENAI_COMPATIBLE_BASE_URL=https://api.groq.com/openai/v1`, `OPENAI_COMPATIBLE_MODEL=llama-3.3-70b-versatile`, `OPENAI_COMPATIBLE_API_KEY=...`, `OPENAI_COMPATIBLE_MAX_TOKENS=6000` (plus `LLM_PROVIDER=openai_compatible` to make it the main model) |
| OpenAI | Paid | `LLM_PROVIDER="openai"`, `OPENAI_API_KEY=...`, `OPENAI_MODEL="gpt-4o"` |
| Ollama on your computer | Free, fully offline | See `deploy/compose/compose.offline.yml` and the Ollama preset in `env.example` |

Citations are judged by `CITEPRISM_ASSESSMENT_PROVIDER`: set it to the same provider (`google`, `openai` or
`openai_compatible`) so one key does everything, or keep the default `huggingface` and add an `HF_API_TOKEN`
(next section).

**When a model is busy.** Free tiers often answer *HTTP 503 — model overloaded*. CitePrism then
automatically tries the next model in `LLM_FALLBACK_MODELS`. Left empty, Gemini falls back to
`gemini-3.1-flash-lite`, then `gemini-3.5-flash`. For more resilience, add a different provider as a last
resort, for example:

```ini
LLM_FALLBACK_MODELS="gemini-3.1-flash-lite,gemini-3.5-flash,openai_compatible:llama-3.3-70b-versatile"
```

Google retires Gemini models regularly: 2.0 Flash was shut down in June 2026 and 2.5 Flash is discontinued
on 16 October 2026 (see [Gemini deprecations](https://ai.google.dev/gemini-api/docs/deprecations)). A
retired model is skipped automatically by the fallback chain, but update `GOOGLE_MODEL` when it happens.

A `provider:model` entry uses the `OPENAI_COMPATIBLE_*` settings from the table, so fill those in too.
Each audit records which model actually produced its results.

## 2. Hugging Face

Configure:

```ini
HF_API_TOKEN="YOUR_OWN_HF_TOKEN"

HF_MODEL="meta-llama/Llama-3.1-8B-Instruct"
```

Create a token at:

https://huggingface.co/settings/tokens

The selected model may require the appropriate access and inference permissions.

## 3. OpenAlex

Configure:

```ini
OPENALEX_EMAIL="YOUR_OWN_EMAIL_ADDRESS"
```

Use your own email address.

The email identifies the API caller. It is not a password.

An additional OpenAlex API key may be needed depending on the access requirements of the configured service.

## 4. Optional API Credentials

CitePrism also contains optional configuration for:

- OpenAI
- Semantic Scholar
- Crossref
- Elsevier
- Springer

Do not share private keys between developers.

Only configure optional providers needed for your workflow.

### Database errors ("Not checked: a scholarly database did not respond")

CitePrism will not call a reference missing when a database simply did not answer, so such references are
shown as *not checked* and counted as technical gaps. The report names the database and the reason. The most
common cause is Semantic Scholar limiting anonymous requests. Either:

- add a free key — `SEMANTIC_SCHOLAR_API_KEY=...` in `.env` (request one at semanticscholar.org/product/api), or
- leave Semantic Scholar out — `CITEPRISM_RESOLUTION_PROVIDERS=openalex,crossref`.

Also set `CROSSREF_MAILTO` and `OPENALEX_EMAIL` to your address; both services answer identified requests
more reliably. Then use **Run the check again**.

The specialist indexes — dblp (computer science), Europe PMC (life sciences) and DataCite (datasets,
software) — are asked only after OpenAlex, Crossref and Semantic Scholar could not match a reference. If one
of them fails while the general indexes all answered, the reference is reported as *not found* with the
failed index named (“dblp returned an error (HTTP 500), so that index was not searched”) instead of being
left unchecked. dblp is tried at dblp.org and, after any failure, at its official mirror
(`CITEPRISM_DBLP_MIRROR_URLS`, default `https://dblp.uni-trier.de`). If the report says dblp “refused the
request”, a firewall, security suite or company proxy is blocking it: allow both hosts, or drop dblp with
`CITEPRISM_SUPPLEMENTARY_PROVIDERS=europe_pmc,datacite`.

## 5. Export Signing Key

Exported review packages are signed so a reader can check they were not altered. The setup script creates a
key in `secrets/export-signing.key` and adds this line to `.env`:

```ini
CITEPRISM_EXPORT_SIGNING_KEY=secrets/export-signing.key
```

Each installation uses its own key. Never share or commit it; if a key was ever pushed, follow
[the rotation runbook](docs/operations/SECURITY_KEY_ROTATION.md).

## 6. Local Runtime Configuration

For the default single-workstation setup, use:

```ini
CITEPRISM_WORKSPACE_BACKEND=sqlite

CITEPRISM_OBJECT_BACKEND=local

CITEPRISM_AUTH_MODE=local

CITEPRISM_RUN_EMBEDDED_WORKER=true
```

These settings allow CitePrism to run locally without Docker.

Keep the other Phase 4 settings supplied by `env.example`.

Save the file before continuing.

> **Security:** Never commit `.env`, private signing keys, API credentials, uploaded manuscripts, or local databases to GitHub.

---

# 🐳 Docker and Database Configuration

CitePrism supports two runtime profiles.

## Default Local Runtime

The default development environment uses:

```text
React Frontend
      |
      v
FastAPI Backend
      |
      v
Python Pipeline
      |
      v
SQLite Database
      +
Local File Storage
```

This is intended for development on a trusted workstation.

No Docker installation is required.

## Optional Durable Runtime

Phase 4 additionally supports:

- PostgreSQL
- S3-compatible object storage
- MinIO
- Separate worker processes
- Durable audit jobs
- Multi-user infrastructure

For deployment profiles, validation, and persistent storage, see the [Deployment Guide](deploy/README.md).

Docker helps run PostgreSQL and MinIO consistently across development machines.

The optional services can be started using:

```powershell
docker compose --project-directory . -f deploy/compose/compose.phase4.yml up -d workspace-db objects provision
```

The database, object-storage, and worker configuration must then be updated according to:

[Phase 4 Enterprise Runtime](docs/phases/PHASE_4_ENTERPRISE_RUNTIME.md)

The Docker Compose credentials supplied for local development must not be reused for a production deployment.

**Docker is optional for normal local CitePrism development.**

---

# 🏗️ Architecture

<p align="center">
  <img src="Case-study-architecture.png" alt="CitePrism Architecture" width="85%">
</p>

The original project architecture illustrates the development of the citation-analysis pipeline.

The current Phase 4 architecture extends this foundation with a React reviewer workspace, FastAPI backend, persistent audit jobs, evidence acquisition, human-review workflows, and optional durable infrastructure.

### Phase 4 Architecture

![Phase 4 Architecture](docs/architecture/phase4-architecture.svg)

The current application follows this workflow:

```text
                  USER / RESEARCHER
                         |
                         v
                Upload Research PDF
                         |
                         v
                  React Frontend
                         |
                         v
                    FastAPI
                         |
                         v
                 Audit Job Created
                         |
                         v
                 Python Pipeline
                         |
          +--------------+--------------+
          |              |              |
          v              v              v
       Parsing       Reference       Citation
                     Resolution      Assessment
          |              |              |
          +--------------+--------------+
                         |
                         v
                Evidence Validation
                         |
                         v
                  Review Findings
                         |
                         v
                 Human Reviewer
                         |
                         v
              Review Report / Export
```

---

# 🚀 How CitePrism Works

CitePrism analyses an uploaded academic manuscript and investigates the references used within it.

The complete workflow is explained below.

## Step 1 — Manuscript Upload

A researcher uploads an academic PDF through the CitePrism web interface.

The system registers the manuscript and prepares it for processing.

The uploaded document is preserved as part of the audit evidence.

## Step 2 — Document Parsing

CitePrism uses its document-processing pipeline and configured LLM provider to extract structured information from the uploaded manuscript.

Extracted information includes:

- Manuscript title
- Authors
- Bibliography entries
- In-text citation markers
- Citation contexts
- Citation locations

For example:

> "Artificial intelligence improves medical diagnosis accuracy [5]."

CitePrism identifies citation `[5]` and attempts to connect it with the corresponding bibliography entry.

## Step 3 — Reference Identification

After extracting the bibliography, CitePrism attempts to identify the real scholarly publications referenced by the manuscript.

It uses external scholarly databases including:

- OpenAlex
- Crossref
- Semantic Scholar

The system compares available bibliographic information such as:

- Publication title
- Author names
- Publication year
- DOI

If a reliable publication identity cannot be established, CitePrism marks the reference as unresolved rather than silently selecting an uncertain match.

## Step 4 — Metadata Enrichment

Once a publication is identified, CitePrism retrieves available scholarly metadata.

This may include:

- Publication title
- Authors
- DOI
- Publication year
- Journal or venue
- Abstract
- Available source information

CitePrism does not require a local database containing millions of academic papers.

Instead, it searches external scholarly services and stores the evidence associated with each audit.

## Step 5 — Citation Relevance Assessment

CitePrism evaluates the relationship between the text surrounding a citation and the available evidence from the cited publication.

For example:

**Manuscript claim:**

> "The proposed model improves classification accuracy by 30%."

**Retrieved source evidence:**

> "The model achieved an 8% improvement in classification accuracy."

CitePrism may identify the difference as requiring further investigation.

However, the system does not automatically conclude that the manuscript is incorrect. Different datasets, experiments, metrics, or publication versions may explain the difference.

## Step 6 — Evidence Grounding

CitePrism validates AI-generated evidence proposals against the retrieved source evidence.

If a model produces a quotation, the system checks whether that quotation can be located in the stored source snapshot.

This helps identify unsupported or potentially hallucinated model-generated evidence.

A successfully located quotation establishes attribution to the stored evidence, not necessarily the scientific correctness of the interpretation.

## Step 7 — Human Review

CitePrism generates review findings based on the available evidence.

A reviewer can:

- Inspect the original bibliography entry
- Examine retrieved source metadata
- Review citation contexts
- Investigate model-generated proposals
- Confirm a review item
- Dismiss a review item
- Request additional evidence
- Escalate an issue
- Propose bibliography corrections

Human reviewers remain responsible for interpreting the results.

---

# 🧠 Technology Stack

| Technology | Purpose |
|---|---|
| Python 3.12 | Core backend and citation-processing pipeline |
| FastAPI | Backend REST API |
| React | Reviewer interface |
| TypeScript | Frontend development |
| Google Gemini | LLM-assisted parsing and assessment |
| Hugging Face | LLM-based relevance scoring |
| Sentence Transformers | Semantic text embeddings (local; optional extra) |
| Cosine Similarity / TF-IDF / BM25 | Similarity scoring and full-text passage selection |
| Ollama / vLLM (OpenAI-compatible) | Optional fully offline LLM deployment |
| ORCID | Author disambiguation for self-citation analysis |
| OpenAlex | Scholarly metadata retrieval |
| Crossref | Publication and DOI metadata |
| Semantic Scholar | Additional source resolution |
| SQLite | Default local workspace database |
| PostgreSQL | Optional durable database |
| MinIO | Optional S3-compatible object storage |
| Docker | Optional containerized infrastructure |
| Git & GitHub | Version control and collaboration |

---

# ✨ Core Features

## 1. Citation Extraction

CitePrism extracts bibliography entries and in-text citations from uploaded academic manuscripts.

It links citation occurrences with bibliography entries where possible.

## 2. Reference Resolution

The system searches scholarly databases to identify the publications cited in a manuscript.

Uncertain matches are preserved for human review.

## 3. Metadata Verification

CitePrism compares the bibliography information submitted by the author against available external publication records.

Each resolved reference is checked field by field and graded minor, moderate or major:

- DOI mismatch, and whether a printed DOI is actually registered
- Title and first-author / author-list differences
- Publication-year mismatch (off-by-one treated as minor)
- Venue mismatch, aware of abbreviations, acronyms and common aliases (e.g. CVPR, NeurIPS)
- Retractions reported by Crossref
- Unresolved source identity and incomplete bibliography metadata

## 4. Citation Relevance Assessment

Every citation receives a hybrid relevance score (see [Hybrid Relevance Engine](#hybrid-relevance-engine)):
an evidence-grounded LLM judgement combined with embedding similarity between the citing sentence and the
cited work. Low scores raise a *potentially irrelevant* finding; when only similarity is available the finding
is the weaker *low topical similarity*. When open-access full text is available, the most relevant passages
are selected per citation with BM25 instead of truncating the paper.

Automated assessments remain proposals for human review.

## Self-Citation Analysis

Author overlap between the manuscript and each reference is matched by ORCID, by surname plus initials, or by
surname only (shown but not counted). The audit reports self-citation rate and flags four patterns —
high self-citation rate, concentration on one author, self-citations never cited in the text, and
self-citations with low relevance. Supplying author ORCIDs when starting an audit makes matching exact.

## Possibly Missing Literature

Works cited by three or more of the manuscript's own references (co-citation via OpenAlex) are surfaced as
possibly missing key literature. Model-suggested papers are only shown as suggestions after they resolve to
a real scholarly record; unverifiable leads are reported separately.

## 5. Evidence-Grounded Analysis

CitePrism checks whether model-generated quotations can be located in the stored source evidence.

This supports more transparent and traceable AI-assisted assessment.

## 6. Human Review Workspace

The system presents review findings through a React-based reviewer interface.

Reviewers can examine evidence and record decisions with rationales.

## 7. Bibliography Correction Proposals

Reviewers can suggest corrections to bibliographic information without modifying the original submitted manuscript.

## 8. Repair Workflows

System-related processing failures can be investigated and retried.

Repairs create new audit runs rather than overwriting existing audit evidence.

## 9. Audit History and Provenance

CitePrism records audit events and reviewer actions to support traceability.

## 10. Exportable Reports

The system supports review outputs including:

- PDF
- HTML
- JSON
- Signed review bundles

---

# 📏 Measuring Accuracy

`scripts/benchmark_injected.py` plants known errors in a real PDF — mistyped citation years, dropped
citations, perturbed metadata, decoy records, author-name variants — and reports how many CitePrism finds
and how many false alarms it raises. It runs offline, needs no API key, and gives the same numbers for the
same seed:

```powershell
.\.venv-phase4\Scripts\python.exe scripts\benchmark_injected.py paper.pdf --authors "Dana Blume" "Benno Volk"   # Windows
```

```bash
.venv-phase4/bin/python scripts/benchmark_injected.py paper.pdf --authors "Dana Blume" "Benno Volk"            # macOS / Linux
```

On a 263-page German edited volume (248 references) every planted error was found with no false alarms
after the fixes in 5.4; the first run exposed four real defects. Results, scope and limits:
[docs/evaluation/BENCHMARK.md](docs/evaluation/BENCHMARK.md). These numbers cover the rule-based detectors only — accuracy of
relevance and support judgements needs a human-annotated evaluation, which has not been done yet.

# 🧩 Additional NLP and Research Features

## Hybrid Relevance Engine

The relevance score used in every audit is:

```text
RS_final = 0.6 × RS_llm + 0.4 × RS_embed
```

- `RS_llm` comes only from evidence-grounded assessments whose quotes were located in the stored source text;
  *insufficient evidence* contributes nothing rather than a guessed score.
- `RS_embed` is the similarity between the citing sentence and the cited work, computed with local
  Sentence Transformers, the Hugging Face Inference API, or a lightweight TF-IDF fallback
  (`CITEPRISM_SIMILARITY_BACKEND`).
- Labels: **Relevant** ≥ 70, **Borderline** 40–69, **Potentially irrelevant** < 40.

Weights and thresholds are configurable and are recorded in each audit's analysis metadata. The scores are
review-support signals and must not be interpreted as final human-reviewed citation judgments.

## Self-Citation Transparency

See [Self-Citation Analysis](#self-citation-analysis). Author overlap is never treated by itself as evidence
of inappropriate self-citation or academic misconduct.

## Literature Search Candidates

See [Possibly Missing Literature](#possibly-missing-literature). Unverified candidates are labelled as such
and are not missing-citation findings.

## Citation Intent Analysis

*Not part of the current audit pipeline.* The earlier Streamlit prototype included analysis of why a publication is cited.

Examples include:

- Supporting
- Contrasting
- Methodology
- Background

## Temporal Citation Analysis

*Descriptive only; not part of the current audit findings.*

Citation age and publication-year distributions may provide descriptive context.

Publication age alone is not treated as a quality defect.

## Explainability and Provenance

The system presents available evidence and review explanations so findings can be investigated rather than accepted blindly.

---

# 🗂️ Repository Structure

```text
CitePrism-V2/
├── citeprism_api.py           FastAPI application entry point (uvicorn citeprism_api:app)
├── pipeline_orchestrator.py   Runs an audit: parse → resolve → assess → review
├── config/                    Settings read from .env (config/settings.py, config/phase4.py)
├── database/                  SQLite/PostgreSQL schemas and migrations
├── src/
│   ├── extractors/            Reading the PDF: references, citations, chapters
│   ├── enrichers/             Looking each work up in scholarly databases
│   ├── evidence/              Identity, metadata consistency, relevance, self-citation, missing works, review
│   ├── scorers/               Quotation-grounded citation assessment
│   ├── assistant/             The plain-language chat and PDF report
│   ├── workspace/             Audits, jobs, decisions, exports, storage
│   ├── webapi/                REST API used by the web app
│   ├── evaluation/            Metrics, calibration, benchmark reports
│   └── telemetry/             Runtime monitoring
├── web/                       React + TypeScript web app (Vite)
├── tests/                     Automated tests (unittest); fixtures in tests/fixtures/
├── scripts/                   setup / verify / start for Windows (.ps1) and macOS/Linux (.sh), worker, tools
├── docs/                      Architecture, evaluation, benchmark, requirement traceability, security
├── env.example                Template for your private .env
├── requirements/                Python dependency manifests
└── deploy/                      Docker deployment files
    └── compose/
        ├── compose.phase3.yml
        ├── compose.phase4.yml
        ├── compose.trust.yml
        └── compose.offline.yml
```

---

# 🛠️ Troubleshooting

| Problem | What to do |
|---|---|
| `py -3.12` not found (Windows) | Install Python 3.12 from python.org; check `py --list`. Or run `.\scripts\setup_phase4.ps1 -GenerateSigningKey -PythonLauncher python` |
| `python3.12: command not found` (macOS/Linux) | Install it (see step 1), or run `./scripts/setup.sh --python python3.13` |
| `ensurepip is not available` (Ubuntu/Debian) | `sudo apt install python3.12-venv`, then run the setup again |
| `running scripts is disabled on this system` (Windows) | Run the `Set-ExecutionPolicy` line from step 3 |
| `Permission denied` running `./scripts/setup.sh` | `chmod +x scripts/*.sh`, or run `bash scripts/setup.sh` |
| `/bin/bash^M: bad interpreter` | The scripts were checked out with Windows line endings. Run `git config core.autocrlf input`, then `git checkout -- scripts` |
| `npm` not recognised | Install Node.js 20+, then close and reopen the terminal (or VS Code) |
| `ModuleNotFoundError` | Start CitePrism with the scripts, which use `.venv-phase4`; rerun the setup if packages are missing |
| Configuration validation failed | Compare `.env` with `env.example`; the message names the setting |
| Port 8000 or 5173 already in use | Stop the other program, or start on other ports: `.\scripts\start_phase4.ps1 -ApiPort 8001 -WebPort 5174` / `./scripts/start.sh --api-port 8001 --web-port 5174` |
| Gemini HTTP 503 (overloaded) or 429 (quota) | Wait and use **Run the check again**; add a fallback model (`LLM_FALLBACK_MODELS`) |
| Model not found (HTTP 404) | The model was retired; set a current `GOOGLE_MODEL` |
| “Not checked: a scholarly database did not respond” | See [Database errors](#database-errors-not-checked-a-scholarly-database-did-not-respond) |
| A PDF yields no references | It may be scanned (enable OCR) or have no text layer; try another PDF to rule out setup problems |

To see which provider and model are configured, without showing keys:

```powershell
Select-String -Path .env -Pattern '^\s*(LLM_PROVIDER|GOOGLE_MODEL|CITEPRISM_ASSESSMENT_PROVIDER)\s*='   # Windows
```

```bash
grep -E '^\s*(LLM_PROVIDER|GOOGLE_MODEL|CITEPRISM_ASSESSMENT_PROVIDER)\s*=' .env                     # macOS / Linux
```

---

# 🤝 Contributing

Contributions are welcome. Team members with write access work on branches in this repository; everyone else
forks it first.

```bash
git switch main && git pull                 # start from the latest code
git switch -c feature/short-description     # one branch per change
# … make and test the change …
./scripts/verify.sh                         # Windows: .\scripts\verify_phase4.ps1
git add -A && git status                    # check that no .env, keys, PDFs or databases are staged
git commit -m "Describe the change"
git push -u origin feature/short-description
```

Then open a pull request against `main`. GitHub Actions runs the tests on Linux and Windows with Python
3.11–3.13, builds the web app and scans for committed secrets; a pull request is merged only when they pass.

---

# 📚 Development History and Documentation

CitePrism has evolved through several phases.

| Phase | Focus |
|---|---|
| Phase 0 | Stabilization of the original citation-analysis prototype |
| Phase 1 | Trust, provenance, and storage foundations |
| Phase 2 | Evidence engine and citation assessment |
| Phase 3 | FastAPI and human review workspace |
| Phase 4 | Durable runtime, evidence acquisition, and enterprise-oriented capabilities |
| V2.0 | Hybrid relevance scoring, self-citation and metadata-consistency analysis, verified missing-literature suggestions, offline deployment, evaluation with confidence intervals |

Earlier versions of CitePrism included a Streamlit-based analyst interface.

The current main reviewer interface uses React and TypeScript.

Historical components remain useful for understanding how the project evolved, but new developers should follow the Phase 4 setup instructions.

### Technical Documentation

- [CitePrism V2.0 requirement traceability](docs/evaluation/V2_REQUIREMENTS_TRACEABILITY.md) — every V2.0 requirement mapped to code and tests
- [Evaluation](docs/evaluation/EVALUATION.md) — an examiner's assessment: strengths, limits, steps before deployment
- [Error-injection benchmark](docs/evaluation/BENCHMARK.md) — how accurately the rule-based checks find planted errors
- [Removing committed secrets and rotating keys](docs/operations/SECURITY_KEY_ROTATION.md)

- [Phase 0 — Stabilization](docs/phases/PHASE_0_STABILIZATION.md)
- [Phase 1 — Trust Kernel](docs/phases/PHASE_1_TRUST_KERNEL.md)
- [Phase 2 — Evidence Engine](docs/phases/PHASE_2_EVIDENCE_ENGINE.md)
- [Phase 3 — Human Review Workspace](docs/phases/PHASE_3_REVIEW_WORKSPACE.md)
- [Phase 4 — Enterprise Runtime](docs/phases/PHASE_4_ENTERPRISE_RUNTIME.md)
- [Threat Model](docs/architecture/THREAT_MODEL.md)

---

# 🔐 Security and Responsible AI

CitePrism is designed as a human-supervised research review system.

The project aims to:

- Preserve original manuscript evidence
- Distinguish source evidence from AI interpretation
- Maintain traceable review history
- Support human reviewer decisions
- Avoid unsupported claims about academic misconduct
- Preserve uncertainty when evidence is insufficient
- Identify system failures separately from manuscript issues
- Protect API credentials and signing keys
- Prevent accidental publication of private research documents

Never commit the following files or directories:

```text
.env
secrets/
.venv-phase4/
node_modules/
local databases
uploaded manuscripts
```

`.gitignore` excludes them. Before every push run:

```powershell
.\.venv-phase4\Scripts\python.exe scripts\check_no_secrets.py   # Windows
```

```bash
.venv-phase4/bin/python scripts/check_no_secrets.py              # macOS / Linux
```

CI runs the same check and fails if a key file or `.env` is tracked. If a key has ever been pushed, deleting
it is not enough — follow [the rotation runbook](docs/operations/SECURITY_KEY_ROTATION.md).

The default local-authentication application must not be exposed directly to the public internet.

---

# 📌 Project Status

CitePrism Phase 4 provides:

- Academic manuscript processing
- Citation extraction
- Scholarly source resolution
- Evidence-based citation assessment
- React reviewer workspace
- FastAPI backend
- Human review and correction workflows
- Audit repair mechanisms
- Review history and provenance
- Signed exports
- Runtime monitoring
- Optional PostgreSQL and S3-compatible storage
- Hybrid LLM + embedding relevance scores with Relevant / Borderline / Potentially irrelevant labels
- ORCID-aware self-citation analysis and pattern detection
- Field-level metadata consistency checks
- Co-citation and verified missing-literature suggestions
- Fully offline operation with a local OpenAI-compatible model (`deploy/compose/compose.offline.yml`)
- Evaluation with AUROC, threshold sweeps and bootstrap confidence intervals

The default local runtime is intended for development and testing on a trusted workstation.

Additional deployment configuration and operational validation are required for production use.

---

# 🎓 Project Background

CitePrism originated as an M.Sc. Applied Data Science case study exploring academic citation relevance, scholarly metadata enrichment, explainable NLP, and responsible AI-assisted research review.

The project has evolved into a collaborative evidence-assurance workspace with an emphasis on transparent analysis, source verification, human oversight, and reproducible review workflows.

---

# 📄 License

CitePrism is released under the [MIT License](LICENSE).

`tests/fixtures/evidence/underwater_manuscript.pdf` (and the recorded responses derived from it) is a student
research paper used as a test document. Its copyright stays with its authors: it is not covered by the MIT
License and may not be reused outside this project's tests without their permission.
