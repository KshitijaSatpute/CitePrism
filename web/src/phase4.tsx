import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type {
  Assignment, Audit, AuthorOverlap, ExportRecord, Finding, Member, MissingCitations,
  RepairPlanResponse, Session, SloReport,
} from "./types";

export function readable(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, character => character.toUpperCase());
}

function when(date?: string | null) {
  if (!date) return "—";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" })
    .format(new Date(date));
}

function bytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

function Pill({ children, tone = "neutral" }: { children: React.ReactNode; tone?: string }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

function Panel({ eyebrow, title, blurb, children }: {
  eyebrow: string; title: string; blurb: string; children: React.ReactNode;
}) {
  return <section className="content-section">
    <div className="section-heading"><div>
      <span className="eyebrow">{eyebrow}</span><h2>{title}</h2><p>{blurb}</p>
    </div></div>
    {children}
  </section>;
}

function Loading({ label }: { label: string }) {
  return <div className="loading" role="status"><span />{label}</div>;
}

function Problem({ message }: { message: string }) {
  return <div className="notice notice-error" role="alert">
    <strong>This view could not load.</strong><span>{message}</span>
  </div>;
}

/** Load an async resource, exposing loading and error states without throwing. */
function useResource<T>(loader: () => Promise<T>, dependencies: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const run = useCallback(() => {
    setBusy(true); setError(null);
    loader()
      .then(setData)
      .catch(reason => setError(reason instanceof Error ? reason.message : "Request failed"))
      .finally(() => setBusy(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, dependencies);
  useEffect(run, [run]);
  return { data, error, busy, reload: run };
}

/* ------------------------------------------------------------------ */
/* Repairs                                                             */
/* ------------------------------------------------------------------ */
export function RepairsView({ audit, session, onStarted }: {
  audit: Audit; session: Session | null; onStarted: (auditId: string) => void;
}) {
  const { data, error, busy, reload } = useResource<RepairPlanResponse>(
    () => api.repairPlan(audit.id), [audit.id]);
  const [selected, setSelected] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const allowed = session?.permissions.includes("repair.run") ?? true;

  useEffect(() => {
    setSelected((data?.plans ?? []).filter(plan => plan.automatic).map(p => p.issue_category));
  }, [data]);

  async function run() {
    setRunning(true); setMessage(null);
    try {
      const result = await api.runRepair(audit.id, selected);
      setMessage(result.note);
      onStarted(result.audit.id);
      reload();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "The repair could not start");
    } finally { setRunning(false); }
  }

  if (busy) return <Loading label="Checking for repairable issues…" />;
  if (error) return <Problem message={error} />;
  if (!data) return null;

  return <Panel eyebrow="Automatic repair" title="System issues"
    blurb="These items report CitePrism processing failures, not defects in the manuscript. A repair starts a new audit and never modifies the stored evidence artifact.">
    {!data.plans.length && <div className="empty">
      <div className="empty-mark">◇</div><h2>No system issues</h2>
      <p>Every finding in this audit describes the manuscript rather than our processing.</p>
    </div>}

    {data.plans.map(plan => <article className="evidence-card" key={plan.issue_category}>
      <header>
        <span>{plan.affected_findings}</span>
        <div><strong>{plan.title}</strong><small>{readable(plan.issue_category)}</small></div>
      </header>
      <section>
        <p>{plan.explanation}</p>
        <p><strong>What the repair does:</strong> {plan.expected_outcome}</p>
        {plan.caveat && <p className="muted">{plan.caveat}</p>}
        {plan.previous_attempts > 0 &&
          <Pill tone="warn">Attempted {plan.previous_attempts}×</Pill>}
        <label className="choice-inline">
          <input type="checkbox" disabled={!plan.automatic || !allowed}
            checked={selected.includes(plan.issue_category)}
            onChange={event => setSelected(current => event.target.checked
              ? [...current, plan.issue_category]
              : current.filter(item => item !== plan.issue_category))} />
          <span>{plan.automatic
            ? "Include this repair in the next run"
            : "Needs human review before another automatic attempt"}</span>
        </label>
      </section>
    </article>)}

    {message && <p className="notice" role="status">{message}</p>}

    {Boolean(data.plans.length) && <div className="panel-actions">
      <button className="button primary" disabled={!selected.length || running || !allowed}
        onClick={run}>
        {running ? "Starting…" : `Repair ${selected.length} issue${selected.length === 1 ? "" : "s"}`}
      </button>
    </div>}
    {!allowed && <p className="muted">Your account cannot start repairs.</p>}

    {Boolean(data.history.length) && <>
      <h3>Repair history</h3>
      <table className="data-table"><thead><tr>
        <th>Issue</th><th>Action</th><th>State</th><th>Attempt</th><th>Started</th>
      </tr></thead><tbody>
        {data.history.map(record => <tr key={record.id}>
          <td>{readable(record.issue_category)}</td>
          <td>{readable(record.action)}</td>
          <td><Pill tone={record.state === "succeeded" ? "safe"
            : record.state === "failed" ? "danger" : "warn"}>{readable(record.state)}</Pill></td>
          <td>{record.attempt}</td>
          <td>{when(record.created_at)}</td>
        </tr>)}
      </tbody></table>
    </>}
  </Panel>;
}

/* ------------------------------------------------------------------ */
/* Exports                                                             */
/* ------------------------------------------------------------------ */
type ExportChoice = ExportRecord["format"] | "report" | "report-simple";
const FORMATS: Array<{ value: ExportChoice; label: string; detail: string }> = [
  { value: "bundle", label: "Signed bundle", detail: "PDF, HTML, JSON, the plain-language report and a signed manifest" },
  { value: "report", label: "Plain-language report (PDF)", detail: "What was found and a step-by-step checklist" },
  { value: "report-simple", label: "Report in simple words (PDF)", detail: "The same, for readers with no background" },
  { value: "pdf", label: "Review package (PDF)", detail: "The formal record for filing or print" },
  { value: "html", label: "HTML", detail: "Self-contained portable page" },
  { value: "json", label: "JSON", detail: "The full review projection" },
];

function exportLabel(record: ExportRecord) {
  const manifest = record.manifest ?? {};
  if (manifest.variant === "report") return manifest.report_style === "simple" ? "Report (simple)" : "Report";
  return readable(record.format);
}

export function ExportsView({ audit, session }: { audit: Audit; session: Session | null }) {
  const { data, error, busy, reload } = useResource<{ items: ExportRecord[] }>(
    () => api.exports(audit.id), [audit.id]);
  const [format, setFormat] = useState<ExportChoice>("bundle");
  const [working, setWorking] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const allowed = session?.permissions.includes("export.create") ?? true;

  async function create() {
    setWorking(true); setProblem(null);
    try {
      if (format === "report" || format === "report-simple") {
        await api.createExport(audit.id, "pdf", { variant: "report", style: format === "report" ? "standard" : "simple" });
      } else {
        await api.createExport(audit.id, format);
      }
      reload();
    }
    catch (reason) {
      setProblem(reason instanceof Error ? reason.message : "The export could not be created");
    } finally { setWorking(false); }
  }

  return <Panel eyebrow="Handoff" title="Signed review packages"
    blurb="An export captures the review as it stands now, with a detached signature over its manifest so a recipient can verify the bytes without access to this system.">
    <div className="toolbar">
      <label className="filter"><span>Format</span>
        <select value={format} onChange={event =>
          setFormat(event.target.value as ExportChoice)}>
          {FORMATS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
      </label>
      <span className="result-count">
        {FORMATS.find(item => item.value === format)?.detail}
      </span>
      <button className="button primary" disabled={working || !allowed} onClick={create}>
        {working ? "Building…" : "Create export"}
      </button>
    </div>
    {!allowed && <p className="muted">Your account cannot create exports.</p>}
    {problem && <Problem message={problem} />}
    {busy && <Loading label="Loading exports…" />}
    {error && <Problem message={error} />}

    {data && !data.items.length && !busy && <div className="empty">
      <div className="empty-mark">◇</div><h2>No exports yet</h2>
      <p>Create one when the review is ready to leave the workspace.</p>
    </div>}

    {data && Boolean(data.items.length) && <table className="data-table"><thead><tr>
      <th>Format</th><th>Size</th><th>SHA-256</th><th>Signature</th><th>Created</th><th></th>
    </tr></thead><tbody>
      {data.items.map(record => <tr key={record.id}>
        <td><Pill>{exportLabel(record)}</Pill></td>
        <td>{bytes(record.size_bytes)}</td>
        <td><code title={record.content_sha256}>{record.content_sha256.slice(0, 16)}…</code></td>
        <td>
          {record.signature_algorithm}
          {record.signature_key_id.endsWith("-ephemeral") &&
            <> <Pill tone="warn">Ephemeral key</Pill></>}
        </td>
        <td>{when(record.created_at)}</td>
        <td><a className="button ghost" href={api.exportUrl(record.id)}
          target="_blank" rel="noreferrer">Download</a></td>
      </tr>)}
    </tbody></table>}
    <p className="muted">
      Verify a package by recomputing each file digest against manifest.json, then checking the
      manifest signature. A key id ending in “ephemeral” was generated at export time and cannot
      attribute the package to this deployment.
    </p>
  </Panel>;
}

/* ------------------------------------------------------------------ */
/* Possibly missing literature                                         */
/* ------------------------------------------------------------------ */
const VERIFICATION_TONE: Record<string, string> = {
  verified: "safe",
  ambiguous_match: "warn",
  no_matching_record: "danger",
  verification_unavailable: "neutral",
  unverified_model_suggestion: "warn",
};

export function MissingCitationsView({ audit }: { audit: Audit }) {
  const { data, error, busy } = useResource<MissingCitations>(
    () => api.missingCitations(audit.id), [audit.id]);
  if (busy) return <Loading label="Loading literature analysis…" />;
  if (error) return <Problem message={error} />;
  if (!data) return null;
  const cocitation = data.cocitation;
  const candidates = cocitation?.candidates ?? [];

  return <Panel eyebrow="Literature gaps" title="Possibly missing literature" blurb={data.guidance}>
    <h3>Frequently co-cited by this manuscript's own references</h3>
    {cocitation?.status === "offline_mode" && <p className="muted">Not available in offline mode.</p>}
    {cocitation?.status === "not_run" && <p className="muted">Run a new audit to compute co-citation candidates.</p>}
    {cocitation && cocitation.status === "completed" && !candidates.length && <p className="muted">
      No work is cited by {cocitation.min_support ?? 2} or more of the {cocitation.references_with_graph} references
      that have citation-graph data and is missing from this bibliography.
    </p>}
    {candidates.map(item => <article className="evidence-card" key={item.openalex_id}>
      <header><span>{item.support_count}</span><div>
        <strong>{item.title ?? item.openalex_id}</strong>
        <small>{item.authors.slice(0, 3).join(", ")}{item.authors.length > 3 ? " et al." : ""}
          {item.year ? ` · ${item.year}` : ""}{item.venue ? ` · ${item.venue}` : ""}</small>
      </div></header>
      <section>
        <p>Cited by {item.support_count} of this manuscript's references: {item.supporting_references.join(", ")}.
          {item.cited_by_count != null ? ` Cited ${item.cited_by_count} times overall.` : ""}</p>
        <div className="status-row">
          <Pill tone="safe">Indexed record</Pill>
          {item.doi && <a className="button ghost" href={`https://doi.org/${item.doi}`} target="_blank" rel="noreferrer">DOI ↗</a>}
          <a className="button ghost" href={item.url} target="_blank" rel="noreferrer">OpenAlex ↗</a>
        </div>
      </section>
    </article>)}

    <h3>Language-model search leads</h3>
    {!data.items.length && <p className="muted">No leads were generated. Choose “Full audit” when starting an audit to generate them.</p>}
    {data.items.map(item => <article className="evidence-card" key={item.id}>
      <header><span>◇</span><div>
        <strong>{item.title}</strong>
        <small>{item.authors} · {item.year}</small>
      </div></header>
      <section>
        <p>{item.rationale}</p>
        {item.matched_record && <p className="muted">Matched record: {item.matched_record.title}
          {item.matched_record.year ? ` (${item.matched_record.year})` : ""}
          {item.matched_record.doi ? ` · doi:${item.matched_record.doi}` : ""}</p>}
        <div className="status-row">
          <Pill tone={VERIFICATION_TONE[item.verification_status] ?? "warn"}>{readable(item.verification_status)}</Pill>
          {item.already_cited && <Pill tone="safe">Already in the bibliography</Pill>}
          <a className="button ghost" href={item.search_url} target="_blank" rel="noreferrer">
            Search a scholarly index ↗
          </a>
        </div>
      </section>
    </article>)}
    {(data.limitations ?? []).map(text => <p className="muted" key={text}>{text}</p>)}
    <p className="muted">{data.not_a_claim}</p>
  </Panel>;
}

/* ------------------------------------------------------------------ */
/* Self-citation and author overlap                                    */
/* ------------------------------------------------------------------ */
const TIER_LABEL: Record<string, string> = {
  orcid: "ORCID-confirmed",
  name_initial: "Surname and initial",
  surname: "Surname only (not counted)",
};

export function AuthorOverlapView({ audit }: { audit: Audit }) {
  const { data, error, busy } = useResource<AuthorOverlap>(
    () => api.authorOverlap(audit.id), [audit.id]);
  if (busy) return <Loading label="Building the self-citation view…" />;
  if (error) return <Problem message={error} />;
  if (!data) return null;

  const maximum = Math.max(1, ...data.nodes.map(node => node.reference_count));
  const analysis = data.self_citation;
  const counts = analysis?.counts ?? {};

  return <Panel eyebrow="Self-citation" title="Self-citation patterns" blurb={data.guidance}>
    {analysis && <>
      <div className="metrics">
        <div className="metric"><span>Self-citations</span>
          <strong>{counts.self_citations ?? 0}</strong>
          <small>{((counts.rate ?? 0) * 100).toFixed(0)}% of {counts.total_references ?? 0} references</small></div>
        <div className="metric"><span>ORCID-confirmed</span>
          <strong>{counts.orcid_confirmed ?? 0}</strong><small>Same person</small></div>
        <div className="metric"><span>Name and initial</span>
          <strong>{counts.name_initial_matches ?? 0}</strong><small>Probable match</small></div>
        <div className="metric emphasis"><span>Questionable</span>
          <strong>{analysis.questionable.length}</strong><small>Uncited or possibly irrelevant</small></div>
      </div>
      {analysis.patterns.length > 0 && <div className="notice" role="status">
        <strong>Patterns for review</strong>
        <ul className="plain-list">{analysis.patterns.map(pattern => <li key={pattern.code}>{pattern.detail}</li>)}</ul>
      </div>}
      {analysis.questionable.length > 0 && <>
        <h3>Self-citations to check</h3>
        <ul className="plain-list">{analysis.questionable.map(item => <li key={item.reference_id}>
          <strong>{item.marker}</strong> — {TIER_LABEL[item.tier] ?? readable(item.tier)};
          {" "}{item.reasons.map(reason => readable(reason).toLowerCase()).join(" and ")}
        </li>)}</ul>
      </>}
      {analysis.references.length > 0 && <>
        <h3>All author matches</h3>
        <ul className="plain-list">{analysis.references.map(item => <li key={item.reference}>
          <strong>{item.reference}</strong> <Pill tone={item.counted ? "warn" : "neutral"}>{TIER_LABEL[item.tier] ?? item.tier}</Pill>
        </li>)}</ul>
      </>}
      <p className="muted">{analysis.identity_note}
        {" "}ORCID iDs used: {[...(analysis.orcids.supplied ?? []), ...(analysis.orcids.front_matter ?? [])].join(", ") || "none — add them when starting an audit for confirmed matching."}</p>
    </>}

    <h3>Most repeated reference authors</h3>
    {!data.repeated_authors.length && <p className="muted">No author recurs across references.</p>}
    {data.repeated_authors.map(node => <div className="bar-row" key={node.id}>
      <span className="bar-label">{node.label}</span>
      <span className="bar-track">
        <span className="bar-fill" style={{ width: `${(node.reference_count / maximum) * 100}%` }} />
      </span>
      <span className="bar-value">{node.reference_count}</span>
    </div>)}
    <p className="muted">{data.not_a_claim}</p>
  </Panel>;
}

/* ------------------------------------------------------------------ */
/* Relevance                                                           */
/* ------------------------------------------------------------------ */
const RELEVANCE_TONE: Record<string, string> = {
  relevant: "safe",
  borderline: "warn",
  potentially_irrelevant: "danger",
  low_topical_similarity: "neutral",
  topic_only: "neutral",
  not_scored: "neutral",
};
const RELEVANCE_WORD: Record<string, string> = {
  topic_only: "Topic only — claims not checked",
};

export function RelevancePill({ label, score, basis }: {
  label?: string; score?: number | null; basis?: string;
}) {
  const value = label ?? "not_scored";
  const title = basis && basis !== "none"
    ? `Hybrid relevance (${readable(basis).toLowerCase()}). Uncalibrated default thresholds.`
    : "No relevance signal was available.";
  return <span className={`pill pill-${RELEVANCE_TONE[value] ?? "neutral"}`} title={title}>
    {RELEVANCE_WORD[value] ?? readable(value)}{score != null ? ` · ${Math.round(score)}` : ""}
  </span>;
}

/* ------------------------------------------------------------------ */
/* Audit options                                                       */
/* ------------------------------------------------------------------ */
const ORCID = /^(\d{4}-\d{4}-\d{4}-\d{3}[\dX])$/;

function orcidValid(value: string) {
  const digits = value.replaceAll("-", "");
  if (!ORCID.test(value)) return false;
  let total = 0;
  for (const character of digits.slice(0, -1)) total = (total + Number(character)) * 2;
  const result = (12 - (total % 11)) % 11;
  return digits.at(-1) === (result === 10 ? "X" : String(result));
}

export function AuditOptionsDialog({ onClose, onStart }: {
  onClose: () => void;
  onStart: (options: { run_missing_citations: boolean; author_orcids: string[] }) => Promise<void>;
}) {
  const [full, setFull] = useState(false);
  const [orcids, setOrcids] = useState("");
  const [busy, setBusy] = useState(false);
  const entries = orcids.split(/[\s,;]+/)
    .map(value => value.trim().replace(/^https?:\/\/orcid\.org\//i, "").toUpperCase())
    .filter(Boolean);
  const invalid = entries.filter(value => !orcidValid(value));

  async function start() {
    setBusy(true);
    try { await onStart({ run_missing_citations: full, author_orcids: entries }); }
    finally { setBusy(false); }
  }

  return <div className="scrim"><div className="dialog" role="dialog" aria-modal="true" aria-labelledby="audit-options-title">
    <div className="panel-head"><div><span className="eyebrow">New audit</span>
      <h2 id="audit-options-title">Audit options</h2></div>
      <button className="icon-button" aria-label="Close" onClick={onClose}>×</button></div>
    <label className="choice-row"><input type="checkbox" checked={full} onChange={event => setFull(event.target.checked)} />
      <span><strong>Full audit</strong><small>Also ask the language model for literature-search leads. Each lead is then
        checked against the scholarly indexes. Adds one model call.</small></span></label>
    <label className="rationale"><span>Author ORCID iDs <small>Optional</small></span>
      <textarea rows={3} value={orcids} onChange={event => setOrcids(event.target.value)}
        placeholder="0000-0002-1825-0097, 0000-0001-5109-3700" />
      <small>With ORCID iDs, self-citations are matched to the same person rather than to a similar name.</small></label>
    {invalid.length > 0 && <p className="field-error" role="alert">Not a valid ORCID iD: {invalid.join(", ")}</p>}
    <div className="panel-actions"><button className="button ghost" onClick={onClose}>Cancel</button>
      <button className="button primary" disabled={busy || invalid.length > 0} onClick={start}>
        {busy ? "Starting…" : "Start audit"}</button></div>
  </div></div>;
}

/* ------------------------------------------------------------------ */
/* Assignment                                                          */
/* ------------------------------------------------------------------ */
export function AssignmentPicker({ audit, finding, session, onChanged }: {
  audit: Audit; finding: Finding; session: Session | null; onChanged?: () => void;
}) {
  const { data } = useResource<{ items: Member[] }>(() => api.workload(), []);
  const { data: current, reload } = useResource<{ items: Assignment[] }>(
    () => api.assignments(audit.id), [audit.id]);
  const [saving, setSaving] = useState(false);
  const allowed = session?.permissions.includes("assignment.write") ?? false;
  const existing = current?.items.find(
    item => item.finding_id === finding.id && item.state === "assigned");

  async function assign(subject: string) {
    setSaving(true);
    try {
      if (subject) await api.assign(audit.id, finding.id, subject);
      else await api.unassign(audit.id, finding.id);
      reload(); onChanged?.();
    } finally { setSaving(false); }
  }

  if (!allowed) {
    return existing
      ? <p className="muted">Assigned to {existing.assignee}.</p>
      : null;
  }
  return <label className="filter"><span>Assigned reviewer</span>
    <select value={existing?.assignee ?? ""} disabled={saving}
      onChange={event => assign(event.target.value)}>
      <option value="">Unassigned</option>
      {(data?.items ?? []).filter(member => member.active).map(member =>
        <option key={member.subject} value={member.subject}>
          {member.display_name ?? member.subject} ({member.open_cases ?? 0} open)
        </option>)}
    </select>
  </label>;
}

export function TeamView({ session }: { session: Session | null }) {
  const { data, error, busy } = useResource<{ items: Member[] }>(() => api.workload(), []);
  if (busy) return <Loading label="Loading the team…" />;
  if (error) return <Problem message={error} />;
  return <Panel eyebrow="Organization" title="Reviewers and workload"
    blurb="Assignment routes a case to one person. Open counts are live, so the least-loaded reviewer is visible before you assign.">
    <table className="data-table"><thead><tr>
      <th>Reviewer</th><th>Roles</th><th>Open cases</th><th>Status</th>
    </tr></thead><tbody>
      {(data?.items ?? []).map(member => <tr key={member.subject}>
        <td><strong>{member.display_name ?? member.subject}</strong>
          <br /><small>{member.email ?? member.subject}</small></td>
        <td>{member.roles.map(role => <Pill key={role}>{readable(role)}</Pill>)}</td>
        <td>{member.open_cases ?? 0}</td>
        <td><Pill tone={member.active ? "safe" : "warn"}>
          {member.active ? "Active" : "Inactive"}</Pill></td>
      </tr>)}
    </tbody></table>
    {!(data?.items ?? []).length && <p className="muted">
      No members are recorded yet. In {session?.auth_mode ?? "local"} mode, members appear as
      they are added by an administrator.
    </p>}
  </Panel>;
}

/* ------------------------------------------------------------------ */
/* Operations: SLOs, cost and queue                                    */
/* ------------------------------------------------------------------ */
export function OperationsView() {
  const { data, error, busy } = useResource<SloReport>(() => api.slos(), []);
  if (busy) return <Loading label="Measuring service objectives…" />;
  if (error) return <Problem message={error} />;
  if (!data) return null;
  const queue = (data.totals.queue ?? {}) as Record<string, number>;

  return <Panel eyebrow="Operations" title="Service objectives, cost and queue"
    blurb={`Measured over the last ${data.window_hours} hours. ${data.note}`}>
    <div className="metrics">
      <div className="metric"><span>Completed audits</span>
        <strong>{data.totals.completed_audits}</strong>
        <small>{data.totals.failed_audits} failed</small></div>
      <div className="metric"><span>Provider cost</span>
        <strong>${data.totals.total_cost_usd.toFixed(2)}</strong>
        <small>${data.totals.cost_per_audit_usd.toFixed(3)} per audit</small></div>
      <div className="metric"><span>Queue</span>
        <strong>{queue.queued ?? 0}</strong>
        <small>{queue.running ?? 0} running · {queue.dead_letter ?? 0} dead-lettered</small></div>
      <div className="metric" ><span>Objectives met</span>
        <strong>{data.objectives.filter(item => item.met).length}/{data.objectives.length}</strong>
        <small>{data.met ? "All targets held" : "Investigate the misses below"}</small></div>
    </div>

    <table className="data-table"><thead><tr>
      <th>Objective</th><th>Target</th><th>Measured</th><th>Samples</th><th>Status</th>
    </tr></thead><tbody>
      {data.objectives.map(objective => <tr key={objective.key}>
        <td><strong>{objective.title}</strong><br /><small>{objective.detail}</small></td>
        <td>{objective.comparison === "at_most" ? "≤" : "≥"} {objective.target} {objective.unit}</td>
        <td>{objective.measured} {objective.unit}</td>
        <td>{objective.sample_size}</td>
        <td><Pill tone={objective.met ? "safe" : "danger"}>
          {objective.met ? "Met" : "Missed"}</Pill></td>
      </tr>)}
    </tbody></table>

    <h3>Stage latency (95th percentile, seconds)</h3>
    {!Object.keys(data.totals.stage_latency_p95_seconds).length &&
      <p className="muted">No stage timings were recorded in this window.</p>}
    {Object.entries(data.totals.stage_latency_p95_seconds).map(([stage, value]) => {
      const scale = Math.max(1, ...Object.values(data.totals.stage_latency_p95_seconds));
      return <div className="bar-row" key={stage}>
        <span className="bar-label">{readable(stage)}</span>
        <span className="bar-track">
          <span className="bar-fill" style={{ width: `${(value / scale) * 100}%` }} />
        </span>
        <span className="bar-value">{value.toFixed(1)}s</span>
      </div>;
    })}
  </Panel>;
}

/* ------------------------------------------------------------------ */
/* Runtime banner                                                      */
/* ------------------------------------------------------------------ */
export function RuntimeBadge({ session }: { session: Session | null }) {
  if (!session) return null;
  const durable = session.workspace_backend === "postgres";
  return <div className="runtime-badge" title={
    `Workspace: ${session.workspace_backend} · Objects: ${session.object_backend} · ` +
    `Auth: ${session.auth_mode}`
  }>
    <Pill tone={durable ? "safe" : "warn"}>
      {durable ? "Durable runtime" : "Local runtime"}
    </Pill>
    <small>{session.display_name ?? session.actor_id}</small>
  </div>;
}

export function useSession() {
  const [session, setSession] = useState<Session | null>(null);
  useEffect(() => { api.session().then(setSession).catch(() => setSession(null)); }, []);
  return session;
}
