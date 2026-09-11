# CitePrism — Phase 3 human review workspace

Phase 3 adds a versioned FastAPI boundary, restart-aware audit jobs, saved reviewer decisions, non-destructive bibliography correction proposals, a tamper-evident activity chain, and a responsive React/TypeScript evidence workspace. The verified Phase 2 evidence engine remains underneath it; Streamlit remains available as a diagnostic fallback.

Start with [Phase 3 setup and behavior](docs/PHASE_3_REVIEW_WORKSPACE.md). On Windows, use `scripts/setup_phase3.ps1`, then `scripts/start_phase3.ps1`. Run `scripts/verify_phase3.ps1 -WithPostgres` before accepting a release. Phase 2 remains documented in [the evidence-engine guide](docs/PHASE_2_EVIDENCE_ENGINE.md).

The earlier project documentation follows for historical context; its hybrid score and legacy dashboard descriptions do not define the new evidence review policy.

---

<p align="center">
  <img src="Logo.png" alt="CitePrism Logo" width="40%">
</p>

## 🔍 Overview

**CitePrism** is a transparent, scalable NLP system designed for auditing academic manuscripts. Developed as an M.Sc. Applied Data Science case study, it focuses on:

- **Citation Relevance Scoring**
- **Ethical Self-Citation Detection**
- **Hybrid Evaluation (HuggingFace LLMs + Sentence Embeddings)**
- **Interactive Analyst Tooling (Streamlit IDE)**
- **Audit-ready PDF/HTML Exporting**

CitePrism helps reviewers prioritize citation checks using explainable relevance,
metadata, provenance, and descriptive author-overlap signals.

> **Prototype status:** CitePrism V2 produces automated review-priority signals,
> not findings of error, bias, or misconduct. Every output requires human review.

## Phase 0 development setup

Use Python 3.11 or 3.12. Do not copy or commit a prebuilt `.venv`.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item env.example .env
python -m unittest discover -v
streamlit run streamlit_app.py
```

Set `GOOGLE_API_KEY` when `LLM_PROVIDER=google`, or `OPENAI_API_KEY` when
`LLM_PROVIDER=openai`. The Hugging Face scoring stage separately requires
`HF_API_TOKEN`. Credentials are validated only when their feature starts.

`requirements.in` now records direct dependencies. The existing
`requirements.txt` is a legacy machine snapshot; replace it with a compiled,
hashed, platform-tested lock file before deployment.

---

<p align="center">
  <img src="CitePrism.png" alt="CitePrism Graphical Abstract" width="85%">
</p>

<p align="center">
  <img src="Case-study-architecture.png" alt="CitePrism Architecture" width="85%">
</p>
---

## Recent Improvements

CitePrism V2 has been improved through multiple merged feature and fix PRs to make the system faster, more stable, and more useful for academic citation reviewers.

### Completed Improvements

1. **Cached SentenceTransformer model loading**
   Reduced repeated embedding model loading and improved scoring speed after the first load.

2. **Made missing citation generation optional**
   Added Fast Audit and Full Audit modes so users can skip expensive missing citation suggestions during normal audits.

3. **Added runtime metrics in the upload UI**
   The app now shows parsing time, enrichment time, scoring time, missing citation time, and total pipeline time after each audit.

4. **Added PDF viewer download fallback**
   Added a download option in case the browser PDF preview fails or appears blank.

5. **Improved Gemini JSON response parsing**
   Made the Gemini parsing stage more robust against noisy or malformed JSON responses.

6. **Added chunked parsing for medium PDFs**
   Medium-size manuscripts are parsed in chunks to reduce timeout and extraction failure risk.

7. **Cleaned noisy upload UI logs**
   Reduced unnecessary backend/debug logs in the Streamlit UI so users see cleaner audit progress.

8. **Added ranked citation review table**
   Added a reviewer-facing table that ranks problematic citations first and shows citation marker, reference title, authors, final score, label, risk flags, explanation, and citation intent.

9. **Fixed enrichment timing in runtime summary**
   Ensured OpenAlex enrichment time is always captured and displayed correctly in the runtime summary.

### Validation Proof

The medium test manuscript was used for validation. The system successfully extracted:

* **20 references**
* **18 in-text citations**

The runtime summary now correctly separates:

* Parsing time
* Enrichment time
* Scoring and risk detection time
* Missing citation generation time
* Total pipeline time

### Overall Outcome

These improvements make CitePrism V2 more demo-ready, more transparent, and closer to a practical academic citation review workflow.


## ✨ Core Pipeline Features

### **1. Hybrid Relevance Engine**
- Computes cosine similarity between manuscript and reference abstracts (`RS_embed`).
- Prompts Large Language Models to judge in-text citation context (`RS_llm`).
- Combines metrics using a calibrated formula: `RS_final = 0.6 × LLM_score + 0.4 × Embedding_score`.
- Categorizes citations dynamically into **Relevant**, **Borderline**, or **Irrelevant**.

### **2. Self-Citation Transparency**
- Detects overlaps in Authors, Teams, and Publication Venues.
- Evaluates topical relevance rather than blindly penalizing self-citations (adhering to COPE guidelines).
- Reports author overlap descriptively for editorial review.

### **3. The Analyst IDE (User Interface)**
- **Side-by-Side Data Extraction Viewer:** Manually verify extracted metadata and in-text contexts alongside the original PDF.
- **Dynamic Threshold Slider (τ):** Interactively filter low-scoring citations in real-time.
- **Explainability:** Displays exact text evidence, AI rationales, and color-coded status badges.

---

## 🚀 Advanced Diagnostics (Student Enhancements)

This project exceeds baseline requirements by implementing deep-dive bibliometric analytics:

- 🧠 **Literature Search Candidates:** Generates unverified candidates that reviewers must resolve against trusted scholarly indexes.
- 🎭 **Citation Intent Analysis:** Semantically classifies *why* a paper was cited (Supporting, Contrasting, Methodology, Background).
- ⏳ **Temporal Distribution:** Describes reference age without treating age alone as a quality defect.
- 🕸️ **Author-Overlap View:** Uses `PyVis` and `NetworkX` to map repeated normalized author names without inferring bias.
- 🛡️ **Hallucination Guard:** Cross-verifies parsed PDF text against external API data (OpenAlex/Crossref) to alert analysts to metadata inconsistencies.

---

## 🧱 System Architecture

1. **Document Parsing:** Extracts titles, abstracts, authors, and surrounding in-text citation windows (±2 sentences).
2. **Reference Canonicalization:** Normalizes raw bibliography strings into structured JSON.
3. **Metadata Enrichment:** Queries OpenAlex / Crossref APIs to fetch missing DOIs and abstracts.
4. **Scoring & Insight Generation:** Runs NLP models to generate hybrid scores and semantic insights.
5. **Reporting:** Compiles all findings into professional, downloadable PDF and HTML audit reports.

---

## 💻 Getting Started

### **1. Clone & Install Dependencies**
Ensure you are using Python 3.9+ and run the following commands:
```bash
git clone [https://github.com/your-username/CitePrism.git](https://github.com/your-username/CitePrism.git)
cd CitePrism
pip install -r requirements.txt

```

*(Note: PDF generation requires `fpdf2`. Network graphs require `pyvis` and `networkx`.)*

### **2. Set Environment Variables**

Create a `.env` file in the root directory and add your API keys (e.g., Gemini / HuggingFace):

```text
GEMINI_API_KEY=your_api_key_here

```

### **3. Run the Application**

Launch the interactive Streamlit dashboard:

```bash
streamlit run streamlit_app.py

```

---

*Built for the M.Sc. Applied Data Science Case Study.*
# Phase 1 trust foundation (optional alpha)

The Phase 0 workflow remains enabled by default. Phase 1 adds typed provenance,
PostgreSQL append-only persistence, content-addressed local/S3 artifacts, audit
verification, import/review services, and a buildable CLI package.

Start with [the Phase 1 guide](docs/PHASE_1_TRUST_KERNEL.md) and
[threat model](docs/THREAT_MODEL.md). Use `scripts/verify_phase1.ps1` for a guarded
Windows check; add `-WithPostgres` to exercise the real database. This phase does
not claim a UI overhaul, measured scoring improvement, or production authorization.
