import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { AskView, MarginMark, explainFailure } from "./ask";
import {
  AssignmentPicker, AuditOptionsDialog, AuthorOverlapView, ExportsView, MissingCitationsView,
  OperationsView, RelevancePill, RepairsView, RuntimeBadge, TeamView, useSession,
} from "./phase4";
import type {
  Activity, Audit, DecisionDisposition, Finding, Job, Manuscript, Priority,
  Reference, ReferenceEvidence, Review, Session,
} from "./types";

type Mode = "ask" | "review";
type Tab = "items" | "references" | "insights" | "export" | "repairs" | "activity" | "team" | "operations";

const PRIMARY_TABS: Array<{ id: Tab; label: string }> = [
  { id: "items", label: "Items to check" },
  { id: "references", label: "References" },
  { id: "insights", label: "Self-citation & missing works" },
  { id: "export", label: "Export" },
];
const MORE_TABS: Array<{ id: Tab; label: string; detail: string }> = [
  { id: "repairs", label: "Repairs", detail: "Re-run checks that failed for technical reasons" },
  { id: "activity", label: "Activity", detail: "Every decision and event, in order" },
  { id: "team", label: "Team", detail: "Reviewers and their workload" },
  { id: "operations", label: "Operations", detail: "Speed, cost and service targets" },
];
const priorities: Priority[] = ["high", "moderate", "low", "informational"];
const PRIORITY_TONE: Record<Priority, string> = { high: "warning", moderate: "caution", low: "neutral", informational: "neutral" };
const PRIORITY_WORD: Record<Priority, string> = { high: "Serious", moderate: "Worth a look", low: "Minor", informational: "For information" };
const DISPOSITION_WORD: Record<DecisionDisposition, string> = {
  confirm: "Confirmed", dismiss: "Dismissed", escalate: "Escalated", needs_more_evidence: "Needs more evidence",
};

function readable(value: string) {
  return value.replaceAll("_", " ").replace(/^\w/, character => character.toUpperCase());
}

function relative(date?: string | null) {
  if (!date) return "not yet";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(date));
}

function remembered<T extends string>(key: string, fallback: T): T {
  try { return (window.localStorage.getItem(key) as T | null) ?? fallback; } catch { return fallback; }
}

function remember(key: string, value: string) {
  try { window.localStorage.setItem(key, value); } catch { /* storage may be unavailable */ }
}

function StatusPill({ children, tone = "neutral" }: { children: React.ReactNode; tone?: string }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

function Empty({ title, detail, action }: { title: string; detail: string; action?: React.ReactNode }) {
  return <div className="empty"><h2>{title}</h2><p>{detail}</p>{action}</div>;
}

const ACTIVE_JOB = ["queued", "running", "cancel_requested"];

export function App() {
  const [manuscripts, setManuscripts] = useState<Manuscript[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [audit, setAudit] = useState<Audit | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [mode, setModeState] = useState<Mode>(() => remembered<Mode>("citeprism.mode", "ask"));
  const [tab, setTab] = useState<Tab>("items");
  const [focusReference, setFocusReference] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const session = useSession();

  const selected = manuscripts.find(item => item.id === selectedId) ?? null;

  function setMode(value: Mode) { setModeState(value); remember("citeprism.mode", value); }

  const loadManuscripts = useCallback(async () => {
    const result = await api.manuscripts();
    setManuscripts(result.items);
    return result.items;
  }, []);

  useEffect(() => {
    loadManuscripts().then(items => setSelectedId(current => current ?? items[0]?.id ?? null))
      .catch(reason => setError(reason.message)).finally(() => setBusy(false));
  }, [loadManuscripts]);

  useEffect(() => {
    if (!selectedId) { setAudit(null); setReview(null); setJob(null); return; }
    setBusy(true); setFocusReference(null);
    api.audits(selectedId).then(async result => {
      const latest = result.items[0] ?? null;
      setAudit(latest);
      setReview(latest?.artifact_path ? await api.review(latest.id) : null);
      if (latest && (latest.state === "queued" || latest.state === "processing")) setJob(await api.job(latest.job_id));
      else setJob(null);
    }).catch(reason => setError(reason.message)).finally(() => setBusy(false));
  }, [selectedId]);

  useEffect(() => {
    if (!job || !ACTIVE_JOB.includes(job.state)) return;
    const timer = window.setInterval(async () => {
      try {
        const current = await api.job(job.id);
        setJob(current);
        if (["succeeded", "failed", "cancelled"].includes(current.state)) {
          window.clearInterval(timer);
          const finished = await api.audit(current.audit_id);
          setAudit(finished);
          await loadManuscripts();
          if (current.state === "succeeded") setReview(await api.review(finished.id));
          setJob(null);
        }
      } catch (reason) { setError(reason instanceof Error ? reason.message : "Lost contact with the server."); }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [job, loadManuscripts]);

  async function upload(file: File) {
    const manuscript = await api.upload(file);
    await loadManuscripts();
    setSelectedId(manuscript.id); setMode("ask");
  }

  async function runAudit(options: { run_missing_citations: boolean; author_orcids: string[] }) {
    if (!selectedId) return;
    setError(null);
    try {
      const result = await api.startAudit(selectedId, options);
      setAudit(result.audit); setJob(result.job); setReview(null); setOptionsOpen(false);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "The check could not start."); }
  }

  async function refreshReview() {
    if (audit) setReview(await api.review(audit.id));
    await loadManuscripts();
  }

  function openReference(id: string) {
    setFocusReference(id); setTab("references"); setMode("review");
  }

  function openTab(value: Tab) { setTab(value); setMode("review"); }

  const running = Boolean(job && ACTIVE_JOB.includes(job.state));

  return <div className={`app ${libraryOpen ? "library-open" : ""}`}>
    <a className="skip-link" href="#main">Skip to content</a>
    <Masthead mode={mode} onMode={setMode}
      onLibrary={() => setLibraryOpen(open => !open)} onMore={openTab} session={session}
      moreActive={MORE_TABS.some(item => item.id === tab) && mode === "review"} />
    <Library manuscripts={manuscripts} selectedId={selectedId}
      onNew={() => { setSelectedId(null); setMode("ask"); setLibraryOpen(false); }}
      onSelect={id => { setSelectedId(id); setLibraryOpen(false); }} />
    <main id="main" className={`main main-${mode}`}>
      {error && <div className="notice notice-error" role="alert">
        <div><strong>{explainFailure(error).title}.</strong> <span>{explainFailure(error).detail}</span>
          <small>{error}</small></div>
        <button className="button ghost" onClick={() => setError(null)}>Dismiss</button></div>}
      {busy && <p className="loading" role="status">Loading…</p>}

      {!busy && mode === "ask" && <AskView manuscript={selected} audit={audit} job={job} hasReview={Boolean(review)}
        dataHandling={session?.features.data_handling ?? null} onUpload={upload} onStart={runAudit} onOpenReference={openReference}
        onSplit={async first => { await loadManuscripts(); setSelectedId(first); }} />}

      {!busy && mode === "review" && <>
        {!selected && <Empty title="No paper selected" detail="Choose a paper from the library, or start a new check."
          action={<button className="button primary" onClick={() => setMode("ask")}>Check a paper</button>} />}
        {selected && running && <JobBanner job={job!} />}
        {selected && !review && !running && !MORE_TABS.some(item => item.id === tab) &&
          <Empty title={audit?.state === "failed" ? explainFailure(audit.error_message).title : "No results yet"}
            detail={audit?.state === "failed" ? explainFailure(audit.error_message).detail
              : "Start a check to see which citations need attention."}
            action={<button className="button primary" onClick={() => setOptionsOpen(true)}>
              {audit ? "Run the check again" : "Start the check"}</button>} />}
        {selected && (review || MORE_TABS.some(item => item.id === tab)) && <>
          {review && audit && <PaperHeader audit={audit} review={review} onRun={() => setOptionsOpen(true)} disabled={running} />}
          <nav className="tabs" aria-label="Review sections">
            {PRIMARY_TABS.map(item => <button key={item.id} className={tab === item.id ? "active" : ""}
              aria-current={tab === item.id ? "page" : undefined} disabled={!review}
              onClick={() => setTab(item.id)}>{item.label}{item.id === "items" && review
                ? <span className="tab-count">{openAttention(review)}</span> : null}</button>)}
            {MORE_TABS.some(item => item.id === tab) && <button className="active" aria-current="page">
              {MORE_TABS.find(item => item.id === tab)!.label}</button>}
          </nav>
          {review && audit && tab === "items" && <ReviewQueue audit={audit} review={review} session={session}
            onSaved={refreshReview} onOpenReference={openReference} />}
          {review && audit && tab === "references" && <EvidenceWorkspace audit={audit} review={review}
            focus={focusReference} onSaved={refreshReview} />}
          {audit && tab === "insights" && <div className="stack"><AuthorOverlapView audit={audit} /><MissingCitationsView audit={audit} /></div>}
          {audit && tab === "export" && <ExportsView audit={audit} session={session} />}
          {audit && tab === "repairs" && <RepairsView audit={audit} session={session} onStarted={async () => {
            await loadManuscripts();
            const audits = await api.audits(selectedId!);
            const latest = audits.items[0] ?? null;
            if (latest) { setAudit(latest); setJob(await api.job(latest.job_id)); }
          }} />}
          {tab === "operations" && <OperationsView />}
          {tab === "team" && <TeamView session={session} />}
          {audit && tab === "activity" && <ActivityView audit={audit} />}
        </>}
      </>}
    </main>
    {optionsOpen && <AuditOptionsDialog onClose={() => setOptionsOpen(false)} onStart={runAudit} />}
  </div>;
}

/* ------------------------------------------------------------------ */
/* Masthead                                                             */
/* ------------------------------------------------------------------ */
function Masthead({ mode, onMode, onLibrary, onMore, session, moreActive }: {
  mode: Mode; onMode: (mode: Mode) => void; onLibrary: () => void;
  onMore: (tab: Tab) => void; session: Session | null; moreActive: boolean;
}) {
  const [open, setOpen] = useState(false);
  const menu = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === "Escape" : !menu.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close); document.addEventListener("keydown", close);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", close); };
  }, [open]);
  return <header className="masthead">
    <button className="library-toggle" aria-label="Show library" onClick={onLibrary}><span /><span /><span /></button>
    <div className="wordmark"><span className="wordmark-prism" aria-hidden />CitePrism</div>
    <div className="modes" role="tablist" aria-label="Mode">
      <button role="tab" aria-selected={mode === "ask"} className={mode === "ask" ? "active" : ""} onClick={() => onMode("ask")}>Ask<span className="wide-only"> CitePrism</span></button>
      <button role="tab" aria-selected={mode === "review"} className={mode === "review" ? "active" : ""} onClick={() => onMode("review")}>Review<span className="wide-only"> in detail</span></button>
    </div>
    <div className="masthead-end">
      <div className="menu" ref={menu}>
        <button className={`button ghost ${moreActive ? "active" : ""}`} aria-expanded={open} aria-haspopup="menu" onClick={() => setOpen(value => !value)}>More</button>
        {open && <div className="menu-list" role="menu">
          {MORE_TABS.map(item => <button role="menuitem" key={item.id} onClick={() => { setOpen(false); onMore(item.id); }}>
            <strong>{item.label}</strong><small>{item.detail}</small></button>)}
          <div className="menu-foot"><RuntimeBadge session={session} /></div>
        </div>}
      </div>
    </div>
  </header>;
}

/* ------------------------------------------------------------------ */
/* Library                                                              */
/* ------------------------------------------------------------------ */
function stateSentence(item: Manuscript) {
  switch (item.latest_audit_state) {
    case null: return "Not checked yet";
    case "queued": case "processing": return "Checking…";
    case "failed": return "Check stopped";
    case "cancelled": return "Check cancelled";
    default: return item.open_findings ? `${item.open_findings} reference${item.open_findings === 1 ? "" : "s"} to look at` : "Nothing needs a look";
  }
}

function Library({ manuscripts, selectedId, onSelect, onNew }: {
  manuscripts: Manuscript[]; selectedId: string | null; onSelect: (id: string) => void; onNew: () => void;
}) {
  const [query, setQuery] = useState("");
  const shown = manuscripts.filter(item => item.title.toLowerCase().includes(query.toLowerCase()));
  return <aside className="library" aria-label="Your papers">
    <button className="button library-new" onClick={onNew}>Check a new paper</button>
    <h2 className="library-title">Your papers</h2>
    {manuscripts.length > 5 && <label className="library-search"><span className="visually-hidden">Search papers</span>
      <input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search papers" /></label>}
    <nav className="library-list">
      {shown.map(item => <button key={item.id} className={item.id === selectedId ? "active" : ""}
        aria-current={item.id === selectedId ? "true" : undefined} onClick={() => onSelect(item.id)}>
        <cite>{item.title}</cite>
        <small className={`state state-${item.latest_audit_state ?? "new"}`}>{stateSentence(item)}</small>
      </button>)}
      {!manuscripts.length && <p className="muted">Papers you check appear here.</p>}
      {manuscripts.length > 0 && !shown.length && <p className="muted">No paper matches “{query}”.</p>}
    </nav>
    <p className="library-foot">Results point to what is worth checking. A person makes every decision.</p>
  </aside>;
}

/* ------------------------------------------------------------------ */
/* Review mode                                                          */
/* ------------------------------------------------------------------ */
/* Same rule as the library card: references with an open, non-technical finding. */
function openAttention(review: Review) {
  const open = review.findings.filter(item => item.priority !== "informational" && !item.decision && !item.system_issue);
  return new Set(open.map(item => item.reference_id ?? `paper:${item.category}`)).size;
}

function technicalGaps(review: Review) {
  return review.findings.filter(item => item.system_issue && !item.decision).length;
}

function PaperHeader({ audit, review, onRun, disabled }: { audit: Audit; review: Review; onRun: () => void; disabled: boolean }) {
  const counts = review.counts;
  const tallies: Array<[number, string]> = [
    [counts.total_references ?? 0, "references"],
    [counts.resolved_references ?? 0, "found in databases"],
    [openAttention(review), "to look at"],
    [counts.potentially_irrelevant ?? 0, "possibly irrelevant"],
    [counts.self_citations ?? 0, "self-citations"],
    [counts.metadata_inconsistencies ?? 0, "detail mismatches"],
  ];
  const gaps = technicalGaps(review);
  if (gaps) tallies.push([gaps, gaps === 1 ? "check did not finish" : "checks did not finish"]);
  return <section className="paper">
    <div className="paper-row">
      <div>
        <cite className="paper-title">{review.title}</cite>
        <p className="paper-meta">{audit.state === "completed_with_issues"
          ? "Checked with some gaps — see Repairs for steps that did not finish."
          : "Checked"} {relative(audit.completed_at)}</p>
      </div>
      <button className="button" onClick={onRun} disabled={disabled}>Run the check again</button>
    </div>
    <dl className="tallies">{tallies.map(([value, label]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
  </section>;
}

function referenceTones(review: Review) {
  const rank: Record<Priority, number> = { high: 3, moderate: 2, low: 1, informational: 0 };
  const best = new Map<string, number>();
  for (const finding of review.findings) {
    if (!finding.reference_id || finding.system_issue) continue;
    best.set(finding.reference_id, Math.max(best.get(finding.reference_id) ?? 0, rank[finding.priority]));
  }
  return (id: string) => ({ 3: "warning", 2: "caution", 1: "neutral", 0: "ok" } as Record<number, string>)[best.get(id) ?? 0];
}

function ReviewQueue({ audit, review, session, onSaved, onOpenReference }: {
  audit: Audit; review: Review; session: Session | null; onSaved: () => Promise<void>; onOpenReference: (id: string) => void;
}) {
  const [priority, setPriority] = useState<Priority | "attention" | "all">("attention");
  const [state, setState] = useState<"open" | "decided" | "all">("open");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Finding | null>(null);
  const references = useMemo(() => new Map(review.references.map(item => [item.id, item])), [review.references]);
  const findings = review.findings
    .filter(item => (priority === "all" || item.priority === priority ||
      (priority === "attention" && item.priority !== "informational")) &&
      (state === "all" || (state === "decided") === Boolean(item.decision)) &&
      `${item.summary} ${item.explanation} ${references.get(item.reference_id ?? "")?.title ?? ""}`.toLowerCase().includes(query.toLowerCase()))
    .sort((a, b) => priorities.indexOf(a.priority) - priorities.indexOf(b.priority));
  return <section className="section">
    <div className="toolbar">
      <label className="field grow"><span className="visually-hidden">Search items</span>
        <input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search by reference title or issue" /></label>
      <label className="field"><span className="visually-hidden">Seriousness</span>
        <select value={priority} onChange={event => setPriority(event.target.value as Priority | "attention" | "all")}>
          <option value="attention">Everything worth a look</option>
          {priorities.map(item => <option key={item} value={item}>{PRIORITY_WORD[item]} only</option>)}
          <option value="all">Everything, including notes for information</option></select></label>
      <label className="field"><span className="visually-hidden">Status</span>
        <select value={state} onChange={event => setState(event.target.value as typeof state)}>
          <option value="open">Not yet decided</option><option value="decided">Decided</option><option value="all">All</option></select></label>
    </div>
    <p className="count-line">{findings.length} item{findings.length === 1 ? "" : "s"}, most serious first{priority === "attention"
      ? ` · ${review.findings.filter(item => item.priority === "informational").length} notes for information hidden` : ""}</p>
    <ul className="items">
      {findings.map(finding => {
        const reference = finding.reference_id ? references.get(finding.reference_id) : undefined;
        return <li key={finding.id} className={finding.decision ? "decided" : ""}>
          <button className="item" onClick={() => setSelected(finding)}>
            {reference ? <MarginMark marker={reference.marker} tone={PRIORITY_TONE[finding.priority]} />
              : <span className={`mark mark-${PRIORITY_TONE[finding.priority]} mark-paper`} aria-label="Whole paper">¶</span>}
            <span className="item-copy">
              <strong>{finding.summary}</strong>
              {reference && <cite>{reference.title}</cite>}
              <span className="item-detail">{finding.explanation}</span>
            </span>
            <span className="item-state">{finding.decision
              ? <StatusPill tone="safe">{DISPOSITION_WORD[finding.decision.disposition]}</StatusPill>
              : finding.system_issue ? <StatusPill tone="neutral">Technical</StatusPill>
              : <span className={`severity severity-${finding.priority}`}>{PRIORITY_WORD[finding.priority]}</span>}</span>
          </button>
        </li>;
      })}
    </ul>
    {!findings.length && <Empty title="Nothing here" detail={state === "open" ? "Every item matching these filters has a decision." : "No items match these filters."} />}
    {selected && <DecisionPanel audit={audit} finding={selected} session={session}
      reference={selected.reference_id ? references.get(selected.reference_id) : undefined}
      onOpenReference={onOpenReference}
      onClose={() => setSelected(null)} onSaved={async () => { setSelected(null); await onSaved(); }} />}
  </section>;
}

const DISPOSITION_HELP: Record<DecisionDisposition, string> = {
  confirm: "The concern holds after checking",
  dismiss: "Checked — not a problem",
  needs_more_evidence: "Cannot decide without the full source",
  escalate: "Needs a specialist",
};

function DecisionPanel({ audit, finding, reference, session, onClose, onSaved, onOpenReference }: {
  audit: Audit; finding: Finding; reference?: Reference; session: Session | null;
  onClose: () => void; onSaved: () => Promise<void>; onOpenReference: (id: string) => void;
}) {
  const [disposition, setDisposition] = useState<DecisionDisposition>(finding.decision?.disposition ?? "confirm");
  const [rationale, setRationale] = useState(finding.decision?.rationale ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), []);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [onClose]);
  async function save() {
    setSaving(true); setError("");
    try { await api.decide(audit.id, finding.id, disposition, rationale, finding.decision?.id); await onSaved(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "The decision could not be saved."); setSaving(false); }
  }
  return <div className="scrim" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <aside className="drawer" role="dialog" aria-modal="true" aria-labelledby="decision-title">
      <div className="drawer-head">
        <div>{reference && <p className="drawer-ref"><MarginMark marker={reference.marker} tone={PRIORITY_TONE[finding.priority]} /><cite>{reference.title}</cite></p>}
          <h2 id="decision-title" tabIndex={-1} ref={heading}>{finding.summary}</h2></div>
        <button className="close" aria-label="Close" onClick={onClose}>×</button>
      </div>
      <div className="drawer-body">
        <section><h3>What CitePrism noticed</h3><p>{finding.explanation}</p></section>
        <section><h3>Suggested next step</h3><p>{finding.action}</p>
          {reference && <button className="link" onClick={() => onOpenReference(reference.id)}>Compare the citing sentence with the source</button>}</section>
        {finding.system_issue && <section className="aside-note"><h3>This is a technical problem, not a problem with the paper</h3>
          <p>Use More → Repairs to re-run the step that failed.</p></section>}
        <AssignmentPicker audit={audit} finding={finding} session={session} />
        <fieldset className="choices"><legend>Your decision</legend>
          {(["confirm", "dismiss", "needs_more_evidence", "escalate"] as DecisionDisposition[]).map(value =>
            <label key={value} className={disposition === value ? "selected" : ""}>
              <input type="radio" name="disposition" checked={disposition === value} onChange={() => setDisposition(value)} />
              <span><strong>{DISPOSITION_WORD[value]}</strong><small>{DISPOSITION_HELP[value]}</small></span></label>)}
        </fieldset>
        <label className="field"><span>Why? <small>required — saved to the activity record</small></span>
          <textarea value={rationale} onChange={event => setRationale(event.target.value)} rows={4}
            placeholder="What did you check, and what did you find?" /></label>
        {error && <p className="field-error" role="alert">{error}</p>}
      </div>
      <div className="drawer-actions"><button className="button ghost" onClick={onClose}>Cancel</button>
        <button className="button primary" disabled={saving || rationale.trim().length < 3} onClick={save}>
          {saving ? "Saving…" : finding.decision ? "Save new decision" : "Save decision"}</button></div>
    </aside>
  </div>;
}

function EvidenceWorkspace({ audit, review, focus, onSaved }: { audit: Audit; review: Review; focus: string | null; onSaved: () => Promise<void> }) {
  const [referenceId, setReferenceId] = useState(focus ?? review.references[0]?.id ?? null);
  const [evidence, setEvidence] = useState<ReferenceEvidence | null>(null);
  const [problem, setProblem] = useState("");
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [query, setQuery] = useState("");
  const tone = useMemo(() => referenceTones(review), [review]);
  useEffect(() => { if (focus) setReferenceId(focus); }, [focus]);
  useEffect(() => {
    if (!referenceId) return;
    setEvidence(null); setProblem("");
    api.reference(audit.id, referenceId).then(setEvidence)
      .catch(reason => setProblem(reason instanceof Error ? reason.message : "This reference could not load."));
  }, [audit.id, referenceId, review]);
  const shown = review.references.filter(item => `${item.marker} ${item.title}`.toLowerCase().includes(query.toLowerCase()));
  const findings = review.findings.filter(item => item.reference_id === referenceId)
    .sort((a, b) => priorities.indexOf(a.priority) - priorities.indexOf(b.priority));
  return <section className="evidence">
    <aside className="rail" aria-label="References">
      <label className="field"><span className="visually-hidden">Find a reference</span>
        <input value={query} onChange={event => setQuery(event.target.value)} placeholder={`Find among ${review.references.length} references`} /></label>
      <ul className="rail-list">{shown.map(reference => <li key={reference.id}>
        <button className={reference.id === referenceId ? "active" : ""} aria-current={reference.id === referenceId ? "true" : undefined}
          onClick={() => setReferenceId(reference.id)}>
          <MarginMark marker={reference.marker} tone={tone(reference.id)} />
          <span><cite>{reference.title}</cite>
            <small>{reference.identity_status === "resolved" ? "" : "Not found · "}
              {reference.relevance_score != null ? `Relevance ${Math.round(reference.relevance_score)}` : "Not scored"}
              {reference.self_citation === "orcid" || reference.self_citation === "name_initial" ? " · Self-citation" : ""}</small></span>
        </button></li>)}</ul>
    </aside>
    <div className="evidence-body">
      {problem && <p className="field-error" role="alert">{problem}</p>}
      {!evidence && !problem && <p className="loading" role="status">Loading the evidence…</p>}
      {evidence && <>
        <header className="evidence-head">
          <MarginMark marker={evidence.reference.marker} tone={tone(evidence.reference.id)} />
          <div><cite className="evidence-title">{evidence.reference.title}</cite>
            <div className="pills">
              <StatusPill tone={evidence.reference.identity_status === "resolved" ? "safe" : "warn"}>
                {evidence.reference.identity_status === "resolved" ? "Found in databases" : readable(evidence.reference.identity_status)}</StatusPill>
              <RelevancePill label={evidence.reference.relevance_label} score={evidence.reference.relevance_score} basis={evidence.reference.relevance_basis} />
              {evidence.reference.evidence_kind === "full_text" && <StatusPill tone="safe">Checked against full text</StatusPill>}
            </div></div>
        </header>
        {findings.length > 0 && <ul className="finding-notes">{findings.map(item => <li key={item.id} className={`finding-${item.priority}`}>
          <strong>{item.summary}.</strong> {item.explanation}</li>)}</ul>}
        <div className="compare">
          <article><h3>In the paper</h3>
            <p className="label">As listed in the references</p>
            <blockquote>{evidence.as_cited.raw_text ?? evidence.as_cited.parsed?.title ?? "The printed entry is not available."}</blockquote>
            {evidence.claim_assessments.map((assessment, index) => <div key={assessment.task_id ?? index}>
              <p className="label">Where it is cited{assessment.claim_anchor?.page ? `, page ${assessment.claim_anchor.page}` : ""}</p>
              <blockquote className="claim">{assessment.claim_anchor?.text ?? "No citing sentence was located."}</blockquote></div>)}
          </article>
          <article><h3>In the source</h3>
            <p className="label">Published record</p>
            <p className="record">{String(evidence.external_metadata.title ?? evidence.source_snapshot?.title ?? "No record found")}</p>
            <dl className="record-meta">
              <div><dt>DOI</dt><dd>{evidence.external_metadata.doi ? <a href={`https://doi.org/${String(evidence.external_metadata.doi)}`} target="_blank" rel="noreferrer">{String(evidence.external_metadata.doi)}</a> : "none on record"}</dd></div>
              <div><dt>Year</dt><dd>{String(evidence.external_metadata.year ?? evidence.external_metadata.publication_year ?? "unknown")}</dd></div>
            </dl>
            {evidence.claim_assessments.flatMap(item => item.validated_quotes ?? []).map((quote, index) =>
              <div key={index}><p className="label">Passage the check relied on (found word for word)</p>
                <blockquote className="source-quote">{quote.text}</blockquote></div>)}
          </article>
        </div>
        <section className="verdicts"><h3>What the AI check proposed</h3>
          {evidence.claim_assessments.length ? evidence.claim_assessments.map((item, index) => <div className="verdict" key={item.task_id ?? index}>
            <StatusPill tone={item.status === "grounded_proposal" ? (item.decision === "supports" ? "safe" : "warn") : "neutral"}>{readable(item.decision ?? item.status)}</StatusPill>
            <p>{item.rationale ?? "No reasoning was accepted for this citation."}</p></div>)
            : <p className="muted">This reference was not checked against a source.</p>}
          <p className="muted">A proposal, not a verdict: the quotes are verified, the interpretation is yours to make.</p>
        </section>
        <div className="evidence-actions">
          <a className="button ghost" href={api.pdfUrl(audit.id)} target="_blank" rel="noreferrer">Open the paper (PDF)</a>
          <button className="button ghost" onClick={() => setCorrectionOpen(true)}>Suggest a correction to this entry</button>
        </div>
      </>}
    </div>
    {correctionOpen && evidence && <CorrectionDialog audit={audit} evidence={evidence} onClose={() => setCorrectionOpen(false)} onSaved={async () => { setCorrectionOpen(false); await onSaved(); }} />}
  </section>;
}

function CorrectionDialog({ audit, evidence, onClose, onSaved }: { audit: Audit; evidence: ReferenceEvidence; onClose: () => void; onSaved: () => Promise<void> }) {
  const [title, setTitle] = useState(String(evidence.external_metadata.title ?? evidence.as_cited.parsed?.title ?? ""));
  const [rationale, setRationale] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    setSaving(true); setError("");
    try { await api.proposeCorrection(audit.id, [evidence.reference.id], "metadata", { title }, rationale); await onSaved(); }
    catch (reason) { setError(reason instanceof ApiError ? reason.message : "The suggestion could not be saved."); setSaving(false); }
  }
  return <div className="scrim"><div className="dialog" role="dialog" aria-modal="true" aria-labelledby="correction-title">
    <div className="drawer-head"><h2 id="correction-title">Suggest a correction</h2><button className="close" aria-label="Close" onClick={onClose}>×</button></div>
    <p>The paper and the audit stay unchanged; this records a suggestion that others can review.</p>
    <label className="field"><span>Correct title</span><input value={title} onChange={event => setTitle(event.target.value)} /></label>
    <label className="field"><span>Where did you confirm it?</span><textarea rows={3} value={rationale} onChange={event => setRationale(event.target.value)} placeholder="For example: the publisher's page, or the DOI record" /></label>
    {error && <p className="field-error" role="alert">{error}</p>}
    <div className="drawer-actions"><button className="button ghost" onClick={onClose}>Cancel</button>
      <button className="button primary" disabled={saving || !title.trim() || rationale.trim().length < 3} onClick={save}>{saving ? "Saving…" : "Save suggestion"}</button></div>
  </div></div>;
}

function ActivityView({ audit }: { audit: Audit }) {
  const [items, setItems] = useState<Activity[]>([]);
  useEffect(() => { api.activity(audit.id).then(result => setItems(result.items)); }, [audit.id]);
  return <section className="section"><h2 className="section-title">Activity</h2>
    <p className="section-lead">Every event and decision, in order. Evidence records are kept separately and cannot be edited.</p>
    <ol className="timeline">{items.map(item => <li key={item.id}><div><strong>{readable(item.action)}</strong><time>{relative(item.created_at)}</time></div>
      <p>{item.actor_id} — {readable(item.entity_type).toLowerCase()}</p></li>)}</ol>
    {!items.length && <p className="muted">Nothing recorded yet.</p>}</section>;
}

function JobBanner({ job }: { job: Job }) {
  return <div className="job-banner" role="status"><strong>Checking…</strong><span>{job.stage.replace(/^[✓❌]\s*/, "")}</span>
    <progress value={job.progress} max={100}>{job.progress}%</progress></div>;
}
