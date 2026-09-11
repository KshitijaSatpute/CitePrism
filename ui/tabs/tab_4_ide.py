import streamlit as st
import json
import os
import pandas as pd
from datetime import datetime
import plotly.graph_objects as go
from html import escape
from src.utils.scoring import is_flagged, numeric_score

try:
    from fpdf import FPDF
    HAS_FPDF = True
except ImportError:
    HAS_FPDF = False

# ---------------------------------------------------------------------------
# UTILS: Unicode-Safe Text Cleaning for PDF
# ---------------------------------------------------------------------------

def clean_text(text):
    """Prevents FPDFUnicodeEncodingException by mapping special characters to ASCII."""
    if not text:
        return "N/A"
    replacements = {
        "\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"', "\u2022": "*", "\u2026": "...",
        "\u00a0": " ", # non-breaking space
    }
    text = str(text)
    for char, rep in replacements.items():
        text = text.replace(char, rep)
    return text.encode('latin-1', 'ignore').decode('latin-1')

# ---------------------------------------------------------------------------
# REPORT GENERATORS (HTML & PDF)
# ---------------------------------------------------------------------------

def generate_html_report(manuscript_title, tau, kpis, all_refs):
    manuscript_title = escape(str(manuscript_title))
    # 1. Flagged References Table
    table_rows = ""
    flagged_refs = [r for r in all_refs if is_flagged(r, tau)]
    for ref in flagged_refs:
        title = escape(str(ref.get('original_data', {}).get('parsed', {}).get('title', 'Unknown Title')))
        score = ref.get('RS_final', 0)
        rationale = escape(str(ref.get('llm_rationale', 'No rationale provided.')))
        
        badges = []
        if ref.get('self_citation', {}).get('is_self_cite'): badges.append("👤 Self-Cite")
        if ref.get('quality_flags'): badges.append("⚠️ Issue")
        if not bool(ref.get('external_metadata', {}).get('abstract')): badges.append("🚫 No Abstract")
        
        table_rows += f"""
        <tr>
            <td>{escape(str(ref.get('ref_id', '')))}</td>
            <td><strong>{title}</strong></td>
            <td style="color: red; font-weight: bold;">{score}</td>
            <td>{" | ".join(badges) if badges else "None"}</td>
            <td>{rationale}</td>
        </tr>"""

    # 2. Complete Missing Abstracts Table
    missing_rows = ""
    missing_refs = [r for r in all_refs if not bool(r.get('external_metadata', {}).get('abstract'))]
    for m_ref in missing_refs:
        m_title = escape(str(m_ref.get('original_data', {}).get('parsed', {}).get('title', 'Unknown Title')))
        missing_rows += f"""
        <tr>
            <td>{escape(str(m_ref.get('ref_id', '')))}</td>
            <td>{m_title}</td>
            <td>{numeric_score(m_ref) if numeric_score(m_ref) is not None else 'Unavailable'}</td>
            <td style="color: #6c757d; font-weight: bold;">🚫 Missing</td>
        </tr>"""

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>CitePrism Audit Report</title>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, sans-serif; margin: 40px; color: #333; }}
            h1 {{ color: #1f77b4; border-bottom: 2px solid #1f77b4; padding-bottom: 10px; }}
            .meta-info {{ font-size: 14px; color: #666; margin-bottom: 20px; }}
            .kpi-container {{ display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 30px; }}
            .kpi-box {{ flex: 1; min-width: 150px; padding: 15px; background: #f8f9fa; border-radius: 8px; border: 1px solid #dee2e6; text-align: center; }}
            .kpi-value {{ font-size: 24px; font-weight: bold; color: #1f77b4; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
            th, td {{ padding: 12px; border: 1px solid #ddd; text-align: left; }}
            th {{ background-color: #1f77b4; color: white; }}
            tr:nth-child(even) {{ background-color: #f9f9f9; }}
        </style>
    </head>
    <body>
        <h1>CitePrism Citation Audit Report</h1>
        <h2>Manuscript: {manuscript_title}</h2>
        <div class="meta-info">
            <strong>Date Generated:</strong> {datetime.now().strftime('%B %d, %Y')}<br>
            <strong>Total Audited:</strong> {kpis['total']} references
        </div>
        
        <div class="kpi-container">
            <div class="kpi-box"><div>Threshold (τ)</div><div class="kpi-value">{tau}</div></div>
            <div class="kpi-box"><div>Flagged</div><div class="kpi-value" style="color: #dc3545;">{kpis['flagged']}</div></div>
            <div class="kpi-box"><div>Missing Abstracts</div><div class="kpi-value" style="color: #6c757d;">{kpis['missing_abstracts']}</div></div>
        </div>

        <h3>🚩 Flagged References (Score < {tau})</h3>
        <table>
            <thead><tr><th>ID</th><th>Title</th><th>Score</th><th>Badges</th><th>Rationale</th></tr></thead>
            <tbody>{table_rows if table_rows else "<tr><td colspan='5'>No flagged references found.</td></tr>"}</tbody>
        </table>

        <br><br>
        <h3 style="color: #6c757d;">🚫 Complete Missing Abstracts Audit Trail ({len(missing_refs)})</h3>
        <table>
            <thead><tr><th>ID</th><th>Reference Title</th><th>Score</th><th>Status</th></tr></thead>
            <tbody>{missing_rows if missing_rows else "<tr><td colspan='4'>No missing abstracts found.</td></tr>"}</tbody>
        </table>
    </body>
    </html>
    """

def generate_pdf_report(manuscript_title, tau, kpis, all_refs):
    class PDF(FPDF):
        def header(self):
            self.set_font("Helvetica", "B", 16)
            self.set_text_color(31, 119, 180)
            self.cell(0, 10, "CitePrism Citation Audit Report", ln=True, align="C")
            self.ln(5)
    
    pdf = PDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 12)
    pdf.multi_cell(0, 8, clean_text(f"Manuscript: {manuscript_title}"))
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Date: {datetime.now().strftime('%Y-%m-%d')}", ln=True)
    pdf.ln(5)

    # Summary Stats
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Audit Metrics Summary:", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"- Total Audited: {kpis['total']}", ln=True)
    pdf.cell(0, 6, f"- Flagged Below {tau}: {kpis['flagged']}", ln=True)
    pdf.cell(0, 6, f"- Total Missing Abstracts: {kpis['missing_abstracts']}", ln=True)
    pdf.ln(10)

    # Flagged Details
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 10, f"Detailed Flagged List (Score < {tau})", ln=True)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(5)

    flagged_refs = [r for r in all_refs if is_flagged(r, tau)]
    for ref in flagged_refs:
        title = ref.get('original_data', {}).get('parsed', {}).get('title', 'Unknown')
        pdf.set_font("Helvetica", "B", 10)
        pdf.multi_cell(0, 6, clean_text(f"[{ref.get('ref_id')}] {title}"))
        
        has_abs = bool(ref.get('external_metadata', {}).get('abstract'))
        badge_str = "No Abstract" if not has_abs else "None"
        
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 6, clean_text(f"Score: {ref.get('RS_final')} | Badges: {badge_str}"), ln=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, clean_text(f"Rationale: {ref.get('llm_rationale', 'N/A')}"))
        pdf.ln(4)

    return bytes(pdf.output())

# ---------------------------------------------------------------------------
# MAIN UI RENDERING
# ---------------------------------------------------------------------------

def render_analyst_ide():
    if not st.session_state.current_document_id:
        st.info("Select a document to begin.")
        return

    doc_id = st.session_state.current_document_id
    files = st.session_state.pipeline.get_document_files(doc_id)
    
    if files.get('scored') and os.path.exists(files['scored']):
        with open(files['scored'], 'r', encoding='utf-8') as f:
            audit_data = json.load(f)
        
        scored_refs = audit_data.get('scored_references', [])
        manuscript_title = audit_data.get('manuscript_metadata', {}).get('title', 'Unknown Manuscript')
        
        if scored_refs:
            st.header("🕵️ Citation Relevance Audit")
            
            # --- UPDATED: Syncing slider with session_state ---
            tau = st.slider(
                "Flag references scoring below this threshold (τ):", 
                0, 100, 
                key="relevance_threshold", 
                step=1
            )
            
            # KPI Calculations
            flagged_refs = [r for r in scored_refs if is_flagged(r, tau)]
            missing_abstracts = [r for r in scored_refs if not bool(r.get('external_metadata', {}).get('abstract'))]
            self_cites = [r for r in scored_refs if r.get('self_citation', {}).get('is_self_cite')]
            issue_refs = [r for r in scored_refs if bool(r.get('quality_flags'))]

            # KPI Dashboard (5 Columns)
            kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
            kpi1.metric("Total Audited", len(scored_refs))
            kpi2.metric("Below Threshold", len(flagged_refs))
            kpi3.metric("Self-Citations", len(self_cites))
            kpi4.metric("Quality Issues", len(issue_refs))
            kpi5.metric("Missing Abstract", len(missing_abstracts))

            st.markdown("<br>", unsafe_allow_html=True)

            # Charts
            col_chart1, col_chart2 = st.columns(2)
            with col_chart1:
                fig_pie = go.Figure(data=[go.Pie(
                    labels=['✅ Relevant', '🚩 Flagged'], 
                    values=[len(scored_refs)-len(flagged_refs), len(flagged_refs)], 
                    marker_colors=['#2ca02c', '#d62728'], hole=.4
                )])
                fig_pie.update_layout(title_text="Audit Relevance Ratio", margin=dict(t=40, b=0, l=0, r=0))
                st.plotly_chart(fig_pie, use_container_width=True)

            with col_chart2:
                fig_bar = go.Figure(data=[go.Bar(
                    x=["Total Audited", "Flagged", "No Abstract"],
                    y=[len(scored_refs), len(flagged_refs), len(missing_abstracts)],
                    marker_color=["#1f77b4", "#d62728", "#7f7f7f"]
                )])
                fig_bar.update_layout(title_text="Analyst Workload Breakdown")
                st.plotly_chart(fig_bar, use_container_width=True)

            # --- RESTORED TABLE LEGEND ---
            st.markdown("""
            <div style="background-color: #f8f9fa; padding: 15px; border-radius: 8px; border: 1px solid #e9ecef; margin-bottom: 15px; font-size: 0.95rem;">
                <strong style="color: #333;">📖 Table Legend:</strong>
                <ul style="margin-top: 5px; margin-bottom: 0; color: #555;">
                    <li><span style="background-color: rgba(255, 0, 0, 0.1); padding: 2px 5px; border-radius: 3px; border: 1px solid #f5c6cb;">Red Highlighted Row</span> & 🚩 <b>Flagged:</b> AI scored this below your threshold (τ). Requires review.</li>
                    <li>✅ <b>OK:</b> The reference is contextually relevant.</li>
                    <li>👤 <b>Self-Cite:</b> Author overlap detected between main paper and citation.</li>
                    <li>🚫 <b>No Abstract:</b> Scored using metadata only (Higher risk of score variance).</li>
                </ul>
            </div>
            """, unsafe_allow_html=True)

            # Table Preparation
            table_rows = []
            for r in scored_refs:
                badge_list = []
                if r.get('self_citation', {}).get('is_self_cite'): badge_list.append("👤")
                if not bool(r.get('external_metadata', {}).get('abstract')): badge_list.append("🚫")
                
                table_rows.append({
                    "ID": r.get('ref_id'),
                    "Title": r.get('original_data', {}).get('parsed', {}).get('title', 'Unknown'),
                    "Score (RS)": r.get('RS_final', 0),
                    "Status": (
                        "⚪ Unavailable" if numeric_score(r) is None
                        else "🚩 Flagged" if is_flagged(r, tau)
                        else "✅ Above threshold"
                    ),
                    "Badges": " ".join(badge_list)
                })

            df = pd.DataFrame(table_rows)
            def highlight_flagged(row):
                return ['background-color: rgba(255, 0, 0, 0.1)'] * len(row) if row['Status'] == '🚩 Flagged' else [''] * len(row)
            st.dataframe(df.style.apply(highlight_flagged, axis=1), use_container_width=True, hide_index=True)

            # Export Buttons
            st.markdown("---")
            st.markdown("### 📥 Export Audit Reports")
            r_kpis = {'total': len(scored_refs), 'flagged': len(flagged_refs), 'missing_abstracts': len(missing_abstracts)}
            c1, c2, _ = st.columns([1, 1, 2])
            with c1:
                st.download_button("📄 HTML Report", data=generate_html_report(manuscript_title, tau, r_kpis, scored_refs), file_name="Audit.html", mime="text/html")
            with c2:
                if HAS_FPDF:
                    st.download_button("📑 PDF Report", data=generate_pdf_report(manuscript_title, tau, r_kpis, scored_refs), file_name="Audit.pdf", mime="application/pdf")

            # --- RESTORED CONTEXT VIEWER ---
            st.markdown("---")
            st.markdown("### 🔍 Context & Evidence Viewer")
            sel_id = st.selectbox("Select Reference ID:", [r['ID'] for r in table_rows])
            for ref in scored_refs:
                if ref.get('ref_id') == sel_id:
                    st.info(f"**LLM Rationale:** {ref.get('llm_rationale')}")
                    with st.expander("View Audit Abstract (OpenAlex/Source)"):
                        st.write(ref.get('external_metadata', {}).get('abstract') or 'No abstract available.')

        else:
            st.info("No references found.")
    else:
        st.warning("Scored data not available. Please run the pipeline first.")
