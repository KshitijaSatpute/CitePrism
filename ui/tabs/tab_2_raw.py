import streamlit as st
import os
import json
import base64
from ui.components.helpers import (
    display_pdf,
    display_json_pretty,
    show_pipeline_progress,
    display_ranked_citation_review,
)
def render_raw_data_tab():
    # 1. Check if a document is selected
    if not st.session_state.current_document_id:
        st.info("📂 Please select a document from the Document Manager sidebar to see results here.")
        return

    doc_id = st.session_state.current_document_id
    str_doc_id = str(doc_id)
    
    # Safely get status and files
    try:
        status = st.session_state.db_manager.get_document_status(doc_id)
    except:
        status = {"pdf_filename": f"Document {doc_id}"}
        
    files = st.session_state.pipeline.get_document_files(doc_id)
    stages = st.session_state.pipeline.check_stage_completion(doc_id)
    
    st.header(f"Results for: {status.get('pdf_filename', 'Selected Document')}")
    show_pipeline_progress(stages)
    st.markdown("---")
    
    # 2. Create the Side-by-Side Layout
    col_pdf, col_text = st.columns([1, 1], gap="large")
    
    # --- LEFT COLUMN: PDF VIEWER ---
    with col_pdf:
        st.subheader("📄 PDF Viewer")
        display_pdf(files['pdf'])
    
    # --- RIGHT COLUMN: HUMAN READABLE INSPECTOR ---
    with col_text:
        st.subheader("🔍 Data Extraction Viewer")
        
        # Determine available stages
        available_options = []
        if stages.get('parsed') and files.get('parsed') and os.path.exists(files['parsed']):
            available_options.append("Parsed Extraction")
        if stages.get('enriched') and files.get('enriched') and os.path.exists(files['enriched']):
            available_options.append("Metadata Enrichment")
        if stages.get('scored') and files.get('scored') and os.path.exists(files['scored']):
            available_options.append("Final Relevance Scoring")
            
        if not available_options:
            st.info("No data available yet. Please run the pipeline.")
        else:
            # Stage Selector Dropdown
            view_choice = st.selectbox(
                "Select Pipeline Stage to View:", 
                available_options, 
                index=len(available_options)-1,
                label_visibility="collapsed"
            )
            
            file_map = {
                "Parsed Extraction": files.get('parsed'),
                "Metadata Enrichment": files.get('enriched'),
                "Final Relevance Scoring": files.get('scored')
            }
            
            selected_file = file_map.get(view_choice)
            
            if selected_file and os.path.exists(selected_file):
                with open(selected_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # --- DATA EXTRACTION ---
                meta = data.get('manuscript_metadata') or data.get('metadata') or {}
                refs = data.get('scored_references') or data.get('enriched_references') or data.get('references') or []
                in_text_cites = data.get('citations_in_text') or data.get('in_text_citations') or []
                
                # KPI METRICS
                st.markdown("<br>", unsafe_allow_html=True)
                kpi1, kpi2 = st.columns(2)
                kpi1.metric(label="📚 Total References", value=len(refs))
                kpi2.metric(label="📎 In-Text Citations", value=len(in_text_cites))
                st.markdown("<br>", unsafe_allow_html=True)
                
                
                # Reviewer-facing ranked table for final scored results
                if view_choice == "Final Relevance Scoring" and data.get("scored_references"):
                    display_ranked_citation_review(data)
                    st.markdown("<br>", unsafe_allow_html=True)
                
                # SUB-TABS
                tab_meta, tab_cites, tab_refs = st.tabs(["📄 Metadata", f"📎 Citations ({len(in_text_cites)})", f"📚 References ({len(refs)})"])
                
                # TAB 1: METADATA
                with tab_meta:
                    title = meta.get('title', 'Unknown Title')
                    authors = meta.get('authors', [])
                    authors_str = ", ".join(authors) if isinstance(authors, list) else str(authors)
                    doi = meta.get('doi')
                    doi_display = f"`{doi}`" if doi else "*NULL*"
                    abstract = meta.get('abstract', 'No abstract extracted.')
                    
                    st.markdown(f"**Title:** {title}")
                    st.markdown(f"**Authors:** {authors_str}")
                    st.markdown(f"**DOI:** {doi_display}")
                    st.markdown("**Abstract:**")
                    st.info(abstract)
                
                # TAB 2: CITATIONS
                with tab_cites:
                    if not in_text_cites:
                        st.write("No in-text citations found.")
                    else:
                        with st.container(height=500):
                            for cite in in_text_cites:
                                marker = cite.get('marker', '[Unknown]')
                                context = cite.get('context_window') or cite.get('text') or "No context text."
                                st.markdown(f"**Marker:** `{marker}`")
                                st.markdown(f"*{context}*")
                                st.divider()

                # TAB 3: REFERENCES
                with tab_refs:
                    if not refs:
                        st.write("No references found.")
                    else:
                        with st.container(height=500):
                            for i, ref in enumerate(refs):
                                # Extraction logic for Enriched vs Scored stages
                                ext_meta = ref.get('external_metadata', {})
                                
                                # 1. Identify best available metadata
                                ref_id = ref.get('ref_id', f"[{i+1}]")
                                
                                # Use Enriched title if available, fallback to parsed PDF title
                                ref_title = ext_meta.get('title') or ref.get('title') or "Unknown Title"
                                
                                # Authors list construction
                                if ext_meta.get('authors'):
                                    ref_authors_str = ", ".join([a.get('display_name', 'Unknown') for a in ext_meta['authors']])
                                else:
                                    ref_authors = ref.get('authors') or []
                                    ref_authors_str = ", ".join(ref_authors) if isinstance(ref_authors, list) else str(ref_authors)
                                
                                ref_year = ext_meta.get('year') or ref.get('year') or "N/A"
                                
                                # --- NEW: Venue & Abstract Status ---
                                ref_venue = ext_meta.get('venue') or "Unknown Venue"
                                abstract_text = ext_meta.get('abstract') or ref.get('abstract')
                                has_abstract = bool(abstract_text and len(str(abstract_text).strip()) > 10)

                                # UI: Title Row
                                st.markdown(f"**{ref_id}** | {ref_title}")
                                
                                # UI: Metadata Line (Includes Venue)
                                st.caption(f"Authors: {ref_authors_str} | Year: {ref_year} | Venue: {ref_venue}")
                                
                                # UI: Missing Abstract Flag
                                if not has_abstract:
                                    st.markdown(
                                        '<div style="display: inline-block; background-color: #f8d7da; color: #721c24; '
                                        'padding: 2px 10px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; '
                                        'border: 1px solid #dc3545; margin-bottom: 8px;">⚠️ ABSTRACT MISSING</div>', 
                                        unsafe_allow_html=True
                                    )

                                # UI: Final Scoring Logic (existing)
                                if view_choice == "Final Relevance Scoring" and 'RS_final' in ref:
                                    label = ref.get('label', '').upper()
                                    if label == "RELEVANT":
                                        badge_color, text_color = "#d4edda", "#155724"
                                    elif label == "IRRELEVANT":
                                        badge_color, text_color = "#f8d7da", "#721c24"
                                    else:
                                        badge_color, text_color = "#fff3cd", "#856404"
                                        
                                    badge_html = f"<span style='background-color: {badge_color}; color: {text_color}; padding: 3px 8px; border-radius: 12px; font-size: 0.8rem; font-weight: bold;'>{label}</span>"
                                    
                                    cols = st.columns([1, 1, 2])
                                    cols[0].markdown(f"**Score:** `{ref.get('RS_final')}/100`")
                                    cols[1].markdown(badge_html, unsafe_allow_html=True)
                                    if ref.get('citation_intent'):
                                        cols[2].markdown(f"**Intent:** {ref.get('citation_intent')}")
                                    
                                    if ref.get('llm_rationale'):
                                        st.markdown(f"> 🤖 **AI:** {ref.get('llm_rationale')}")
                                
                                st.divider()

                # DOWNLOAD BUTTONS
                st.markdown("<br>", unsafe_allow_html=True)
                col_down, col_raw = st.columns([1, 1])
                with col_down:
                    json_string = json.dumps(data, indent=4)
                    st.download_button(
                        label=f"📥 Download {view_choice} JSON",
                        file_name=f"{str_doc_id}_{view_choice.replace(' ', '_').lower()}.json",
                        mime="application/json",
                        data=json_string,
                        width="stretch"
                    )
                with col_raw:
                    with st.expander("⚙️ View Raw JSON Data"):
                        display_json_pretty(data, title=f"Showing: {view_choice}")
            else:
                st.error(f"Could not load the file for {view_choice}.")