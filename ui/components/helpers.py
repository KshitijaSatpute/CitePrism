import streamlit as st
import json
import base64
import os
from datetime import datetime

def display_pdf(pdf_path: str):
    """Display PDF in an iframe with download fallback."""
    try:
        if not os.path.exists(pdf_path):
            st.error(f"File not found: {pdf_path}")
            return

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        file_name = os.path.basename(pdf_path)

        st.info(
            "If the PDF preview does not load in the browser, use the download button below."
        )

        st.download_button(
            label="📥 Download PDF",
            data=pdf_bytes,
            file_name=file_name,
            mime="application/pdf",
            key=f"download_pdf_{file_name}",
        )

        if len(pdf_bytes) > 10 * 1024 * 1024:
            st.warning("Inline preview is disabled for PDFs larger than 10 MiB.")
            return

        base64_pdf = base64.b64encode(pdf_bytes).decode("utf-8")

        pdf_display = f"""
        <iframe
            src="data:application/pdf;base64,{base64_pdf}"
            width="100%"
            height="800px"
            type="application/pdf">
        </iframe>
        """

        st.markdown(pdf_display, unsafe_allow_html=True)

    except Exception as e:
        st.error(f"Error displaying PDF: {e}")



def _get_first(ref: dict, keys: list, default=""):
    """Return the first available value from possible key names."""
    for key in keys:
        value = ref.get(key)
        if value not in (None, "", []):
            return value
    return default


def _safe_float(value, default=None):
    """Convert score values safely."""
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return default


def _format_risk_flags(ref: dict) -> str:
    """Create readable risk flag text from different possible risk fields."""
    risks = _get_first(
        ref,
        ["quality_flags", "risk_flags", "risks", "risk_labels", "detected_risks", "risk_summary"],
        default=[]
    )

    if isinstance(risks, list):
        return ", ".join(str(r) for r in risks) if risks else "None"

    if isinstance(risks, dict):
        active = [k for k, v in risks.items() if v]
        return ", ".join(active) if active else "None"

    return str(risks) if risks else "None"


def display_ranked_citation_review(json_data: dict):
    """
    Display reviewer-friendly ranked citation results.

    Risky / low-confidence citations are shown first so reviewers can focus
    on the most important cases instead of reading raw JSON.
    """
    scored_refs = json_data.get("scored_references", [])

    if not scored_refs:
        return

    rows = []

    for idx, ref in enumerate(scored_refs, start=1):
        marker = _get_first(
            ref,
            ["ref_id", "marker", "citation_marker", "in_text_marker", "citation"],
            default=f"Ref {idx}"
        )

        ext_meta = ref.get("external_metadata", {})
        title = (
            ext_meta.get("title")
            or _get_first(ref, ["title", "reference_title", "paper_title"], default="Untitled reference")
        )

        ext_authors = ext_meta.get("authors")

        if ext_authors:
            if isinstance(ext_authors, list):
                authors = ", ".join(
                    a.get("display_name", str(a)) if isinstance(a, dict) else str(a)
                    for a in ext_authors[:3]
                )
            else:
                authors = str(ext_authors)
        else:
            authors = _get_first(
                ref,
                ["authors", "author", "reference_authors"],
                default=""
            )

        if isinstance(authors, list):
                authors = ", ".join(str(a) for a in authors[:3])

        final_score = _safe_float(
            _get_first(
                ref,
                ["RS_final", "final_score", "combined_score", "relevance_score", "score"],
                default=None
            )
        )

        label = str(
            _get_first(
                ref,
                ["label", "relevance_label", "classification", "decision"],
                default="Unknown"
            )
        )

        explanation = _get_first(
            ref,
            ["llm_rationale", "explanation", "reason", "llm_explanation", "judgement", "ai_judgement"],
            default=""
        )

        risk_flags = _format_risk_flags(ref)

        label_lower = label.lower()

        if "irrelevant" in label_lower:
            priority = 1
        elif "borderline" in label_lower:
            priority = 2
        elif risk_flags.lower() != "none":
            priority = 3
        else:
            priority = 4

        rows.append(
            {
                "Review Priority": (
    "High" if priority == 1
    else "Medium" if priority == 2
    else "Risk" if priority == 3
    else "Normal"
),
"Sort Priority": priority,
                "Citation": marker,
                "Reference": title,
                "Authors": authors,
                "Final Score": final_score,
                "Label": label,
                "Risk Flags": risk_flags,
                "Explanation": str(explanation)[:220],
                "Intent": _get_first(ref, ["citation_intent", "intent"], default="")
            }
        )

    rows = sorted(
        rows,
        key=lambda x: (
            x["Sort Priority"],
            x["Final Score"] is None,
            x["Final Score"] if x["Final Score"] is not None else 101,
        ),
    )

    st.markdown("### 🚩 Ranked Citation Review")

    label_filter = st.selectbox(
        "Filter citations",
        ["All", "Irrelevant", "Borderline", "Relevant", "With Risk Flags"],
        key="citation_review_filter",
    )

    filtered_rows = rows

    if label_filter != "All":
        if label_filter == "With Risk Flags":
            filtered_rows = [
                r for r in rows
                if r["Risk Flags"] and r["Risk Flags"].lower() != "none"
            ]
        else:
            filtered_rows = [
                r for r in rows
                if label_filter.lower() in str(r["Label"]).lower()
            ]

    st.caption(
        "Citations are sorted so irrelevant, borderline, risky, and low-score cases appear first."
    )

    display_rows = [
        {k: v for k, v in row.items() if k != "Sort Priority"}
        for row in filtered_rows
    ]

    st.dataframe(
    display_rows,
    width="stretch",
    hide_index=True,
    )

def display_json_pretty(json_data: dict, title: str = "JSON Data"):
    """Display JSON data with syntax highlighting and download button."""
    st.markdown(f"### {title}")

    # --- NEW: Calculate and Display Total Reference Count ---
    total_refs = 0
    if 'scored_references' in json_data:
        total_refs = len(json_data['scored_references'])
    elif 'enriched_references' in json_data:
        total_refs = len(json_data['enriched_references'])
    elif 'references_list' in json_data:
        total_refs = len(json_data['references_list'])

    if total_refs > 0:
        st.info(f"📚 **Total References Extracted:** {total_refs}")
    # --------------------------------------------------------

    with st.expander("📄 Manuscript Metadata", expanded=True):
        st.json(json_data.get('manuscript_metadata') or json_data.get('metadata', {}))

    if 'citations_in_text' in json_data:
        with st.expander(f"📎 In-Text Citations ({len(json_data['citations_in_text'])})", expanded=False):
            st.json(json_data['citations_in_text'])

    if 'references_list' in json_data:
        with st.expander(f"📚 Parsed References ({len(json_data['references_list'])})", expanded=False):
            st.json(json_data['references_list'])

    if 'enriched_references' in json_data:
        with st.expander(f"🔍 External Enrichment ({len(json_data['enriched_references'])})", expanded=False):
            st.json(json_data['enriched_references'])

    if 'scored_references' in json_data:
        with st.expander(
            f"⭐ Raw Scored Results JSON ({len(json_data['scored_references'])})",
            expanded=False
        ):
            st.json(json_data['scored_references'])
    json_str = json.dumps(json_data, indent=2)
    st.download_button(
        "📥 Download Analysis JSON",
        data=json_str,
        file_name=f"citeprism_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        mime="application/json"
    )

def show_pipeline_progress(stages_completed: dict):
    """Display pipeline progress icons."""
    st.markdown("### Pipeline Status")
    stages = [
        ('Parsed', stages_completed.get('parsed', False)),
        ('Enriched', stages_completed.get('enriched', False)),
        ('Scored', stages_completed.get('scored', False))
    ]
    cols = st.columns(3)
    for col, (stage_name, is_complete) in zip(cols, stages):
        with col:
            if is_complete:
                st.success(f"✅ {stage_name}")
            else:
                st.info(f"⏳ {stage_name}")
