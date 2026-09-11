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
const FORMATS: Array<{ value: ExportRecord["format"]; label: string; detail: string }> = [
  { value: "bundle", label: "Signed bundle", detail: "PDF, HTML, JSON and a signed manifest" },
  { value: "pdf", label: "PDF", detail: "Paginated copy for filing or print" },
  { value: "html", label: "HTML", detail: "Self-contained portable page" },
  { value: "json", label: "JSON", detail: "The full review projection" },
];

export function ExportsView({ audit, session }: { audit: Audit; session: Session | null }) {
  const { data, error, busy, reload } = useResource<{ items: ExportRecord[] }>(
    () => api.exports(audit.id), [audit.id]);
  const [format, setFormat] = useState<ExportRecord["format"]>("bundle");
  const [working, setWorking] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const allowed = session?.permissions.includes("export.create") ?? true;

  async function create() {
    setWorking(true); setProblem(null);
    try { await api.createExport(audit.id, format); reload(); }
    catch (reason) {
      setProblem(reason instanceof Error ? reason.message : "The export could not be created");
    } finally { setWorking(false); }
  }

  return <Panel eyebrow="Handoff" title="Signed review packages"
    blurb="An export captures the review as it stands now, with a detached signature over its manifest so a recipient can verify the bytes without access to this system.">
    <div className="toolbar">
      <label className="filter"><span>Format</span>
        <select value={format} onChange={event =>
          setFormat(event.target.value as ExportRecord["format"])}>
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
        <td><Pill>{readable(record.format)}</Pill></td>
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
/* Literature-search candidates                                        */
/* ------------------------------------------------------------------ */
export function MissingCitationsView({ audit }: { audit: Audit }) {
  const { data, error, busy } = useResource<MissingCitations>(
    () => api.missingCitations(audit.id), [audit.id]);
  if (busy) return <Loading label="Loading search leads…" />;
  if (error) return <Problem message={error} />;
  if (!data) return null;

  return <Panel eyebrow="Literature search" title="Unverified candidates"
    blurb={data.guidance}>
    {!data.items.length && <div className="empty">
      <div className="empty-mark">◇</div><h2>No candidates were generated</h2>
      <p>Run an audit with the full-audit option to generate literature-search leads.</p>
    </div>}
    {data.items.map(item => <article className="evidence-card" key={item.id}>
      <header><span>◇</span><div>
        <strong>{item.title}</strong>
        <small>{item.authors} · {item.year}</small>
      </div></header>
      <section>
        <p>{item.rationale}</p>
        <div className="status-row">
          <Pill tone="warn">{readable(item.verification_status)}</Pill>
          {item.already_cited && <Pill tone="safe">Already in the bibliography</Pill>}
          <a className="button ghost" href={item.search_url} target="_blank" rel="noreferrer">
            Search a scholarly index ↗
          </a>
        </div>
      </section>
    </article>)}
    <p className="muted">{data.not_a_claim}</p>
  </Panel>;
}

/* ------------------------------------------------------------------ */
/* Author overlap                                                      */
/* ------------------------------------------------------------------ */
export function AuthorOverlapView({ audit }: { audit: Audit }) {
  const { data, error, busy } = useResource<AuthorOverlap>(
    () => api.authorOverlap(audit.id), [audit.id]);
  if (busy) return <Loading label="Building the overlap view…" />;
  if (error) return <Problem message={error} />;
  if (!data) return null;

  const maximum = Math.max(1, ...data.nodes.map(node => node.reference_count));

  return <Panel eyebrow="Descriptive analysis" title="Author overlap"
    blurb={data.guidance}>
    <div className="metrics">
      <div className="metric"><span>References</span>
        <strong>{data.counts.total_references}</strong><small>In this bibliography</small></div>
      <div className="metric"><span>Share a manuscript author</span>
        <strong>{data.counts.references_sharing_a_manuscript_author}</strong>
        <small>{(data.counts.share_of_references * 100).toFixed(0)}% of references</small></div>
      <div className="metric"><span>Distinct reference authors</span>
        <strong>{data.counts.distinct_reference_authors}</strong><small>After normalization</small></div>
    </div>

    <h3>Manuscript authors appearing in the bibliography</h3>
    {!data.edges.length && <p className="muted">No manuscript author appears in a reference.</p>}
    {Boolean(data.edges.length) && <ul className="plain-list">
      {data.edges.map((edge, index) => <li key={`${edge.author_id}-${index}`}>
        <strong>{edge.author}</strong> appears in reference {edge.reference}
      </li>)}
    </ul>}

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
