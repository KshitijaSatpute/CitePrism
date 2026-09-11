"""
CitePrism Streamlit Application
================================
Main entry point for the CitePrism citation audit pipeline.

Features:
- Modern welcome page
- Modular audit workflow
- Manuscript-level Reference Risk Summary
- Cached audit loading
- Improved empty states and navigation
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import streamlit as st

# Add project root to path to ensure modules are found.
project_root = Path(__file__).parent
sys.path.append(str(project_root))

# Initialize logging before importing application modules.
from src.utils.logger import setup_logger

setup_logger()

from config.settings import Config
from database_manager import DatabaseManager
from pipeline_orchestrator import PipelineOrchestrator
from src.trust.factory import build_trust_kernel

# Modular UI components.
from ui.tabs.tab_1_upload import render_upload_tab
from ui.components.evidence_workspace import render_workspace

logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="CitePrism | Editorial Citation Audit",
    page_icon="🔷",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Global styling
# ---------------------------------------------------------------------------

st.markdown("""<style>
.block-container {max-width:1400px;padding-top:2rem;padding-bottom:4rem;}
div[data-testid="stMetric"] {border:1px solid #8090a044;border-radius:10px;padding:14px;}
</style>""", unsafe_allow_html=True)


def init_session_state() -> None:
    """Initialize application-wide session state."""

    Config.ensure_directories()
    if "db_manager" not in st.session_state:
        st.session_state.db_manager = DatabaseManager(Config.DATABASE_PATH)

    if "config" not in st.session_state:
        st.session_state.config = Config()

    if "trust_kernel" not in st.session_state:
        st.session_state.trust_kernel = build_trust_kernel(st.session_state.config)

    if "pipeline" not in st.session_state:
        st.session_state.pipeline = PipelineOrchestrator(
            st.session_state.db_manager,
            st.session_state.config,
            trust_kernel=st.session_state.trust_kernel,
        )

    defaults = {
        "current_document_id": None,
        "app_started": False,
        "active_tab": "New audit",
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _normalize_existing_path(path_value: Optional[str]) -> Optional[Path]:
    """Resolve a stored path if it exists in the current project."""

    if not path_value:
        return None

    candidate = Path(str(path_value).replace("\\", "/"))

    if candidate.exists():
        return candidate

    project_candidate = project_root / candidate
    if project_candidate.exists():
        return project_candidate

    return None


def load_selected_scored_data() -> Optional[Dict[str, Any]]:
    """Load the scored JSON for the selected manuscript."""

    document_id = st.session_state.current_document_id
    if not document_id:
        return None

    try:
        status = st.session_state.db_manager.get_document_status(document_id)
    except Exception as exc:
        logger.warning("Could not read document status: %s", exc)
        return None

    if not status:
        return None

    scored_path = _normalize_existing_path(status.get("scored_path"))
    if not scored_path:
        return None

    try:
        with scored_path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load scored audit JSON: %s", exc)
        return None


def render_welcome_page() -> None:
    st.title("CitePrism")
    st.subheader("Review the evidence behind a manuscript's citations")
    st.write("Upload a PDF, inspect unresolved sources, and compare citation claims with located source quotations.")
    first, second, third = st.columns(3)
    first.info("1. Run an audit\n\nExtract the bibliography and locate citation occurrences on manuscript pages.")
    second.info("2. Review the evidence\n\nCheck source identity, metadata differences and model proposals.")
    third.info("3. Export the review\n\nShare a report with the same counts and evidence shown in the workspace.")
    st.caption("Current scope: text-based PDFs and retrieved abstracts. Human interpretation review is required.")
    if st.button("Open workspace", type="primary"):
        st.session_state.app_started = True
        st.session_state.active_tab = "New audit"
        st.rerun()


def render_main_app() -> None:
    st.title("CitePrism")
    st.caption("Citation evidence review")
    with st.sidebar:
        st.subheader("Manuscripts")
        documents = st.session_state.db_manager.list_all_documents() or []
        if documents:
            options = {d["id"]: d.get("title") or d.get("display_filename") or f"Manuscript {d['id']}" for d in documents}
            current = st.session_state.current_document_id
            if current not in options:
                current = None
            if st.session_state.get("sidebar_doc_selector") != current:
                st.session_state.sidebar_doc_selector = current
            def select_document():
                st.session_state.current_document_id = st.session_state.sidebar_doc_selector
                st.session_state.active_tab = "Review"
                st.session_state.evidence_navigation = "Review"
            st.selectbox("Active manuscript", list(options), index=None, format_func=options.get,
                         key="sidebar_doc_selector", on_change=select_document, placeholder="Choose a manuscript")
        else:
            st.info("Your audited manuscripts will appear here.")
        def new_audit():
            st.session_state.active_tab = "New audit"
            st.session_state.evidence_navigation = "New audit"
        st.button("New audit", on_click=new_audit, type="primary", width="stretch")
        st.divider()
        st.caption("An audit is an evidence review aid. It does not certify a manuscript or establish misconduct.")
    tabs = ["New audit", "Review", "Evidence", "Diagnostics"]
    if st.session_state.active_tab not in tabs:
        st.session_state.active_tab = "New audit"
    if st.session_state.get("evidence_navigation") != st.session_state.active_tab:
        st.session_state.evidence_navigation = st.session_state.active_tab
    def navigate():
        st.session_state.active_tab = st.session_state.evidence_navigation
    st.radio("Workspace", tabs, horizontal=True, key="evidence_navigation", on_change=navigate)
    st.divider()
    if st.session_state.active_tab == "New audit":
        render_upload_tab()
    else:
        files = st.session_state.pipeline.get_document_files(st.session_state.current_document_id) if st.session_state.current_document_id else {}
        render_workspace(load_selected_scored_data(), st.session_state.active_tab, files.get("pdf"))


def main() -> None:
    init_session_state()

    if st.session_state.app_started:
        render_main_app()
    else:
        render_welcome_page()


if __name__ == "__main__":
    main()
