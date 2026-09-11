import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "./api";
import {
  AssignmentPicker, AuthorOverlapView, ExportsView, MissingCitationsView, OperationsView,
  RepairsView, RuntimeBadge, TeamView, useSession,
} from "./phase4";
import type {
  Activity, Audit, DecisionDisposition, Finding, Job, Manuscript, Priority,
  Reference, ReferenceEvidence, Review, Session,
} from "./types";

type View =
  | "queue" | "evidence" | "repairs" | "insights" | "exports"
  | "operations" | "team" | "activity";

const VIEWS: View[] = [
  "queue", "evidence", "repairs", "insights", "exports", "operations", "team", "activity",
];
const priorities: Priority[] = ["high", "moderate", "low", "informational"];

function readable(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, character => character.toUpperCase());
}

function relative(date?: string | null) {
  if (!date) return "Not yet";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(date));
}

function StatusPill({ children, tone = "neutral" }: { children: React.ReactNode; tone?: string }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

function Empty({ title, detail, action }: { title: string; detail: string; action?: React.ReactNode }) {
  return <div className="empty"><div className="empty-mark">◇</div><h2>{title}</h2><p>{detail}</p>{action}</div>;
}

export function App() {
  const [manuscripts, setManuscripts] = useState<Manuscript[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [audit, setAudit] = useState<Audit | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [view, setView] = useState<View>("queue");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const session = useSession();

  const selected = manuscripts.find(item => item.id === selectedId) ?? null;

  const loadManuscripts = useCallback(async () => {
    const result = await api.manuscripts();
    setManuscripts(result.items);
    setSelectedId(current => current ?? result.items[0]?.id ?? null);
  }, []);

  useEffect(() => {
    loadManuscripts().catch(reason => setError(reason.message)).finally(() => setBusy(false));
  }, [loadManuscripts]);

  useEffect(() => {
    if (!selectedId) { setAudit(null); setReview(null); return; }
    setBusy(true);
    api.audits(selectedId).then(async result => {
      const latest = result.items[0] ?? null;
      setAudit(latest);
      setReview(latest?.artifact_path === null || !latest ? null : await api.review(latest.id));
    }).catch(reason => setError(reason.message)).finally(() => setBusy(false));
  }, [selectedId]);

  useEffect(() => {
    if (!job || !["queued", "running", "cancel_requested"].includes(job.state)) return;
    const timer = window.setInterval(async () => {
      const current = await api.job(job.id);
      setJob(current);
      if (["succeeded", "failed", "cancelled"].includes(current.state)) {
        window.clearInterval(timer);
        const finishedAudit = await api.audit(current.audit_id);
        setAudit(finishedAudit);
        await loadManuscripts();
        if (current.state === "succeeded") {
          setReview(await api.review(finishedAudit.id));
        } else {
          setError(finishedAudit.error_message ?? `The audit ${current.state}.`);
        }
        setJob(null);
      }
    }, 1200);
    return () => window.clearInterval(timer);
  }, [job, loadManuscripts, selectedId]);

  async function startAudit() {
    if (!selectedId) return;
    setError(null);
    try {
      const result = await api.startAudit(selectedId);
      setAudit(result.audit); setJob(result.job); setReview(null); setView("queue");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Audit could not start"); }
  }

  async function refreshReview() {
    if (audit) setReview(await api.review(audit.id));
    await loadManuscripts();
  }

  return <div className="app-shell">
    <a className="skip-link" href="#workspace">Skip to workspace</a>
    <Sidebar manuscripts={manuscripts} selectedId={selectedId} onSelect={setSelectedId}
      onUpload={() => setUploadOpen(true)} />
    <main id="workspace">
      <Topbar view={view} onView={setView} manuscript={selected} session={session} />
      {error && <div className="notice notice-error" role="alert"><strong>Something needs attention.</strong><span>{error}</span><button onClick={() => setError(null)}>Dismiss</button></div>}
      {job && ["queued", "running", "cancel_requested"].includes(job.state) && <JobBanner job={job} />}
      {busy && <div className="loading" role="status"><span />Loading workspace…</div>}
      {!busy && !selected && <Empty title="Start with a manuscript" detail="Upload a text-based PDF to create a traceable citation review."
        action={<button className="button primary" onClick={() => setUploadOpen(true)}>Upload manuscript</button>} />}
      {!busy && selected && !audit && <Empty title="Ready for the first audit" detail="The manuscript is stored. Run the evidence pipeline when you are ready."
        action={<button className="button primary" onClick={startAudit}>Start audit</button>} />}
      {!busy && selected && audit && !review && !job && <Empty title={audit.state === "failed" ? "Audit stopped" : "No review artifact yet"}
        detail={audit.error_message ?? "Run or retry the audit to produce an evidence workspace."}
        action={<button className="button primary" onClick={startAudit}>Run audit</button>} />}
      {!busy && selected && review && <>
        <WorkspaceHeader manuscript={selected} audit={audit!} review={review} onAudit={startAudit} />
        {view === "queue" && <ReviewQueue audit={audit!} review={review} session={session} onSaved={refreshReview} />}
        {view === "evidence" && <EvidenceWorkspace audit={audit!} review={review} onSaved={refreshReview} />}
        {view === "repairs" && <RepairsView audit={audit!} session={session} onStarted={async () => {
          await loadManuscripts();
          const audits = await api.audits(selectedId!);
          const latest = audits.items[0] ?? null;
          if (latest) { setAudit(latest); setJob(await api.job(latest.job_id)); }
        }} />}
        {view === "insights" && <>
          <MissingCitationsView audit={audit!} />
          <AuthorOverlapView audit={audit!} />
        </>}
        {view === "exports" && <ExportsView audit={audit!} session={session} />}
        {view === "operations" && <OperationsView />}
        {view === "team" && <TeamView session={session} />}
        {view === "activity" && <ActivityView audit={audit!} />}
      </>}
    </main>
    {uploadOpen && <UploadDialog onClose={() => setUploadOpen(false)} onUploaded={async manuscript => {
      await loadManuscripts(); setSelectedId(manuscript.id); setUploadOpen(false);
    }} />}
  </div>;
}

function Sidebar({ manuscripts, selectedId, onSelect, onUpload }: {
  manuscripts: Manuscript[]; selectedId: string | null; onSelect: (id: string) => void; onUpload: () => void;
}) {
  const [query, setQuery] = useState("");
  const shown = manuscripts.filter(item => item.title.toLowerCase().includes(query.toLowerCase()));
  return <aside className="sidebar" aria-label="Manuscript library">
    <div className="brand"><span className="brand-mark">◆</span><div><strong>CitePrism</strong><small>Evidence assurance</small></div></div>
    <button className="button primary upload" onClick={onUpload}><span>＋</span> New manuscript</button>
    <label className="search"><span>Search manuscripts</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search library" /></label>
    <div className="library-label"><span>Manuscripts</span><span>{shown.length}</span></div>
    <nav className="manuscript-list">
      {shown.map(item => <button key={item.id} className={item.id === selectedId ? "active" : ""} onClick={() => onSelect(item.id)}>
        <span className="document-icon">▱</span><span className="document-copy"><strong>{item.title}</strong><small>{item.open_findings} open · {readable(item.latest_audit_state ?? "not audited")}</small></span>
      </button>)}
      {!shown.length && <p className="muted">No manuscripts match this search.</p>}
    </nav>
    <div className="sidebar-foot"><StatusPill tone="safe">Human review required</StatusPill><p>Model output is evidence to inspect, not a verdict.</p></div>
  </aside>;
}

function Topbar({ view, onView, manuscript, session }: {
  view: View; onView: (view: View) => void; manuscript: Manuscript | null; session: Session | null;
}) {
  const initials = (session?.display_name ?? session?.actor_id ?? "Local reviewer")
    .split(/[\s@._-]+/).filter(Boolean).slice(0, 2).map(part => part[0]!.toUpperCase()).join("");
  return <header className="topbar">
    <div><span className="eyebrow">Review workspace</span><strong>{manuscript?.title ?? "Manuscript library"}</strong></div>
    <nav aria-label="Workspace sections">
      {VIEWS.map(item => <button key={item} className={view === item ? "active" : ""}
        aria-current={view === item ? "page" : undefined}
        onClick={() => onView(item)}>{readable(item)}</button>)}
    </nav>
    <div className="reviewer">
      <span>{initials || "CP"}</span>
      <div>
        <strong>{session?.display_name ?? session?.actor_id ?? "Local reviewer"}</strong>
        <small>{(session?.roles ?? ["reviewer"]).map(readable).join(" · ")}</small>
      </div>
      <RuntimeBadge session={session} />
    </div>
  </header>;
}

function WorkspaceHeader({ manuscript, audit, review, onAudit }: { manuscript: Manuscript; audit: Audit; review: Review; onAudit: () => void }) {
  const counts = review.counts;
  return <section className="workspace-header">
    <div className="breadcrumbs">Manuscripts <span>/</span> {manuscript.title}</div>
    <div className="title-row"><div><div className="status-row"><StatusPill tone={audit.state === "completed" ? "safe" : "warn"}>{readable(audit.state)}</StatusPill><span>Completed {relative(audit.completed_at)}</span></div><h1>{review.title}</h1><p>Review source identity, citation use, and grounded model proposals in one traceable case file.</p></div>
      <button className="button secondary" onClick={onAudit}>Run new audit</button></div>
    <div className="metrics" aria-label="Audit summary">
      <Metric label="References" value={counts.total_references ?? 0} detail={`${counts.resolved_references ?? 0} resolved`} />
      <Metric label="Citation uses" value={counts.citation_occurrences ?? 0} detail="Located in manuscript" />
      <Metric label="Grounded proposals" value={counts.grounded_proposals ?? 0} detail="Awaiting interpretation" />
      <Metric label="Open review items" value={counts.open_findings ?? counts.findings ?? 0} detail={`${counts.system_issues ?? 0} system issues`} emphasis />
    </div>
  </section>;
}

function Metric({ label, value, detail, emphasis = false }: { label: string; value: number; detail: string; emphasis?: boolean }) {
  return <div className={`metric ${emphasis ? "emphasis" : ""}`}><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>;
}

function ReviewQueue({ audit, review, session, onSaved }: {
  audit: Audit; review: Review; session: Session | null; onSaved: () => Promise<void>;
}) {
  const [priority, setPriority] = useState<Priority | "all">("all");
  const [state, setState] = useState<"open" | "decided" | "all">("open");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Finding | null>(null);
  const references = useMemo(() => new Map(review.references.map(item => [item.id, item])), [review.references]);
  const findings = review.findings.filter(item =>
    (priority === "all" || item.priority === priority) &&
    (state === "all" || (state === "decided") === Boolean(item.decision)) &&
    `${item.summary} ${item.explanation} ${item.category}`.toLowerCase().includes(query.toLowerCase())
  );
  return <section className="content-section">
    <div className="section-heading"><div><span className="eyebrow">Human review</span><h2>Review queue</h2><p>Work from the highest consequence items down. Every decision is appended to the activity trail.</p></div></div>
    <div className="toolbar">
      <label className="filter search-wide"><span>Search review items</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search title, issue, or explanation" /></label>
      <label className="filter"><span>Priority</span><select value={priority} onChange={event => setPriority(event.target.value as Priority | "all")}><option value="all">All priorities</option>{priorities.map(item => <option key={item}>{item}</option>)}</select></label>
      <label className="filter"><span>Status</span><select value={state} onChange={event => setState(event.target.value as typeof state)}><option value="open">Open</option><option value="decided">Decided</option><option value="all">All</option></select></label>
      <span className="result-count">{findings.length} items</span>
    </div>
    <div className="queue-list">
      {findings.map(finding => {
        const reference = finding.reference_id ? references.get(finding.reference_id) : undefined;
        return <button className="queue-card" key={finding.id} onClick={() => setSelected(finding)}>
          <span className={`priority-dot ${finding.priority}`} aria-label={`${finding.priority} priority`} />
          <span className="queue-main"><span className="queue-meta">{reference ? `${reference.marker} · ${reference.title}` : "Manuscript-level"}</span><strong>{finding.summary}</strong><small>{finding.explanation}</small></span>
          <span className="queue-end"><StatusPill tone={finding.decision ? "safe" : finding.system_issue ? "danger" : "warn"}>{finding.decision ? readable(finding.decision.disposition) : readable(finding.priority)}</StatusPill><span aria-hidden>›</span></span>
        </button>;
      })}
      {!findings.length && <Empty title="This view is clear" detail="No review items match the current filters." />}
    </div>
    {selected && <DecisionPanel audit={audit} finding={selected} session={session} reference={selected.reference_id ? references.get(selected.reference_id) : undefined} onClose={() => setSelected(null)} onSaved={async () => { setSelected(null); await onSaved(); }} />}
  </section>;
}

function DecisionPanel({ audit, finding, reference, session, onClose, onSaved }: { audit: Audit; finding: Finding; reference?: Reference; session: Session | null; onClose: () => void; onSaved: () => Promise<void> }) {
  const [disposition, setDisposition] = useState<DecisionDisposition>(finding.decision?.disposition ?? "needs_more_evidence");
  const [rationale, setRationale] = useState(finding.decision?.rationale ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), []);
  async function save() {
    setSaving(true); setError("");
    try { await api.decide(audit.id, finding.id, disposition, rationale, finding.decision?.id); await onSaved(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Decision could not be saved"); setSaving(false); }
  }
  return <div className="scrim" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <aside className="decision-panel" role="dialog" aria-modal="true" aria-labelledby="decision-title">
      <div className="panel-head"><div><span className="eyebrow">Reviewer decision</span><h2 id="decision-title" tabIndex={-1} ref={heading}>{finding.summary}</h2></div><button className="icon-button" aria-label="Close decision panel" onClick={onClose}>×</button></div>
      <div className="panel-body">
        <div className="case-context"><span>{reference ? `${reference.marker} · ${reference.title}` : "Manuscript-level finding"}</span><StatusPill tone={finding.priority === "high" ? "danger" : "warn"}>{readable(finding.priority)}</StatusPill></div>
        <section><h3>What the system observed</h3><p>{finding.explanation}</p></section>
        <section className="recommended"><h3>Recommended next step</h3><p>{finding.action}</p></section>
        {finding.system_issue && <section className="recommended"><h3>This is a system issue</h3><p>This item reports a CitePrism processing failure, not a defect in the manuscript. The Repairs tab can rerun the affected stage.</p></section>}
        <section><AssignmentPicker audit={audit} finding={finding} session={session} /></section>
        <fieldset><legend>Disposition</legend><div className="choice-grid">{(["confirm", "dismiss", "needs_more_evidence", "escalate"] as DecisionDisposition[]).map(value => <label key={value} className={disposition === value ? "selected" : ""}><input type="radio" name="disposition" checked={disposition === value} onChange={() => setDisposition(value)} /><span><strong>{readable(value)}</strong><small>{value === "confirm" ? "Evidence supports this review item" : value === "dismiss" ? "Evidence resolves or disproves it" : value === "escalate" ? "Assign specialist review" : "More source evidence is required"}</small></span></label>)}</div></fieldset>
        <label className="rationale"><span>Reviewer rationale <b>Required</b></span><textarea value={rationale} onChange={event => setRationale(event.target.value)} rows={5} placeholder="Record what you checked and why this disposition is appropriate." /></label>
        {error && <p className="field-error" role="alert">{error}</p>}
      </div>
      <div className="panel-actions"><button className="button ghost" onClick={onClose}>Cancel</button><button className="button primary" disabled={saving || rationale.trim().length < 3} onClick={save}>{saving ? "Saving…" : finding.decision ? "Save new revision" : "Record decision"}</button></div>
    </aside>
  </div>;
}

function EvidenceWorkspace({ audit, review, onSaved }: { audit: Audit; review: Review; onSaved: () => Promise<void> }) {
  const [referenceId, setReferenceId] = useState(review.references[0]?.id ?? null);
  const [evidence, setEvidence] = useState<ReferenceEvidence | null>(null);
  const [correctionOpen, setCorrectionOpen] = useState(false);
  useEffect(() => { if (referenceId) api.reference(audit.id, referenceId).then(setEvidence); }, [audit.id, referenceId, review]);
  return <section className="evidence-layout">
    <aside className="reference-rail"><div className="rail-heading"><span className="eyebrow">Reference inventory</span><strong>{review.references.length} entries</strong></div>
      <div className="reference-list">{review.references.map(reference => <button key={reference.id} className={reference.id === referenceId ? "active" : ""} onClick={() => setReferenceId(reference.id)}><span className="marker">{reference.marker}</span><span><strong>{reference.title}</strong><small>{readable(reference.identity_status)} · {reference.citation_count} use{reference.citation_count === 1 ? "" : "s"}</small></span><span className={`health ${reference.evidence_status === "grounded_proposal" ? "good" : "warn"}`} /></button>)}</div>
    </aside>
    <div className="evidence-content">
      {!evidence && <div className="loading"><span />Loading evidence…</div>}
      {evidence && <>
        <div className="evidence-title"><div><span className="eyebrow">{evidence.reference.marker}</span><h2>{evidence.reference.title}</h2><div className="status-row"><StatusPill tone={evidence.reference.identity_status === "resolved" ? "safe" : "warn"}>{readable(evidence.reference.identity_status)}</StatusPill><StatusPill>{readable(evidence.reference.evidence_status)}</StatusPill></div></div><button className="button secondary" onClick={() => setCorrectionOpen(true)}>Propose correction</button></div>
        <div className="comparison-grid">
          <article className="evidence-card"><header><span>01</span><div><strong>Manuscript evidence</strong><small>What the author submitted</small></div></header><section><h3>As cited</h3><blockquote>{evidence.as_cited.raw_text ?? evidence.as_cited.parsed?.title ?? "Printed entry not available"}</blockquote></section>{evidence.claim_assessments.map((assessment, index) => <section key={assessment.task_id ?? index}><h3>Claim at citation</h3><p className="quote">“{assessment.claim_anchor?.text ?? "No anchored claim was accepted."}”</p>{assessment.claim_anchor?.page && <small>Manuscript page {assessment.claim_anchor.page}</small>}</section>)}</article>
          <article className="evidence-card source"><header><span>02</span><div><strong>Source evidence</strong><small>Retrieved snapshot · immutable for this audit</small></div></header><section><h3>Resolved record</h3><p>{String(evidence.external_metadata.title ?? evidence.source_snapshot?.title ?? "No source title")}</p><dl><div><dt>DOI</dt><dd>{String(evidence.external_metadata.doi ?? "Not returned")}</dd></div><div><dt>Year</dt><dd>{String(evidence.external_metadata.year ?? evidence.external_metadata.publication_year ?? "Unknown")}</dd></div></dl></section>{evidence.claim_assessments.flatMap(item => item.validated_quotes ?? []).map((quote, index) => <section key={index}><h3>Located source quotation</h3><blockquote className="source-quote">{quote.text}</blockquote></section>)}</article>
        </div>
        <div className="interpretation"><span className="eyebrow">Interpretation layer</span><h2>Grounded proposals</h2>{evidence.claim_assessments.length ? evidence.claim_assessments.map((item, index) => <div className="proposal" key={item.task_id ?? index}><StatusPill tone={item.status === "grounded_proposal" ? "safe" : "warn"}>{readable(item.decision ?? item.status)}</StatusPill><p>{item.rationale ?? "No model rationale was accepted."}</p><small>Quotation checks establish attribution only. A reviewer must decide scientific meaning.</small></div>) : <p className="muted">No citation-use assessment was eligible for this reference.</p>}</div>
        <a className="button ghost pdf-link" href={api.pdfUrl(audit.id)} target="_blank" rel="noreferrer">Open manuscript PDF ↗</a>
      </>}
    </div>
    {correctionOpen && evidence && <CorrectionDialog audit={audit} evidence={evidence} onClose={() => setCorrectionOpen(false)} onSaved={async () => { setCorrectionOpen(false); await onSaved(); }} />}
  </section>;
}

function CorrectionDialog({ audit, evidence, onClose, onSaved }: { audit: Audit; evidence: ReferenceEvidence; onClose: () => void; onSaved: () => Promise<void> }) {
  const [title, setTitle] = useState(evidence.as_cited.parsed?.title ?? "");
  const [rationale, setRationale] = useState("");
  const [saving, setSaving] = useState(false);
  async function save() { setSaving(true); await api.proposeCorrection(audit.id, [evidence.reference.id], "metadata", { title }, rationale); await onSaved(); }
  return <div className="scrim"><div className="dialog" role="dialog" aria-modal="true" aria-labelledby="correction-title"><div className="panel-head"><div><span className="eyebrow">Non-destructive proposal</span><h2 id="correction-title">Correct bibliography metadata</h2></div><button className="icon-button" onClick={onClose}>×</button></div><p>The submitted entry and original evidence artifact remain unchanged. This creates a reviewable correction proposal.</p><label className="rationale"><span>Proposed title</span><input value={title} onChange={event => setTitle(event.target.value)} /></label><label className="rationale"><span>Rationale</span><textarea rows={4} value={rationale} onChange={event => setRationale(event.target.value)} placeholder="State the authoritative record you checked." /></label><div className="panel-actions"><button className="button ghost" onClick={onClose}>Cancel</button><button className="button primary" disabled={saving || !title.trim() || rationale.trim().length < 3} onClick={save}>{saving ? "Saving…" : "Create proposal"}</button></div></div></div>;
}

function ActivityView({ audit }: { audit: Audit }) {
  const [items, setItems] = useState<Activity[]>([]);
  useEffect(() => { api.activity(audit.id).then(result => setItems(result.items)); }, [audit.id]);
  return <section className="content-section"><div className="section-heading"><div><span className="eyebrow">Auditability</span><h2>Activity trail</h2><p>Operational events and human actions are retained in order. Evidence provenance remains in the immutable trust ledger.</p></div></div><ol className="timeline">{items.map(item => <li key={item.id}><span className="timeline-dot" /><div><div><strong>{readable(item.action)}</strong><time>{relative(item.created_at)}</time></div><p>{item.actor_id} · {readable(item.entity_type)}</p><code>{item.entity_id}</code></div></li>)}</ol></section>;
}

function JobBanner({ job }: { job: Job }) {
  return <div className="job-banner" role="status"><div className="job-spinner" /><div><strong>Audit in progress</strong><span>{job.stage}</span></div><progress className="job-progress" value={job.progress} max="100">{job.progress}%</progress><strong>{job.progress}%</strong></div>;
}

function UploadDialog({ onClose, onUploaded }: { onClose: () => void; onUploaded: (value: Manuscript) => Promise<void> }) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function upload() { if (!file) return; setBusy(true); setError(""); try { await onUploaded(await api.upload(file, title || undefined)); } catch (reason) { setError(reason instanceof ApiError ? reason.message : "Upload failed"); setBusy(false); } }
  return <div className="scrim"><div className="dialog upload-dialog" role="dialog" aria-modal="true" aria-labelledby="upload-title"><div className="panel-head"><div><span className="eyebrow">New case</span><h2 id="upload-title">Upload a manuscript</h2></div><button className="icon-button" onClick={onClose}>×</button></div><p>Use a text-based PDF up to 50 MiB. Scanned documents need OCR before review.</p><label className="drop-zone"><input type="file" accept="application/pdf,.pdf" onChange={event => setFile(event.target.files?.[0] ?? null)} /><span className="drop-icon">⇧</span><strong>{file?.name ?? "Choose a PDF manuscript"}</strong><small>{file ? `${(file.size / 1024 / 1024).toFixed(1)} MiB selected` : "PDF · maximum 50 MiB"}</small></label><label className="rationale"><span>Case title <small>Optional</small></span><input value={title} onChange={event => setTitle(event.target.value)} placeholder="Defaults to the PDF filename" /></label>{error && <p className="field-error">{error}</p>}<div className="panel-actions"><button className="button ghost" onClick={onClose}>Cancel</button><button className="button primary" disabled={!file || busy} onClick={upload}>{busy ? "Uploading…" : "Create case"}</button></div></div></div>;
}
