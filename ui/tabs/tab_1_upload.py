import streamlit as st
import os
import logging
import tempfile
from pathlib import Path

# Resolve project root dynamically
PROJECT_ROOT = Path(__file__).parent.parent.parent


# ── Level colours (light-theme friendly) ──────────────────────────────────────
_LEVEL_COLOUR = {
    "DEBUG":    "#888888",
    "INFO":     "#1a1a2e",
    "WARNING":  "#b45309",
    "ERROR":    "#b91c1c",
    "CRITICAL": "#7f1d1d",
}

_LEVEL_PREFIX = {
    "DEBUG":    "⚙",
    "INFO":     "ℹ",
    "WARNING":  "⚠",
    "ERROR":    "✖",
    "CRITICAL": "🔥",
}

_BOX_CSS = (
    "background:#f8f9fa;"
    "border:1px solid #dee2e6;"
    "border-radius:8px;"
    "padding:12px 16px;"
    "height:320px;"
    "overflow-y:scroll;"
    "scrollbar-width:thin;"
    "scrollbar-color:#adb5bd #f8f9fa;"
    "font-family:'Courier New',monospace;"
    "font-size:0.78rem;"
    "line-height:1.55;"
)


def _build_html(logs):
    lines = ""
    for entry in logs:
        colour = _LEVEL_COLOUR.get(entry["level"], "#1a1a2e")
        prefix = _LEVEL_PREFIX.get(entry["level"], "•")
        safe = (
            entry["msg"]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        lines += (
            f'<div style="color:{colour};margin:2px 0;">'
            f'<span style="opacity:0.45;">{prefix}</span> {safe}'
            f'</div>\n'
        )

    return (
        "<style>"
        "  .citeprism-logbox::-webkit-scrollbar{width:8px}"
        "  .citeprism-logbox::-webkit-scrollbar-track{background:#e9ecef;border-radius:4px}"
        "  .citeprism-logbox::-webkit-scrollbar-thumb{background:#adb5bd;border-radius:4px}"
        "  .citeprism-logbox::-webkit-scrollbar-thumb:hover{background:#6c757d}"
        "</style>"
        f'<div class="citeprism-logbox" style="{_BOX_CSS}">'
        f"{lines}"
        '<div id="cplog-end"></div>'
        "</div>"
        "<script>"
        "(function(){var e=document.getElementById('cplog-end');if(e)e.scrollIntoView({behavior:'instant'});})();"
        "</script>"
    )


def _empty_box(msg="Logs will appear here once the audit starts..."):
    return (
        f'<div class="citeprism-logbox" style="{_BOX_CSS}color:#adb5bd;font-style:italic;">'
        f"{msg}</div>"
    )


# ── Live-flushing log handler ──────────────────────────────────────────────────

class _LiveLogHandler(logging.Handler):
    def __init__(self, placeholder):
        super().__init__()
        self._placeholder = placeholder

    def emit(self, record):
        noisy_loggers = (
            "urllib3",
            "httpx",
            "httpcore",
            "sentence_transformers",
            "transformers",
            "huggingface_hub",
            "pyalex",
        )

        # Hide noisy DEBUG/INFO logs from UI, but keep warnings/errors.
        if record.name.startswith(noisy_loggers) and record.levelno < logging.WARNING:
            return

        # Hide generic DEBUG logs from UI.
        if record.levelno == logging.DEBUG:
            return

        if "ui_logs" not in st.session_state:
            st.session_state.ui_logs = []

        st.session_state.ui_logs.append({
            "level": record.levelname,
            "msg": self.format(record),
        })

        try:
            self._placeholder.markdown(
                _build_html(st.session_state.ui_logs),
                unsafe_allow_html=True,
            )
        except Exception:
            pass

def _attach(placeholder):
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h, _LiveLogHandler)]
    handler = _LiveLogHandler(placeholder)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)
    return handler


def _detach(handler):
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h is not handler]

def _render_runtime_summary(results):
    """Display pipeline runtime metrics in the Streamlit UI."""
    timings = results.get("timings", {})

    if not timings:
        return

    st.markdown("### ⏱ Runtime Summary")

    total_time = timings.get("total_pipeline_sec", 0)
    parsing_time = timings.get("parsing_sec", 0)
    enrichment_time = timings.get("enrichment_sec", 0)
    scoring_group_time = timings.get("scoring_group_sec", 0)
    missing_time = timings.get("missing_citations_sec", None)

    audit_mode = "Full Audit" if missing_time is not None else "Fast Audit"

    if missing_time is not None:
        scoring_risk_time = max(0, round(scoring_group_time - missing_time, 2))
        missing_display = f"{missing_time} sec"
    else:
        scoring_risk_time = scoring_group_time
        missing_display = "Skipped"

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Pipeline", f"{total_time} sec")
    col2.metric("Audit Mode", audit_mode)
    col3.metric("Missing Citations", missing_display)

    col4, col5, col6 = st.columns(3)
    col4.metric("Parsing", f"{parsing_time} sec")
    col5.metric("Enrichment", f"{enrichment_time} sec")
    col6.metric("Scoring + Risk", f"{scoring_risk_time} sec")
# ── Main tab renderer ──────────────────────────────────────────────────────────

def render_processing_failure(results):
    failure = results.get("provider_failure", {})
    if failure.get("retryable"):
        st.warning("The model provider could not finish this request. The audit is incomplete.")
        for message in results.get("errors", []):
            st.write(message)
        st.info("Wait a few minutes, then click Run citation audit again with the same PDF. "
                "Completed extraction requests are saved and reused when the PDF and model settings match. "
                "You can leave the advanced options unchecked.")
    else:
        st.error("Processing could not finish.")
        for message in results.get("errors", []):
            st.error(message)

def render_upload_tab():
    from ui.components.evidence_workspace import show_review
    st.header("New manuscript audit")
    st.write("Upload a text-based PDF. The audit preserves each reference entry, resolves source identities and records evidence for review.")
    if st.session_state.get("last_pipeline_results", {}).get("success"):
        st.button("Open completed review", on_click=show_review, type="primary")
    uploaded_file = st.file_uploader("Choose a research paper (PDF)", type=["pdf"])

    if not uploaded_file:
        return

    with st.expander("Advanced processing options"):
        st.caption("Completed stages are reused when their inputs and method versions match. Re-extraction also refreshes downstream stages.")
        force_parse = st.checkbox("Extract this PDF again")
        force_enrich = st.checkbox("Refresh source metadata")
        force_score = st.checkbox("Retry claim assessments")
        force_all = False
        run_missing_citations = st.checkbox("Generate unverified literature search leads", value=False,
            help="Optional extra model call. These are search leads and are not verified publications.")

    if run_missing_citations and not force_score:
        st.info("Missing citation suggestions require scoring, so Re-Score will run automatically.")
        force_score = True


    st.markdown("<br>", unsafe_allow_html=True)

    # Button ABOVE the log box
    start_clicked = st.button("Run citation audit", type="primary")

    # Log box
    with st.expander("Technical log", expanded=False):
        log_placeholder = st.empty()

    existing_logs = st.session_state.get("ui_logs", [])
    if existing_logs:
        log_placeholder.markdown(_build_html(existing_logs), unsafe_allow_html=True)
    else:
        log_placeholder.markdown(_empty_box(), unsafe_allow_html=True)

    if not start_clicked:
        return

    # Keep untrusted bytes outside the durable raw store. The orchestrator
    # validates and content-addresses the PDF before database registration.
    staging_dir = PROJECT_ROOT / "cache" / "uploads"
    staging_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".pdf"
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=suffix, dir=staging_dir, delete=False
    ) as staged_file:
        staged_file.write(uploaded_file.getbuffer())
        temp_pdf_path = Path(staged_file.name)

    # Reset and wire up live handler before pipeline starts
    st.session_state.ui_logs = []
    log_placeholder.markdown(_empty_box("Starting audit..."), unsafe_allow_html=True)
    ui_handler = _attach(log_placeholder)

    print("\n" + "=" * 60)
    print(f"🚀 STARTING CITEPRISM AUDIT FOR: {uploaded_file.name}")
    print("=" * 60)

    progress_bar = st.progress(0, text="Initializing Audit Pipeline...")

    try:
        results = st.session_state.pipeline.process_document(
            temp_pdf_path,
            progress_bar=progress_bar,
            force_parse=force_parse or force_all,
            force_enrich=force_enrich or force_all,
            force_score=force_score or force_all,
            run_missing_citations=run_missing_citations,
            original_filename=uploaded_file.name,
        )

        if results["success"]:
            progress_bar.progress(1.0, text="Processing finished. Review the evidence and unresolved items.")
            if results.get("processing_status") == "completed_with_issues":
                st.warning("Processing finished with unresolved items. Open Review for the next actions.")
            else:
                st.success("Processing finished. The evidence is ready for human review.")

            st.session_state.current_document_id = results["document_id"]
            st.session_state.last_pipeline_results = results

            _render_runtime_summary(results)

            st.button("Review this manuscript", on_click=show_review, type="primary")

            print(f"✅ AUDIT COMPLETE: {uploaded_file.name}")
            print(f"   Document ID: {results['document_id']}")
            print("=" * 60 + "\n")

        else:
            retryable = results.get("provider_failure", {}).get("retryable", False)
            progress_bar.progress(0.0, text="Processing paused. Retry when the provider is available."
                                  if retryable else "Processing stopped. Resolve the error and retry.")
            render_processing_failure(results)
            print(f"❌ AUDIT FAILED: {uploaded_file.name}")
            for err in results.get("errors", []):
                print(f"   ERROR: {err}")
            print("=" * 60 + "\n")

    finally:
        _detach(ui_handler)
        temp_pdf_path.unlink(missing_ok=True)
