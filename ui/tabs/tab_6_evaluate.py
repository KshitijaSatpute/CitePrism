import streamlit as st
import pandas as pd
import json
import os
import numpy as np # Added for dynamic label detection
from sklearn.metrics import cohen_kappa_score, confusion_matrix, classification_report
import seaborn as sns
import matplotlib.pyplot as plt

def render_evaluation_tab():
    st.header("📈 Evaluation Metrics & Validation")
    st.markdown("Calculate **Cohen's Kappa (κ)** against human Gold Labels to scientifically validate CitePrism's accuracy.")
    st.markdown("---")

    # 1. Check if a document is selected
    if not st.session_state.current_document_id:
        st.info("📂 Please select a document from the Document Manager sidebar to begin evaluation.")
        return

    doc_id = st.session_state.current_document_id
    files = st.session_state.pipeline.get_document_files(doc_id)

    # 2. Check if the document has been fully scored
    if not files.get('scored') or not os.path.exists(files['scored']):
        st.warning("⚠️ No scored data found for this document. Please run the full audit pipeline first.")
        return

    # Load the AI Scored Data
    with open(files['scored'], 'r', encoding='utf-8') as f:
        audit_data = json.load(f)

    scored_refs = audit_data.get('scored_references', [])
    if not scored_refs:
        st.warning("⚠️ No scored references found in this document's JSON.")
        return

    # --- UI: File Uploader ---
    st.markdown("### 1. Upload Answer Key (Gold Labels)")
    st.markdown("Upload your CSV file containing the `ref_id` and `human_label` columns.")

    uploaded_file = st.file_uploader("Upload gold_labels.csv", type=['csv'])

    if uploaded_file is not None:
        try:
            df_human = pd.read_csv(uploaded_file)

            # Validate columns
            if 'ref_id' not in df_human.columns or 'human_label' not in df_human.columns:
                st.error("❌ Invalid CSV format. It must contain exactly 'ref_id' and 'human_label' columns.")
                return

            # Clean IDs for merging
            df_human['ref_id'] = df_human['ref_id'].astype(str).str.strip()

        except Exception as e:
            st.error(f"❌ Error reading CSV: {e}")
            return

        st.markdown("---")
        st.markdown("### 2. Holdout Evaluation")

        # --- NEW INFO BOX ---
        st.info(
            "💡 **What is Cohen's Kappa?** It is a strict grading metric that measures how well the AI's "
            "judgments match your human grades, while removing any agreements that happened just by random chance.\n\n"
            "The decision threshold must be fixed before inspecting this answer key. "
            "Changing it against the same labels would leak the test set into model selection."
        )

        scoring_metadata = audit_data.get('scoring_metadata', {})
        tau = int(scoring_metadata.get('borderline_threshold', 40))
        st.metric("Pre-registered decision threshold (τ)", tau)

        # Process AI Data based on the slider value
        ai_results = []
        for ref in scored_refs:
            ref_id = str(ref.get('ref_id')).strip()
            rs_final = ref.get('RS_final')
            if rs_final is None:
                continue
            ai_label = 1 if float(rs_final) >= tau else 0
            ai_results.append({'ref_id': ref_id, 'ai_score': rs_final, 'ai_label': ai_label})

        df_ai = pd.DataFrame(ai_results)

        # Merge Human and AI Data
        df_eval = pd.merge(df_ai, df_human, on='ref_id', how='inner')

        if len(df_eval) == 0:
            st.error("❌ No matching Reference IDs found between the JSON and the CSV.")
            with st.expander("🔍 Debug ID Mapping (See what the system sees)"):
                st.write("**First 5 JSON IDs:**", df_ai['ref_id'].head().tolist())
                st.write("**First 5 CSV IDs:**", df_human['ref_id'].head().tolist())
                st.info("Update your CSV `ref_id` column to perfectly match the format of the JSON IDs.")
            return

        st.success(f"✅ Successfully matched {len(df_eval)} references for evaluation.")
        st.markdown("<br>", unsafe_allow_html=True)

        # --- CALCULATE METRICS ---
        y_human = df_eval['human_label']
        y_ai = df_eval['ai_label']
        kappa = cohen_kappa_score(y_human, y_ai)

        # Display Kappa Score
        kpi_col, text_col = st.columns([1, 2])
        with kpi_col:
            st.metric(label="Cohen's Kappa (κ)", value=f"{kappa:.3f}")

        with text_col:
            st.markdown("<div style='margin-top: 25px;'></div>", unsafe_allow_html=True)
            if kappa >= 0.81: st.success("Result: Almost Perfect Agreement 🌟")
            elif kappa >= 0.61: st.success("Result: Substantial Agreement ✅ (MEETS TARGET!)")
            elif kappa >= 0.41: st.warning("Result: Moderate Agreement ⚠️")
            elif kappa >= 0.21: st.error("Result: Fair Agreement ❌ (Needs tuning)")
            else: st.error("Result: Slight/Poor Agreement ❌ (Check your parsing or threshold)")

        st.markdown("---")

        # --- VISUALIZATIONS ---
        col1, col2 = st.columns([1.2, 1])

        # NEW LOGIC: Detect which labels are present to avoid classification_report crashes
        unique_present_labels = np.unique(np.concatenate([y_human, y_ai]))
        all_possible_names = {0: 'Flagged (0)', 1: 'Clean (1)'}
        target_names_for_report = [all_possible_names[label] for label in unique_present_labels]

        with col1:
            st.markdown("#### 📊 Confusion Matrix")
            cm = confusion_matrix(y_human, y_ai, labels=[0, 1]) # Fixed labels for consistent size

            # Create the Seaborn Plot
            fig, ax = plt.subplots(figsize=(6, 5))
            sns.heatmap(cm, annot=True, fmt='d', cmap='Blues',
                        xticklabels=['Flagged (0)', 'Clean (1)'],
                        yticklabels=['Flagged (0)', 'Clean (1)'],
                        annot_kws={"size": 16}, ax=ax)
            ax.set_ylabel('Human Gold Label (Actual)', fontsize=11)
            ax.set_xlabel('CitePrism AI Label (Predicted)', fontsize=11)

            # Render plot in Streamlit
            st.pyplot(fig)

        with col2:
            st.markdown("#### 📈 Classification Report")

            # FIX: Only run if we have labels, and use target_names that match unique labels
            if len(unique_present_labels) > 0:
                try:
                    report_dict = classification_report(
                        y_human,
                        y_ai,
                        labels=unique_present_labels,
                        target_names=target_names_for_report,
                        output_dict=True
                    )
                    report_df = pd.DataFrame(report_dict).transpose()

                    # Display as a beautiful dataframe
                    st.dataframe(
                        report_df.style.format(precision=3).background_gradient(cmap='Blues', subset=['f1-score']),
                        use_container_width=True
                    )
                except Exception as e:
                    st.error(f"Could not generate report: {e}")
                    st.info("This usually happens when there is no variation in the labels. Try adjusting the slider.")
            else:
                st.info("No data available to generate classification report.")

        # Summary of metrics for the user
        if len(unique_present_labels) < 2:
            st.warning("The pre-registered threshold produced one prediction category. Report this result as-is; revise thresholds only on a separate development set.")
