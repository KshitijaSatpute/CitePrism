import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type {
  AssistantAction, AssistantAnswer, AssistantDraft, AssistantReference, AssistantSection, AssistantTone, AssistantTurn,
  Audit,
  DataHandling, DocumentStructure, Job, Manuscript,
} from "./types";

/* ------------------------------------------------------------------ */
/* Margin mark: a reference number in the gutter with an ink stroke.   */
/* The one visual signature used everywhere a reference is listed.     */
/* ------------------------------------------------------------------ */
export function MarginMark({ marker, tone = "neutral" }: { marker: string; tone?: AssistantTone | string }) {
  const label = { warning: "needs attention", caution: "worth a look", ok: "no issues", neutral: "" }[tone] ?? "";
  // A numbered reference keeps its brackets ("[6]"), so a list of items reads as references,
  // not as a count ("6, 6, 7" for two items about [6] looked like a broken list).
  const bare = marker.replace(/^\[|\]$/g, "");
  return <span className={`mark mark-${tone}`} aria-label={label ? `Reference ${marker}, ${label}` : `Reference ${marker}`}>
    {/^\d+$/.test(bare) ? `[${bare}]` : bare}
  </span>;
}

/* ------------------------------------------------------------------ */
/* Plain explanations of why an audit stopped                          */
/* ------------------------------------------------------------------ */
export function explainFailure(message: string | null | undefined): { title: string; detail: string } {
  const text = (message ?? "").toLowerCase();
  if (text.includes("every configured model failed")) {
    return { title: "All the AI models we tried were busy",
      detail: "Each configured model refused the request. Wait a few minutes and try again, or add another provider (a free Groq key or a local Ollama model) under LLM_FALLBACK_MODELS in the .env file." };
  }
  if (text.includes("http 404") || text.includes("may have been retired") || text.includes("no longer available")) {
    return { title: "The AI model in your settings is no longer offered",
      detail: "Google and other providers retire models regularly. Set a current model in the .env file (for example GOOGLE_MODEL=gemini-3.6-flash), restart CitePrism and try again." };
  }
  if (text.includes("503") || text.includes("temporarily unavailable") || text.includes("overloaded")) {
    return { title: "The AI service is busy right now",
      detail: "The language-model provider turned the request away because it is overloaded. Nothing is wrong with your paper. Try again in a few minutes; finished steps are reused, so it continues where it stopped." };
  }
  if (text.includes("rate limit") || text.includes("quota")) {
    return { title: "The AI service's usage limit was reached",
      detail: "Free plans allow a limited number of requests per minute and per day. Wait, then try again — or switch to another model in the .env file." };
  }
  if (text.includes("api key") || text.includes("authentication") || text.includes("access permissions")) {
    return { title: "The AI service did not accept the API key",
      detail: "Check the key in the .env file, then restart CitePrism." };
  }
  if (text.includes("timed out") || text.includes("timeout")) {
    return { title: "A step took too long",
      detail: "Long papers can exceed the time limit. Try again; finished steps are reused." };
  }
  if (text.includes("pdf") || text.includes("text layer") || text.includes("ocr")) {
    return { title: "The PDF could not be read",
      detail: "Use a PDF with selectable text. Scanned pages need OCR first." };
  }
  return { title: "The check stopped", detail: "Try again. If it stops at the same point, open Review in detail → Repairs." };
}

const STEPS = [
  { until: 45, label: "Reading the paper and its reference list" },
  { until: 80, label: "Finding each cited work in scholarly databases" },
  { until: 96, label: "Checking every citation against its source" },
  { until: 101, label: "Writing up what needs your attention" },
];

function Progress({ job }: { job: Job }) {
  const current = STEPS.findIndex(step => job.progress < step.until);
  return <div className="note note-progress" role="status" aria-live="polite">
    <p className="note-lead">Checking the citations. This usually takes a few minutes; you can leave this page open.</p>
    <ol className="steps">
      {STEPS.map((step, index) => <li key={step.label}
        className={index < current ? "done" : index === current ? "active" : ""}>
        <span className="step-dot" aria-hidden />{step.label}
        {index === current && job.stage && <small>{job.stage.replace(/^[✓❌]\s*/, "")}</small>}
      </li>)}
    </ol>
    <progress value={job.progress} max={100} aria-label="Overall progress">{job.progress}%</progress>
  </div>;
}

/* ------------------------------------------------------------------ */
/* Start: upload a paper                                               */
/* ------------------------------------------------------------------ */
function list(names: string[]) {
  return names.length <= 1 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/* Where the paper's text goes. Free model plans may keep what they receive, so the
   warning names the services; a paper under peer review is confidential. */
function DataNotice({ handling }: { handling: DataHandling | null }) {
  if (!handling) return null;
  const remote = handling.model_recipients.filter(name => name !== "a local model");
  if (handling.terms === "local_only") {
    return <p className="data-notice data-ok"><strong>Stays here.</strong> The paper's text is read by a model running
      on your organisation's own server{handling.reference_lookups.length
        ? <>; only the titles and authors in its reference list are looked up in {list(handling.reference_lookups)}</> : null}.</p>;
  }
  if (handling.terms === "no_training") {
    return <p className="data-notice data-ok"><strong>Where the text goes.</strong> To read and check the paper, its text
      is sent to {list(handling.model_recipients)} under your organisation's agreement, which excludes using it for
      training.</p>;
  }
  return <div className="data-notice data-warn" role="note">
    <p><strong>Before you upload a confidential paper</strong></p>
    <p>To read and check the paper, its text is sent to {list(remote.length ? remote : handling.model_recipients)}.
      On free plans these services may keep what they receive and use it to improve their products. Don't upload a
      manuscript under peer review, or anything else confidential, unless your administrator has set up a paid plan
      that excludes training or a local model.</p>
  </div>;
}

function Start({ onUpload, dataHandling }: { onUpload: (file: File) => Promise<void>; dataHandling: DataHandling | null }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [dragging, setDragging] = useState(false);
  async function take(file: File | undefined) {
    if (!file) return;
    if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") { setProblem("Choose a PDF file."); return; }
    setBusy(true); setProblem("");
    try { await onUpload(file); }
    catch (reason) { setProblem(reason instanceof Error ? reason.message : "The upload failed."); setBusy(false); }
  }
  return <div className="start">
    <h1>Check the citations in a paper</h1>
    <p className="start-lead">CitePrism reads the reference list, finds every cited work, and tells you which
      citations are worth a second look — and why. You decide what to do about each one.</p>
    <label className={`dropzone ${dragging ? "dragging" : ""}`}
      onDragOver={event => { event.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={event => { event.preventDefault(); setDragging(false); take(event.dataTransfer.files[0]); }}>
      <input type="file" accept="application/pdf,.pdf" disabled={busy}
        onChange={event => take(event.target.files?.[0])} />
      <strong>{busy ? "Uploading…" : "Choose a PDF"}</strong>
      <span>or drop it here · text-based PDF up to 50 MB</span>
    </label>
    {problem && <p className="field-error" role="alert">{problem}</p>}
    <DataNotice handling={dataHandling} />
    <ol className="how">
      <li><span><strong>Upload the paper.</strong> The file is stored on the CitePrism server you are using.</span></li>
      <li><span><strong>Start the check.</strong> A typical paper takes a few minutes.</span></li>
      <li><span><strong>Read the summary and ask questions.</strong> Every answer points to the references it is about.</span></li>
    </ol>
  </div>;
}

/* ------------------------------------------------------------------ */
/* Ready: choose options and start                                     */
/* ------------------------------------------------------------------ */
const ORCID = /^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$/;
function orcidValid(value: string) {
  if (!ORCID.test(value)) return false;
  const digits = value.replaceAll("-", "");
  let total = 0;
  for (const character of digits.slice(0, -1)) total = (total + Number(character)) * 2;
  const result = (12 - (total % 11)) % 11;
  return digits.at(-1) === (result === 10 ? "X" : String(result));
}

function Chapters({ manuscript, structure, onSplit, compact = false }: {
  manuscript: Manuscript; structure: DocumentStructure; onSplit: (selectFirst: string) => Promise<void>;
  compact?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  async function split() {
    setBusy(true); setProblem("");
    try {
      const result = await api.splitIntoParts(manuscript.id);
      const first = result.items[0]?.manuscript.id;
      if (first) await onSplit(first);
    } catch (reason) {
      setProblem(reason instanceof Error ? reason.message : "The PDF could not be split.");
      setBusy(false);
    }
  }
  return <div className="note note-book">
    <p className="note-lead">This PDF looks like a book or proceedings: it has {structure.parts.length} chapters,
      each with its own reference list.</p>
    <p>Checking chapter by chapter gives each chapter its own authors, citations and report — which is what
      makes the self-citation check meaningful. The original PDF is kept.</p>
    {(() => {
      const list = <ol className="chapters">
        {structure.parts.map(part => <li key={part.number}>
          <span className="chapter-pages">pp. {part.page_start}–{part.page_end}</span>
          <span className="chapter-copy"><cite>{part.title || `Part ${part.number}`}</cite>
            <small>{part.authors.length ? part.authors.join(", ") + " · " : ""}{part.references} references</small></span>
        </li>)}
      </ol>;
      return compact ? <details className="options"><summary>Show the chapters</summary>{list}</details> : list;
    })()}
    {problem && <p className="field-error" role="alert">{problem}</p>}
    <div className="note-actions">
      <button className="button primary" disabled={busy} onClick={split}>
        {busy ? "Splitting…" : `Split into ${structure.parts.length} chapters`}</button>
    </div>
  </div>;
}

function Ready({ manuscript, onStart, again, wholeBook = false }: {
  manuscript: Manuscript; again: boolean; wholeBook?: boolean;
  onStart: (options: { run_missing_citations: boolean; author_orcids: string[] }) => Promise<void>;
}) {
  const [full, setFull] = useState(false);
  const [orcids, setOrcids] = useState("");
  const [busy, setBusy] = useState(false);
  const entries = orcids.split(/[\s,;]+/).map(value => value.trim()
    .replace(/^https?:\/\/orcid\.org\//i, "").toUpperCase()).filter(Boolean);
  const invalid = entries.filter(value => !orcidValid(value));
  async function start() {
    setBusy(true);
    try { await onStart({ run_missing_citations: full, author_orcids: entries }); } finally { setBusy(false); }
  }
  return <div className={again ? "ready-inline" : "note"}>
    {!again && <p className="note-lead">{wholeBook ? "Or check the whole PDF at once" : <>Ready to check <cite>{manuscript.title}</cite>.</>}</p>}
    <details className="options">
      <summary>Options</summary>
      <label className="check"><input type="checkbox" checked={full} onChange={event => setFull(event.target.checked)} />
        <span>Also ask the AI to suggest missing literature<small>Works that this paper's own references cite often are always included. AI suggestions are each looked up to confirm they exist, and add one more AI request.</small></span></label>
      <label className="field"><span>Authors' ORCID iDs <small>optional</small></span>
        <textarea rows={2} value={orcids} onChange={event => setOrcids(event.target.value)}
          placeholder="0000-0002-1825-0097" />
        <small>Makes self-citation matching exact instead of name-based.</small></label>
      {invalid.length > 0 && <p className="field-error" role="alert">Not a valid ORCID iD: {invalid.join(", ")}</p>}
    </details>
    <div className="note-actions">
      <button className="button primary" disabled={busy || invalid.length > 0} onClick={start}>
        {busy ? "Starting…" : again ? "Try again" : wholeBook ? "Check the whole PDF" : "Start the check"}</button>
    </div>
  </div>;
}

/* ------------------------------------------------------------------ */
/* Conversation                                                        */
/* ------------------------------------------------------------------ */
type Message = { id: number; role: "user"; text: string }
  | { id: number; role: "assistant"; answer: AssistantAnswer }
  | { id: number; role: "pending" }
  | { id: number; role: "problem"; text: string };

/* Conversations survive switching between Ask and Review in detail, and reloading
   the page: each audit's conversation is kept in this browser (never on the server). */
const conversations = new Map<string, Message[]>();
const STORED_MESSAGES = 60;

function storedConversation(auditId: string): Message[] | undefined {
  try {
    const raw = window.localStorage.getItem(`citeprism.chat.${auditId}`);
    const value = raw ? JSON.parse(raw) : null;
    if (!Array.isArray(value)) return undefined;
    // Stored answers come from an earlier version of the app; keep only ones this version can show.
    const strings = (items: unknown) => Array.isArray(items) && items.every(item => typeof item === "string");
    const shown = (answer: AssistantAnswer | undefined) => Boolean(answer && typeof answer.text === "string"
      && typeof answer.intent === "string" && strings(answer.points) && strings(answer.suggestions)
      && Array.isArray(answer.references) && (answer.sections === undefined || Array.isArray(answer.sections)));
    const valid = value.filter((message: Message) => message && typeof message.id === "number"
      && (message.role === "user" ? typeof message.text === "string"
        : message.role === "assistant" ? shown(message.answer) : false));
    return valid.length ? valid : undefined;
  } catch { return undefined; }
}

function storeConversation(auditId: string, messages: Message[]) {
  try {
    const kept = messages.filter(message => message.role === "user" || message.role === "assistant").slice(-STORED_MESSAGES);
    if (kept.length) window.localStorage.setItem(`citeprism.chat.${auditId}`, JSON.stringify(kept));
    else window.localStorage.removeItem(`citeprism.chat.${auditId}`);
  } catch { /* storage full or blocked: the conversation still works for this visit */ }
}

/* "Label (refs): 1. Do this. 2. Then that." -> a lead line and numbered steps. */
function Point({ text }: { text: string }) {
  const parts = text.split(/\s(?=\d{1,2}\.\s)/);
  if (parts.length < 3) return <li>{text}</li>;
  const [lead, ...steps] = parts;
  return <li>{lead}<ol className="steps-inline">
    {steps.map(step => <li key={step}>{step.replace(/^\d{1,2}\.\s/, "")}</li>)}
  </ol></li>;
}

function References({ items, more, onOpenReference }: {
  items: AssistantReference[]; more: number; onOpenReference: (id: string) => void;
}) {
  if (!items.length) return null;
  return <>
    <ul className="ref-list">
      {items.map(item => <li key={item.id}>
        <button className="ref-row" onClick={() => onOpenReference(item.id)}>
          <MarginMark marker={item.marker} tone={item.tone} />
          <span className="ref-copy"><cite>{item.title}</cite>{item.note && <span>{item.note}</span>}</span>
          <span className="ref-open">Open</span>
        </button>
      </li>)}
    </ul>
    {more > 0 && <p className="answer-more">And {more} more — ask “show me all of them”, or see Review in detail.</p>}
  </>;
}

/* A message to copy and send. E-mail links are limited in length, so long drafts are copy-only. */
function Draft({ draft }: { draft: AssistantDraft }) {
  const [copied, setCopied] = useState<"idle" | "done" | "failed">("idle");
  const text = `${draft.subject}\n\n${draft.body}`;
  async function copy() {
    try { await navigator.clipboard.writeText(text); setCopied("done"); }
    catch { setCopied("failed"); }
  }
  const mail = `mailto:?subject=${encodeURIComponent(draft.subject)}&body=${encodeURIComponent(draft.body)}`;
  return <figure className="draft">
    <figcaption><span className="draft-label">Subject</span> {draft.subject}</figcaption>
    <pre className="draft-body" tabIndex={0}>{draft.body}</pre>
    <div className="draft-actions">
      <button className="button" onClick={copy}>{copied === "done" ? "Copied" : "Copy the message"}</button>
      {mail.length < 1900 && <a className="button" href={mail}>Open in your e-mail app</a>}
      {copied === "failed" && <span className="field-error" role="alert">Copying was blocked by the browser — select the text and copy it.</span>}
    </div>
  </figure>;
}

function Section({ section, onOpenReference }: { section: AssistantSection; onOpenReference: (id: string) => void }) {
  return <section className="answer-section">
    <h3>{section.heading}</h3>
    <p className="answer-text">{section.text}</p>
    {section.points.length > 0 && <ul className="answer-points">
      {section.points.map(point => <Point key={point} text={point} />)}</ul>}
    {section.draft && <Draft draft={section.draft} />}
    <References items={section.references} more={section.more_references} onOpenReference={onOpenReference} />
  </section>;
}

function ReportButton({ audit, action }: { audit: Audit; action: AssistantAction }) {
  const [state, setState] = useState<"idle" | "working" | "done" | "failed">("idle");
  const [problem, setProblem] = useState("");
  async function make() {
    setState("working"); setProblem("");
    try {
      const record = await api.createExport(audit.id, "pdf", { variant: "report", style: action.style });
      const link = document.createElement("a");
      link.href = api.exportUrl(record.id);
      link.rel = "noopener";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setState("done");
    } catch (reason) {
      setProblem(reason instanceof Error ? reason.message : "The report could not be made.");
      setState("failed");
    }
  }
  return <div className="answer-action">
    <button className="button primary" onClick={make} disabled={state === "working"}>
      {state === "working" ? "Preparing your report…" : state === "done" ? "Download again" : action.label}</button>
    {state === "done" && <span className="answer-action-note">Saved to your downloads. It is also listed under
      Review in detail → Export, with its signature.</span>}
    {state === "failed" && <span className="field-error" role="alert">{problem}</span>}
  </div>;
}

function Answer({ answer, audit, onOpenReference, onAccept, latest }: {
  answer: AssistantAnswer; audit: Audit; onOpenReference: (id: string) => void;
  onAccept: (text: string) => void; latest: boolean;
}) {
  const sections = answer.sections ?? [];
  return <div className="answer">
    <p className="answer-text">{answer.text}</p>
    {answer.points.length > 0 && <ul className="answer-points">
      {answer.points.map(point => <Point key={point} text={point} />)}
    </ul>}
    {answer.draft && <Draft draft={answer.draft} />}
    <References items={answer.references} more={answer.more_references} onOpenReference={onOpenReference} />
    {sections.map((section, index) => <Section key={`${index}-${section.intent}`} section={section}
      onOpenReference={onOpenReference} />)}
    {(answer.actions ?? []).map(action => <ReportButton key={action.kind + action.style} audit={audit} action={action} />)}
    {answer.offer_text && latest && <p className="answer-offer">{answer.offer_text}
      <button className="link" onClick={() => onAccept("Yes, please")}>Yes, please</button></p>}
    {answer.grounding === "model_phrased" && <p className="answer-source">
      Phrased by {answer.model ?? "an AI model"} from this audit's results.</p>}
  </div>;
}

function turns(messages: Message[]): AssistantTurn[] {
  const history: AssistantTurn[] = [];
  for (const message of messages) {
    if (message.role === "user") history.push({ role: "user", text: message.text.slice(0, 4000) });
    if (message.role === "assistant") {
      const answer = message.answer;
      const ids = answer.reference_ids ?? [
        ...answer.references.map(item => item.id),
        ...(answer.sections ?? []).flatMap(section => section.references.map(item => item.id)),
      ];
      history.push({ role: "assistant", text: answer.text.slice(0, 4000), intent: answer.intent.slice(0, 40),
        reference_ids: ids.slice(0, 40), offer: answer.offer ?? null, style: answer.register ?? null,
        topics: (answer.topics ?? []).slice(0, 8) });
    }
  }
  return history.slice(-12);
}

export function Conversation({ audit, onOpenReference }: {
  audit: Audit; onOpenReference: (id: string) => void;
}) {
  const [messages, setMessages] = useState<Message[]>(() => conversations.get(audit.id)
    ?? storedConversation(audit.id) ?? [{ id: 0, role: "pending" }]);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const next = useRef(Math.max(0, ...messages.map(message => message.id)) + 1);
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const settled = messages.filter(message => message.role !== "pending");
    conversations.set(audit.id, settled);
    storeConversation(audit.id, settled);
  }, [audit.id, messages]);

  useEffect(() => {
    if (conversations.get(audit.id)?.length || messages.some(message => message.role === "assistant")) return;
    let live = true;
    api.assistantSummary(audit.id)
      .then(answer => live && setMessages([{ id: 0, role: "assistant", answer }]))
      .catch(reason => live && setMessages([{ id: 0, role: "problem",
        text: reason instanceof Error ? reason.message : "The summary could not load." }]));
    return () => { live = false; };
  }, [audit.id]);

  useEffect(() => {
    if (messages.length > 1) end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  async function ask(text: string) {
    const value = text.trim();
    if (!value || asking) return;
    setAsking(true); setQuestion("");
    if (input.current) input.current.style.height = "auto";
    let history: AssistantTurn[] = [];
    try { history = turns(messages); } catch { history = []; }   // never leave the composer stuck
    const userId = next.current++, pendingId = next.current++;
    setMessages(current => [...current, { id: userId, role: "user", text: value }, { id: pendingId, role: "pending" }]);
    try {
      const answer = await api.ask(audit.id, value, history);
      setMessages(current => current.map(message => message.id === pendingId
        ? { id: pendingId, role: "assistant", answer } : message));
    } catch (reason) {
      setMessages(current => current.map(message => message.id === pendingId
        ? { id: pendingId, role: "problem", text: reason instanceof Error ? reason.message : "No answer came back." }
        : message));
    } finally { setAsking(false); input.current?.focus(); }
  }

  function startOver() {
    conversations.delete(audit.id);
    storeConversation(audit.id, []);
    setMessages([{ id: next.current++, role: "pending" }]);
    api.assistantSummary(audit.id)
      .then(answer => setMessages([{ id: next.current++, role: "assistant", answer }]))
      .catch(() => setMessages([{ id: next.current++, role: "problem", text: "The summary could not load." }]));
  }

  const lastAssistant = [...messages].reverse().find(message => message.role === "assistant");
  const suggestions = lastAssistant && lastAssistant.role === "assistant" ? lastAssistant.answer.suggestions : [];

  return <div className="conversation">
    <div className="thread" aria-live="polite">
      {messages.map(message => {
        if (message.role === "user") return <p className="question" key={message.id}>{message.text}</p>;
        if (message.role === "pending") return <p className="thinking" key={message.id} role="status">Reading the results…</p>;
        if (message.role === "problem") return <p className="field-error" key={message.id} role="alert">{message.text}</p>;
        return <Answer key={message.id} answer={message.answer} audit={audit} onOpenReference={onOpenReference}
          onAccept={ask} latest={message === lastAssistant && !asking} />;
      })}
      <div ref={end} />
    </div>
    <div className="composer"><div className="composer-inner">
      {suggestions.length > 0 && <div className="suggestions" aria-label="Suggested questions">
        {suggestions.map(item => <button key={item} className="suggestion" disabled={asking} onClick={() => ask(item)}>{item}</button>)}
      </div>}
      <form className="ask" onSubmit={event => { event.preventDefault(); ask(question); }}>
        <label className="visually-hidden" htmlFor="ask-input">Ask about this audit</label>
        <textarea id="ask-input" ref={input} rows={1} maxLength={1000} value={question}
          placeholder="Ask anything about the results — e.g. “What should I fix first?” or “Explain it simply”"
          onChange={event => {
            setQuestion(event.target.value);
            const box = event.target;
            box.style.height = "auto";
            box.style.height = `${Math.min(box.scrollHeight, 180)}px`;
          }}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ask(question); } }} />
        <button className="button primary" disabled={asking || !question.trim()}>Ask</button>
      </form>
      <p className="composer-note">Answers come from this audit's results and point to what is worth checking; they
        are not verdicts. {messages.length > 2 && <button className="link" onClick={startOver}>Start over</button>}</p>
    </div></div>
  </div>;
}

/* ------------------------------------------------------------------ */
/* Ask CitePrism: the guided mode                                      */
/* ------------------------------------------------------------------ */
export function AskView({ manuscript, audit, job, hasReview, dataHandling, onUpload, onStart, onOpenReference, onSplit }: {
  manuscript: Manuscript | null; audit: Audit | null; job: Job | null; hasReview: boolean;
  dataHandling: DataHandling | null;
  onUpload: (file: File) => Promise<void>;
  onStart: (options: { run_missing_citations: boolean; author_orcids: string[] }) => Promise<void>;
  onOpenReference: (id: string) => void;
  onSplit: (selectFirst: string) => Promise<void>;
}) {
  const running = job && ["queued", "running", "cancel_requested"].includes(job.state);
  const [structure, setStructure] = useState<DocumentStructure | null>(null);
  useEffect(() => {
    setStructure(null);
    if (!manuscript) return;
    let live = true;
    api.structure(manuscript.id).then(value => live && setStructure(value)).catch(() => undefined);
    return () => { live = false; };
  }, [manuscript?.id]);
  return <section className="ask-view" aria-label="Ask CitePrism">
    {!manuscript && <Start onUpload={onUpload} dataHandling={dataHandling} />}
    {manuscript && <header className="paper-head">
      <cite className="paper-title">{manuscript.title}</cite>
      <span className="paper-file">{manuscript.original_filename}</span>
    </header>}
    {manuscript && running && <Progress job={job!} />}
    {manuscript && !running && audit && structure?.multi_part &&
      <Chapters manuscript={manuscript} structure={structure} onSplit={onSplit} compact />}
    {manuscript && !running && !audit && structure?.multi_part &&
      <Chapters manuscript={manuscript} structure={structure} onSplit={onSplit} />}
    {manuscript && !running && !audit && <Ready manuscript={manuscript} again={false} onStart={onStart}
      wholeBook={Boolean(structure?.multi_part)} />}
    {manuscript && !running && audit && (audit.state === "failed" || audit.state === "cancelled") &&
      <div className="note note-problem" role="alert">
        <p className="note-lead">{audit.state === "cancelled" ? "The check was cancelled" : explainFailure(audit.error_message).title}</p>
        {audit.state === "failed" && <p>{explainFailure(audit.error_message).detail}</p>}
        {audit.error_message && <p className="note-detail">Technical detail: {audit.error_message}</p>}
        <Ready manuscript={manuscript} again onStart={onStart} />
      </div>}
    {manuscript && !running && audit && hasReview && <Conversation key={audit.id} audit={audit} onOpenReference={onOpenReference} />}
  </section>;
}
