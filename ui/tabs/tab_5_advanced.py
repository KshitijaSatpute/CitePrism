import streamlit as st
import streamlit.components.v1 as components
import json
import os
import tempfile
from html import escape
from pathlib import Path
from src.utils.scoring import is_clean, is_flagged, numeric_score
import pandas as pd
from datetime import datetime
import networkx as nx
from pyvis.network import Network
import plotly.express as px

try:
    from fpdf import FPDF
    HAS_FPDF = True
except ImportError:
    HAS_FPDF = False

# --- 1. FIXED HTML REPORT GENERATOR ---
def generate_html_report(manuscript_title, tau, kpis, all_refs, missing_citations, cliques=None):
    manuscript_title = escape(str(manuscript_title))
    flagged_rows = ""
    clean_rows = ""
    unavailable_rows = ""

    # --- Calculate Advanced Insights ---
    intents = [r.get('citation_intent', 'Background') for r in all_refs]
    total_intents = len(intents) if intents else 1
    supp_pct = (intents.count('Supporting') / total_intents) * 100
    back_pct = (intents.count('Background') / total_intents) * 100
    cont_pct = (intents.count('Contrasting') / total_intents) * 100
    meth_pct = (intents.count('Methodology') / total_intents) * 100

    if back_pct > 70:
        intent_insight_html = "ℹ️ <strong>Background-heavy distribution:</strong> Over 70% of citations were classified as background. Interpretation depends on manuscript type and discipline."
    elif cont_pct > 20:
        intent_insight_html = "ℹ️ <strong>Contrast-heavy distribution:</strong> More than 20% were classified as contrasting; reviewers should inspect the cited passages."
    else:
        intent_insight_html = "ℹ️ Intent labels are descriptive model outputs and require reviewer verification."

    current_year = datetime.now().year
    years = []
    for r in all_refs:
        year = r.get('external_metadata', {}).get('publication_year') or r.get('external_metadata', {}).get('year') or r.get('original_data', {}).get('parsed', {}).get('year')
        if year:
            try:
                years.append(int("".join(filter(str.isdigit, str(year)))[:4]))
            except ValueError:
                pass

    total_years = len(years) if years else 1
    if years:
        older_pct = (sum(1 for y in years if current_year - y > 10) / total_years) * 100
        recent_pct = (sum(1 for y in years if current_year - y <= 5) / total_years) * 100

        if older_pct > 40:
            temporal_insight_html = f"ℹ️ {older_pct:.1f}% of references are over 10 years old. Age alone does not determine relevance or quality."
        elif recent_pct > 60:
            temporal_insight_html = f"ℹ️ {recent_pct:.1f}% of references are from the last 5 years. Review currency in disciplinary context."
        else:
            temporal_insight_html = f"ℹ️ {recent_pct:.1f}% of references were published in the last 5 years."
    else:
        older_pct = recent_pct = 0
        temporal_insight_html = "No publication year data could be extracted for these references."

    for ref in all_refs:
        title = escape(str(ref.get('original_data', {}).get('parsed', {}).get('title', 'Unknown Title')))
        score = numeric_score(ref)
        score_display = score if score is not None else "Unavailable"
        rationale = escape(str(ref.get('llm_rationale', 'No rationale provided.')))
        badges = []
        if ref.get('self_citation', {}).get('is_self_cite'): badges.append("👤 Self-Cite")
        if ref.get('quality_flags'): badges.append("⚠️ Issue")
        badge_str = " | ".join(badges) if badges else "None"

        row_html = f"""
        <tr>
            <td>{escape(str(ref.get('ref_id', '')))}</td>
            <td><strong>{title}</strong></td>
            <td style="font-weight: bold;">{score_display}</td>
            <td>{badge_str}</td>
            <td>{rationale}</td>
        </tr>
        """
        if score is None:
            unavailable_rows += row_html
        elif score < tau:
            flagged_rows += row_html
        else:
            clean_rows += row_html

    clique_html = ""
    if not cliques:
        clique_html = "<p>No repeated-author groups were detected by exact normalized-name matching.</p>"
    else:
        clique_html = "<p><strong>Repeated-author groups:</strong> This is descriptive overlap and is not evidence of bias.</p><ul>"
        for author, refs in cliques.items():
            clique_html += (
                f"<li><strong>{escape(str(author))}</strong>: Cited in References "
                f"[{escape(', '.join(map(str, refs)))}]</li>"
            )
        clique_html += "</ul>"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>CitePrism Audit Report</title>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 40px; color: #333; }}
            h1 {{ color: #1f77b4; border-bottom: 2px solid #1f77b4; padding-bottom: 10px; }}
            h2 {{ color: #555; margin-bottom: 5px; }}
            h3 {{ color: #1f77b4; margin-top: 30px; border-bottom: 1px solid #eee; padding-bottom: 5px; }}
            .meta-info {{ font-size: 14px; color: #666; margin-bottom: 20px; }}
            .kpi-container {{ display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 30px; }}
            .kpi-box {{ flex: 1; min-width: 150px; padding: 15px; background: #f8f9fa; border-radius: 8px; border: 1px solid #dee2e6; text-align: center; }}
            .kpi-value {{ font-size: 24px; font-weight: bold; color: #1f77b4; }}
            .insights-container {{ display: flex; gap: 20px; margin-bottom: 30px; }}
            .insight-box {{ flex: 1; background: #f8f9fa; padding: 20px; border-radius: 8px; border: 1px solid #dee2e6; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom: 30px;}}
            th, td {{ padding: 12px; border: 1px solid #ddd; text-align: left; }}
            th {{ background-color: #1f77b4; color: white; }}
            tr:nth-child(even) {{ background-color: #f9f9f9; }}
            .clique-box {{ background-color: #fff3cd; border-left: 5px solid #ffc107; padding: 15px; margin-bottom: 30px; border-radius: 8px; }}
        </style>
    </head>
    <body>
        <h1>CitePrism Citation Audit Report</h1>
        <h2>Manuscript: {manuscript_title}</h2>
        <div class="meta-info">
            <strong>Date Generated:</strong> {datetime.now().strftime('%B %d, %Y - %H:%M:%S')}<br>
            <strong>Total References Audited:</strong> {kpis.get('total', 0)} references
        </div>

        <div class="kpi-container">
            <div class="kpi-box"><div>Threshold (τ)</div><div class="kpi-value">{tau}</div></div>
            <div class="kpi-box"><div>Total References</div><div class="kpi-value">{kpis.get('total', 0)}</div></div>
            <div class="kpi-box"><div>Below Threshold</div><div class="kpi-value" style="color: #dc3545;">{kpis.get('flagged', 0)}</div></div>
            <div class="kpi-box"><div>Score Unavailable</div><div class="kpi-value">{kpis.get('unavailable', 0)}</div></div>
            <div class="kpi-box"><div>Self-Citations</div><div class="kpi-value">{kpis.get('self_cites', 0)}</div></div>
            <div class="kpi-box"><div>Quality Issues</div><div class="kpi-value" style="color: #ffc107;">{kpis.get('issues', 0)}</div></div>
        </div>

        <h3>📊 Advanced Semantic & Temporal Insights</h3>
        <div class="insights-container">
            <div class="insight-box">
                <h4 style="margin-top: 0; color: #1f77b4;">🎭 Citation Intent Breakdown</h4>
                <ul style="color: #555;">
                    <li><strong>Supporting:</strong> {supp_pct:.1f}%</li>
                    <li><strong>Background:</strong> {back_pct:.1f}%</li>
                    <li><strong>Contrasting:</strong> {cont_pct:.1f}%</li>
                    <li><strong>Methodology:</strong> {meth_pct:.1f}%</li>
                </ul>
                <p>{intent_insight_html}</p>
            </div>
            <div class="insight-box">
                <h4 style="margin-top: 0; color: #1f77b4;">⏳ Temporal Currency (Age)</h4>
                <ul style="color: #555;">
                    <li><strong>Recent (&le; 5 years):</strong> {recent_pct:.1f}%</li>
                    <li><strong>Older (> 10 years):</strong> {older_pct:.1f}%</li>
                </ul>
                <p>{temporal_insight_html}</p>
            </div>
        </div>

        <h3>🕸️ Descriptive Author Overlap</h3>
        <div class="clique-box">
            {clique_html}
        </div>

        <h3 style="color: #dc3545; border-bottom: 1px solid #dc3545;">🚩 Flagged References (Score < {tau})</h3>
        <table>
            <thead>
                <tr style="background-color: #dc3545;">
                    <th width="5%">ID</th>
                    <th width="30%">Reference Title</th>
                    <th width="10%">Score</th>
                    <th width="15%">Badges</th>
                    <th width="40%">LLM Rationale</th>
                </tr>
            </thead>
            <tbody>
                {flagged_rows if flagged_rows else "<tr><td colspan='5' style='text-align:center;'>No flagged references found.</td></tr>"}
            </tbody>
        </table>

        <h3>⚪ References without a score</h3>
        <table>
            <thead><tr><th>ID</th><th>Reference Title</th><th>Score</th><th>Badges</th><th>Rationale</th></tr></thead>
            <tbody>{unavailable_rows if unavailable_rows else "<tr><td colspan='5'>All references received a score.</td></tr>"}</tbody>
        </table>

        <h3 style="color: #28a745; border-bottom: 1px solid #28a745;">References at or above threshold (Score &ge; {tau})</h3>
        <table>
            <thead>
                <tr style="background-color: #28a745;">
                    <th width="5%">ID</th>
                    <th width="30%">Reference Title</th>
                    <th width="10%">Score</th>
                    <th width="15%">Badges</th>
                    <th width="40%">LLM Rationale</th>
                </tr>
            </thead>
            <tbody>
                {clean_rows if clean_rows else "<tr><td colspan='5' style='text-align:center;'>No references were at or above threshold.</td></tr>"}
            </tbody>
        </table>
    </body>
    </html>
    """
    return html_content

# --- 2. FIXED PDF REPORT GENERATOR ---
def generate_pdf_report(manuscript_title, tau, kpis, all_refs, missing_citations, cliques=None):
    class PDF(FPDF):
        def header(self):
            self.set_font("Helvetica", "B", 16)
            self.set_text_color(31, 119, 180)
            self.cell(0, 10, "CitePrism Citation Audit Report", border=False, ln=True, align="C")
            self.ln(5)

        def footer(self):
            self.set_y(-15)
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(128)
            self.cell(0, 10, f"Page {self.page_no()}", align="C")

    pdf = PDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    def clean_text(text):
        if not text: return "N/A"
        return str(text).encode('latin-1', 'replace').decode('latin-1')

    intents = [r.get('citation_intent', 'Background') for r in all_refs]
    total_intents = len(intents) if intents else 1
    supp_pct = (intents.count('Supporting') / total_intents) * 100
    back_pct = (intents.count('Background') / total_intents) * 100
    cont_pct = (intents.count('Contrasting') / total_intents) * 100
    meth_pct = (intents.count('Methodology') / total_intents) * 100

    if back_pct > 70:
        intent_insight_text = "Background-heavy distribution (>70%); interpret by manuscript type and discipline."
    elif cont_pct > 20:
        intent_insight_text = "Contrast-heavy distribution (>20%); inspect cited passages before interpretation."
    else:
        intent_insight_text = "Intent labels are descriptive model outputs requiring reviewer verification."

    current_year = datetime.now().year
    years = []
    for r in all_refs:
        year = r.get('external_metadata', {}).get('publication_year') or r.get('external_metadata', {}).get('year') or r.get('original_data', {}).get('parsed', {}).get('year')
        if year:
            try:
                years.append(int("".join(filter(str.isdigit, str(year)))[:4]))
            except ValueError:
                pass

    total_years = len(years) if years else 1
    if years:
        older_pct = (sum(1 for y in years if current_year - y > 10) / total_years) * 100
        recent_pct = (sum(1 for y in years if current_year - y <= 5) / total_years) * 100
        if older_pct > 40:
            temporal_insight_text = f"{older_pct:.1f}% are over 10 years old; age alone does not determine quality."
        elif recent_pct > 60:
            temporal_insight_text = f"{recent_pct:.1f}% are from the last 5 years; interpret currency by discipline."
        else:
            temporal_insight_text = f"{recent_pct:.1f}% were published in the last 5 years."
    else:
        older_pct = recent_pct = 0
        temporal_insight_text = "No publication year data could be extracted for these references."

    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(50)
    pdf.cell(0, 8, f"Manuscript: {clean_text(manuscript_title)}", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Date Generated: {datetime.now().strftime('%B %d, %Y - %H:%M:%S')}", ln=True)
    pdf.ln(5)

    if kpis:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Audit KPIs", ln=True)
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 6, f"- Total References Found: {kpis.get('total', 0)}", ln=True)
        total_refs = max(int(kpis.get('total', 0) or 0), 1)
        flagged_count = kpis.get('flagged', 0)
        pdf.cell(0, 6, f"- Below Threshold: {flagged_count} ({(flagged_count/total_refs*100):.1f}% of total)", ln=True)
        pdf.cell(0, 6, f"- Score Unavailable: {kpis.get('unavailable', 0)}", ln=True)
        pdf.cell(0, 6, f"- Total Self-Citations: {kpis.get('self_cites', 0)}", ln=True)
        pdf.ln(5)

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(31, 119, 180)
    pdf.cell(0, 10, "Advanced Semantic & Temporal Insights", ln=True)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(0)
    pdf.cell(0, 6, "1. Citation Intent Breakdown:", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"- Supporting: {supp_pct:.1f}% | Background: {back_pct:.1f}% | Contrasting: {cont_pct:.1f}% | Methodology: {meth_pct:.1f}%", ln=True)
    pdf.set_font("Helvetica", "I", 10)
    pdf.multi_cell(0, 6, f"AI Insight: {intent_insight_text}")
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "2. Temporal Currency (Age) Analysis:", ln=True)
    pdf.set_font("Helvetica", "", 10)
    if years:
        pdf.cell(0, 6, f"- Recent (<= 5 years): {recent_pct:.1f}% | Older (> 10 years): {older_pct:.1f}%", ln=True)
    pdf.set_font("Helvetica", "I", 10)
    pdf.multi_cell(0, 6, f"AI Insight: {temporal_insight_text}")
    pdf.ln(5)

    if missing_citations:
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_text_color(31, 119, 180)
        pdf.cell(0, 10, "Unverified Literature-Search Candidates", ln=True)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(3)
        pdf.set_text_color(0)
        for paper in missing_citations:
            pdf.set_font("Helvetica", "B", 11)
            pdf.ln(2)
            pdf.multi_cell(0, 6, f"Title: {clean_text(paper.get('title'))}")
            pdf.set_font("Helvetica", "I", 10)
            pdf.cell(0, 6, f"Authors: {clean_text(paper.get('authors'))} | Year: {paper.get('year')}", ln=True)
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5, f"AI Rationale: {clean_text(paper.get('rationale'))}")
            pdf.ln(4)
        pdf.ln(5)

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(31, 119, 180)
    pdf.cell(0, 10, "Descriptive Author Overlap", ln=True)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)
    pdf.set_text_color(0)
    if not cliques:
        pdf.set_font("Helvetica", "I", 11)
        pdf.cell(0, 10, "No repeated-author groups were detected by normalized-name matching.", ln=True)
    else:
        pdf.set_font("Helvetica", "", 11)
        pdf.multi_cell(0, 6, "Repeated-author groups (descriptive only; not evidence of bias):")
        pdf.ln(2)
        for author, refs in cliques.items():
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(45, 6, clean_text(f"{author}:"))
            pdf.set_font("Helvetica", "", 11)
            pdf.cell(0, 6, f"Cited in Refs [{', '.join(refs)}]", ln=True)
    pdf.ln(8)

    flagged_refs = [r for r in all_refs if is_flagged(r, tau)]
    clean_refs = [r for r in all_refs if is_clean(r, tau)]
    unavailable_refs = [r for r in all_refs if numeric_score(r) is None]

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(220, 53, 69)
    pdf.cell(0, 10, f"[FLAGGED] Flagged References (Score < {tau})", ln=True)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(5)
    pdf.set_text_color(0)
    if not flagged_refs:
        pdf.set_font("Helvetica", "I", 11)
        pdf.cell(0, 10, "No flagged references found.", ln=True)
    else:
        for ref in flagged_refs:
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 11)
            title_text = clean_text(f"Ref [{ref.get('ref_id')}]: {ref.get('original_data', {}).get('parsed', {}).get('title')}")
            pdf.multi_cell(0, 6, title_text)
            pdf.set_font("Helvetica", "", 10)
            score_rationale = f"Score: {ref.get('RS_final')} | Rationale: {clean_text(ref.get('llm_rationale'))}"
            pdf.ln(1)
            pdf.multi_cell(0, 5, score_rationale)
            pdf.ln(3)

    pdf.ln(5)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(90)
    pdf.cell(0, 10, "[UNAVAILABLE] References without a score", ln=True)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(5)
    pdf.set_text_color(0)
    if not unavailable_refs:
        pdf.set_font("Helvetica", "I", 11)
        pdf.cell(0, 10, "All references received a score.", ln=True)
    else:
        for ref in unavailable_refs:
            pdf.set_font("Helvetica", "B", 11)
            pdf.multi_cell(0, 6, clean_text(
                f"Ref [{ref.get('ref_id')}]: "
                f"{ref.get('original_data', {}).get('parsed', {}).get('title')}"
            ))
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5, clean_text(ref.get('llm_rationale')))
            pdf.ln(3)

    pdf.ln(5)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(40, 167, 69)
    pdf.cell(0, 10, f"References at or above threshold (Score >= {tau})", ln=True)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(5)
    pdf.set_text_color(0)
    if not clean_refs:
        pdf.set_font("Helvetica", "I", 11)
        pdf.cell(0, 10, "No references were at or above threshold.", ln=True)
    else:
        for ref in clean_refs:
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 11)
            title_text = clean_text(f"Ref [{ref.get('ref_id')}]: {ref.get('original_data', {}).get('parsed', {}).get('title')}")
            pdf.multi_cell(0, 6, title_text)
            pdf.set_font("Helvetica", "", 10)
            score_rationale = f"Score: {ref.get('RS_final')} | Rationale: {clean_text(ref.get('llm_rationale'))}"
            pdf.ln(1)
            pdf.multi_cell(0, 5, score_rationale)
            pdf.ln(3)

    return bytes(pdf.output())

# --- 3. ADVANCED FEATURES TAB ---
def render_advanced_features():
    if not st.session_state.current_document_id:
        st.info("📂 Please select a document from the Document Manager sidebar.")
        return

    doc_id = st.session_state.current_document_id
    files = st.session_state.pipeline.get_document_files(doc_id)

    if not files.get('scored') or not os.path.exists(files['scored']):
        st.info("Pipeline incomplete. Please process the document first.")
        return

    with open(files['scored'], 'r', encoding='utf-8') as f:
        audit_data = json.load(f)

    scored_refs = audit_data.get('scored_references', [])
    missing_cites = audit_data.get("missing_citations", [])
    manuscript_title = audit_data.get('manuscript_metadata', {}).get('title', 'Unknown Manuscript')

    st.header("🎓 Advanced Audit Diagnostics")
    st.markdown("Comprehensive pipeline features including core integrity validations and deep-dive bibliometric analytics.")

    # ==========================================
    # GROUP 1: ESSENTIAL INTEGRITY VALIDATION
    # ==========================================
    st.markdown("---")
    st.subheader("🛡️ Essential Integrity Checks")
    st.markdown("Standard pipeline validations to ensure metadata accuracy and comprehensive literature coverage.")

    # 1A. HALLUCINATION GUARD
    st.markdown("#### Metadata Hallucination Guard")
    hallucinations = [r for r in scored_refs if "Mismatch" in r.get('consistency_status', '')]
    if hallucinations:
        st.error(f"🚨 Detected {len(hallucinations)} metadata inconsistencies!")
        for h in hallucinations:
            with st.expander(f"Ref {h.get('ref_id')} - {h.get('consistency_status')}"):
                st.write(f"**Parsed Title (PDF):** {h.get('original_data', {}).get('parsed', {}).get('title')}")
                st.write(f"**Retrieved Title (API):** {h.get('external_metadata', {}).get('title')}")
    else:
        st.success("✅ No severe metadata hallucinations detected between PDF extraction and API retrieval.")

    # 1B. MISSING LITERATURE
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("#### Unverified Literature-Search Candidates")
    if not missing_cites:
        st.info("No missing citations were generated.")
    else:
        st.warning(f"The model generated **{len(missing_cites)} unverified literature-search candidates**. Resolve each candidate in a trusted scholarly index before using it.")
        for paper in missing_cites:
            st.markdown(f"""
            <div style="background-color: #f8f9fa; padding: 20px; border-radius: 10px; border-left: 5px solid #1f77b4; margin-bottom: 10px;">
                <h5 style="color: #333; margin-top: 0;">💡 {escape(str(paper.get('title', 'Unknown Title')))}</h5>
                <p style="color: #666; font-size: 0.90rem; margin-bottom: 10px;"><b>Authors:</b> {escape(str(paper.get('authors', 'N/A')))} | <b>Year:</b> {escape(str(paper.get('year', 'N/A')))}</p>
                <p style="color: #222; font-size: 0.95rem; margin-bottom: 0;"><b>AI Rationale:</b> {escape(str(paper.get('rationale', 'No rationale provided.')))}</p>
            </div>
            """, unsafe_allow_html=True)


    # ==========================================
    # GROUP 2: ADVANCED BIBLIOMETRIC EXPLORATIONS
    # ==========================================
    st.markdown("---")
    st.subheader("🔬 Deep-Dive Bibliometric Analytics")
    st.markdown("Extended exploratory features mapping the semantic, temporal, and social landscape of the author's bibliography.")

    # 2A. CITATION INTENT
    st.markdown("#### Citation Intent & Semantic Analysis")
    st.markdown("Analyzes the semantic context of *why* the author cited these papers (e.g., building on past work vs. arguing against it).")

    intents = [r.get('citation_intent', 'Background') for r in scored_refs]
    if intents:
        intent_counts = pd.Series(intents).value_counts().reset_index()
        intent_counts.columns = ['Intent', 'Count']

        color_map = {
            'Supporting': '#2ca02c',
            'Contrasting': '#d62728',
            'Methodology': '#9467bd',
            'Background': '#7f7f7f'
        }

        fig_intent = px.pie(
            intent_counts,
            values='Count',
            names='Intent',
            hole=0.4,
            color='Intent',
            color_discrete_map=color_map
        )
        fig_intent.update_traces(textposition='inside', textinfo='percent+label')
        fig_intent.update_layout(margin=dict(t=20, b=0, l=0, r=0), height=350)

        col_chart, col_text = st.columns([1.2, 1])
        with col_chart:
            st.plotly_chart(fig_intent, use_container_width=True)

        with col_text:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("##### 🤖 AI Insight:")

            background_pct = (intents.count('Background') / len(intents)) * 100 if intents else 0
            contrasting_pct = (intents.count('Contrasting') / len(intents)) * 100 if intents else 0

            if background_pct > 70:
                st.info("**Background-heavy distribution:** More than 70% were classified as background. Interpret this in the context of manuscript type and discipline.")
            elif contrasting_pct > 20:
                st.info("**Contrast-heavy distribution:** More than 20% were classified as contrasting. Inspect the cited passages before drawing conclusions.")
            else:
                st.info("Intent labels are descriptive model outputs and require reviewer verification.")

        st.markdown("""
        <div style="background-color: #f8f9fa; padding: 15px; border-radius: 8px; border: 1px solid #e9ecef; margin-bottom: 20px;">
            <p style="margin-bottom: 5px; font-weight: 600;">📖 Intent Chart Legend:</p>
            <ul style="margin-bottom: 0; font-size: 0.90rem; color: #444;">
                <li>🟩 <b>Supporting:</b> Agrees with, builds upon, or uses the paper to validate claims.</li>
                <li>🟥 <b>Contrasting:</b> Disagrees with, critiques, or highlights a gap.</li>
                <li>🟪 <b>Methodology:</b> Explicitly uses a formula, dataset, or algorithm.</li>
                <li>⬜ <b>Background:</b> Mentions purely for historical context.</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)

    # 2B. TEMPORAL CURRENCY
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("#### Temporal Currency (Age) Analysis")
    st.markdown("Describes publication-year distribution. Reference age alone is not a relevance or quality judgment.")

    current_year = datetime.now().year
    years = []
    for r in scored_refs:
        year = r.get('external_metadata', {}).get('publication_year') or r.get('external_metadata', {}).get('year') or r.get('original_data', {}).get('parsed', {}).get('year')
        if year:
            try:
                clean_year = int("".join(filter(str.isdigit, str(year)))[:4])
                years.append(clean_year)
            except ValueError:
                pass

    if years:
        df_years = pd.DataFrame({'Year': years})
        fig_time = px.histogram(
            df_years,
            x='Year',
            nbins=15,
            color_discrete_sequence=['#1f77b4']
        )
        fig_time.update_layout(
            xaxis_title="Publication Year",
            yaxis_title="Number of Citations",
            margin=dict(t=20, b=0, l=0, r=0),
            bargap=0.1,
            height=300
        )

        col_chart2, col_text2 = st.columns([1.5, 1])
        with col_chart2:
            st.plotly_chart(fig_time, use_container_width=True)

        with col_text2:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("##### 🤖 AI Insight:")
            total_valid_years = len(years)
            older_pct = (sum(1 for y in years if current_year - y > 10) / total_valid_years) * 100
            recent_pct = (sum(1 for y in years if current_year - y <= 5) / total_valid_years) * 100

            if older_pct > 40:
                st.info(f"{older_pct:.1f}% of references are over 10 years old. Age alone does not determine relevance or quality.")
            elif recent_pct > 60:
                st.info(f"{recent_pct:.1f}% of references are from the last 5 years. Review currency in disciplinary context.")
            else:
                st.info(f"{recent_pct:.1f}% of references were published in the last 5 years.")
    else:
        st.info("No publication year data could be extracted for these references.")


    # 2C. NETWORK BIAS
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("#### Descriptive Author-Overlap Network")
    st.markdown("Maps repeated author names across cited references. Name overlap is descriptive and is not evidence of bias.")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Top Cited Venues**")
        venues = [r.get('external_metadata', {}).get('venue') or r.get('original_data', {}).get('parsed', {}).get('venue') for r in scored_refs]
        venues = [v for v in venues if v]
        if venues: st.bar_chart(pd.Series(venues).value_counts().head(7), color="#1f77b4", height=250)
    with col2:
        st.markdown("**Top Cited Authors**")
        all_authors = []
        for r in scored_refs:
            all_authors.extend(r.get('original_data', {}).get('parsed', {}).get('authors', []))
        clean_authors = [a.strip() for a in all_authors if a]
        if clean_authors: st.bar_chart(pd.Series(clean_authors).value_counts().head(7), color="#ff7f0e", height=250)

    cliques = {}
    if scored_refs:
        G = nx.Graph()
        G.add_node("MANUSCRIPT", label="Main Manuscript", title=escape(str(manuscript_title)), color="#e74c3c", size=30)
        author_to_refs = {}

        for ref in scored_refs:
            ref_id = str(ref.get('ref_id'))
            title = ref.get('original_data', {}).get('parsed', {}).get('title', 'Unknown Title')
            authors = ref.get('original_data', {}).get('parsed', {}).get('authors', [])

            short_label = title[:30] + "..." if len(title) > 30 else title
            G.add_node(
                ref_id,
                label=short_label,
                title=escape(f"[{ref_id}] {title}"),
                color="#3498db",
                size=15,
            )
            G.add_edge("MANUSCRIPT", ref_id, color="#bdc3c7")

            for author in authors:
                clean_author = author.replace(',', '').replace('.', '').strip().lower()
                if clean_author:
                    if clean_author not in author_to_refs:
                        author_to_refs[clean_author] = []
                    author_to_refs[clean_author].append(ref_id)

        clique_edges_added = 0
        for author, refs in author_to_refs.items():
            if len(refs) > 1:
                display_author = author.title()
                cliques[display_author] = refs

                for i in range(len(refs)):
                    for j in range(i + 1, len(refs)):
                        if not G.has_edge(refs[i], refs[j]):
                            G.add_edge(
                                refs[i],
                                refs[j],
                                color="#e74c3c",
                                dashes=True,
                                title=escape(f"Shared Author: {display_author}"),
                            )
                            clique_edges_added += 1

        net = Network(
            height="400px",
            width="100%",
            bgcolor="#ffffff",
            font_color="#333333",
            select_menu=True,
            cdn_resources='in_line',
        )
        net.from_nx(G)
        net.repulsion(node_distance=150, central_gravity=0.1, spring_length=150, spring_strength=0.05)

        source_code = net.generate_html()

        components.html(source_code, height=415)

        st.markdown("""
        <div style="background-color: #f8f9fa; padding: 15px; border-radius: 8px; border: 1px solid #e9ecef; margin-bottom: 10px;">
            <p style="margin-bottom: 5px; font-weight: 600;">🔍 Network Graph Legend:</p>
            <ul style="margin-bottom: 0; font-size: 0.90rem; color: #444;">
                <li>🔴 <b>Large Red Node:</b> The Main Manuscript. | <li>⚪ <b>Small Grey Nodes:</b> The cited references. (Turns 🔵 <b>Blue</b> when clicked).</li>
                <li>➖ <b>Solid Grey Lines:</b> Direct citations from the manuscript.</li>
                <li>🚩 <b>Red Dashed Lines:</b> <b>Author Overlap</b> (Two references share the exact same author).</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)

        if clique_edges_added > 0:
            st.info(f"Found {clique_edges_added} interconnections between cited papers based on shared normalized author names.")
        else:
            st.info("No repeated-author connections were found by this matching method.")


    # ==========================================
    # GROUP 3: EXPORT AND REPORTING
    # ==========================================
    st.markdown("---")
    st.subheader("📥 Audit Export & Reporting")
    st.markdown("Compile all core checks and advanced bibliometric insights into a formal review package.")

    # --- UPDATED: Syncing threshold slider with session state ---
    tau = st.slider(
        "Select Tolerance Threshold (τ) for Exported Report:",
        min_value=0,
        max_value=100,
        key="relevance_threshold",
        step=1
    )

    flagged_refs_raw = [r for r in scored_refs if is_flagged(r, tau)]
    kpis = {
        'total': len(scored_refs),
        'flagged': len(flagged_refs_raw),
        'self_cites': sum(1 for r in scored_refs if r.get('self_citation', {}).get('is_self_cite')),
        # Adding missing abstracts and issues for the summary table
        'missing_abstracts': sum(1 for r in scored_refs if not bool(r.get('external_metadata', {}).get('abstract'))),
        'issues': sum(1 for r in scored_refs if bool(r.get('quality_flags'))),
        'unavailable': sum(1 for r in scored_refs if numeric_score(r) is None),
    }

    col_html, col_pdf, col_empty = st.columns([1, 1, 3])

    with col_html:
        html_report = generate_html_report(manuscript_title, tau, kpis, scored_refs, missing_cites, cliques)
        st.download_button(
            label="📄 Export as HTML",
            data=html_report,
            file_name=f"CitePrism_Audit_{datetime.now().strftime('%Y%m%d_%H%M')}.html",
            mime="text/html",
            use_container_width=True
        )

    with col_pdf:
        if HAS_FPDF:
            pdf_report = generate_pdf_report(manuscript_title, tau, kpis, scored_refs, missing_cites, cliques)
            st.download_button(
                label="📑 Export as PDF",
                data=pdf_report,
                file_name=f"CitePrism_Audit_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
                mime="application/pdf",
                type="primary",
                use_container_width=True
            )
        else:
            st.error("Missing 'fpdf2' library. Run `pip install fpdf2` to enable PDF export.")
