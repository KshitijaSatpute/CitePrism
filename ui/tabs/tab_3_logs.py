import streamlit as st
import pandas as pd
import plotly.express as px

def render_logs_tab():
    if not st.session_state.current_document_id:
        st.info("Select a document from the sidebar to view its activity logs.")
        return
        
    st.header("Audit Event Timeline")
    logs = st.session_state.db_manager.get_processing_logs(st.session_state.current_document_id)
    
    if not logs:
        st.info("No logs found for this document.")
        return

    df = pd.DataFrame(logs)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['stage'] = df['stage'].str.capitalize()

    color_map = {
        'success': '#28a745',  
        'warning': '#ffc107',  
        'failed': '#dc3545',   
        'error': '#dc3545'     
    }

    fig = px.scatter(
        df,
        x='timestamp',
        y='stage',
        color='status',
        color_discrete_map=color_map,
        hover_data=['message', 'error'],
        title="Pipeline Execution Timeline",
        labels={'timestamp': 'Execution Time', 'stage': 'Pipeline Stage', 'status': 'Status'}
    )
    
    fig.update_traces(marker=dict(size=14, line=dict(width=1, color='DarkSlateGrey')))
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    with st.expander("🔍 View Raw Text Logs"):
        for log in logs:
            s_class = 'status-success' if log['status'] == 'success' else 'status-warning'
            error_text = f"<br><span style='color:#dc3545'>Error: {log.get('error')}</span>" if log.get('error') else ""
            
            st.markdown(f"""
                <div class='status-box {s_class}'>
                    <strong>{log['stage'].upper()}</strong>: {log['message']}<br>
                    <small>{log['timestamp']}</small>
                    {error_text}
                </div>
            """, unsafe_allow_html=True)