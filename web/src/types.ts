export type Priority = "high" | "moderate" | "low" | "informational";
export type AuditState = "queued" | "processing" | "completed" | "completed_with_issues" | "failed" | "cancelled";
export type DecisionDisposition = "confirm" | "dismiss" | "escalate" | "needs_more_evidence";

export interface Manuscript {
  id: string;
  title: string;
  original_filename: string;
  content_sha256: string;
  latest_audit_id: string | null;
  latest_audit_state: AuditState | null;
  open_findings: number;
  created_at: string;
  updated_at: string;
}

export interface Audit {
  id: string;
  manuscript_id: string;
  job_id: string;
  state: AuditState;
  artifact_path: string | null;
  summary: Record<string, number>;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface Job {
  id: string;
  audit_id: string;
  state: "queued" | "running" | "succeeded" | "failed" | "cancel_requested" | "cancelled";
  progress: number;
  stage: string;
  attempts: number;
  error_code: string | null;
  error_message: string | null;
}

export interface Decision {
  id: string;
  disposition: DecisionDisposition;
  rationale: string;
  reviewer_id: string;
  created_at: string;
}

export interface Finding {
  id: string;
  reference_id: string | null;
  category: string;
  priority: Priority;
  summary: string;
  explanation: string;
  action: string;
  source_locations: Array<{ page?: number; page_start?: number; text?: string }>;
  system_issue: boolean;
  decision?: Decision;
}

export interface Reference {
  id: string;
  marker: string;
  title: string;
  identity_status: string;
  evidence_status: string;
  citation_count: number;
  claim_assessment: string;
  quote_count: number;
  finding_ids: string[];
  topic_similarity?: number | null;
  relevance_score?: number | null;
  relevance_label?: string;
  relevance_basis?: string;
  llm_relevance?: number | null;
  self_citation?: string;
  metadata_severity?: string;
  evidence_kind?: string | null;
}

export interface AuditStartOptions {
  run_missing_citations: boolean;
  author_orcids: string[];
}

export interface Correction {
  id: string;
  kind: string;
  reference_ids: string[];
  proposed_value: Record<string, unknown>;
  rationale: string;
  reviewer_id: string;
  state: string;
  created_at: string;
}

export interface Review {
  title: string;
  processing_status: string;
  review_status: "not_reviewed" | "in_review" | "reviewed";
  counts: Record<string, number>;
  references: Reference[];
  findings: Finding[];
  corrections: Correction[];
  limitations: string[];
}

export interface Activity {
  id: number;
  audit_id: string | null;
  actor_id: string;
  action: string;
  entity_type: string;
  entity_id: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ReferenceEvidence {
  reference: Reference;
  as_cited: {
    raw_text?: string;
    parsed?: { title?: string; authors?: string[]; year?: number; doi?: string };
    source_locations?: Array<{ page?: number; page_start?: number; text?: string }>;
  };
  resolution: { status?: string; metadata_differences?: Array<{ field: string; as_cited: string; candidate: string }> };
  source_snapshot: { title?: string; abstract?: string; source_text?: string; snapshot_id?: string } | null;
  external_metadata: Record<string, unknown>;
  claim_assessments: Array<{
    task_id?: string;
    status: string;
    decision?: string;
    rationale?: string;
    claim_anchor?: { text?: string; page?: number };
    validated_quotes?: Array<{ text: string }>;
  }>;
  findings: Finding[];
}

/* ------------------------------------------------------------------ */
/* Phase 4: identity, assignment, repair, exports, insights, telemetry  */
/* ------------------------------------------------------------------ */

export type Permission = string;

export interface Session {
  tenant_id: string;
  actor_id: string;
  roles: string[];
  permissions: Permission[];
  auth_mode: "local" | "trusted_proxy" | "oidc";
  email: string | null;
  display_name: string | null;
  workspace_backend: "sqlite" | "postgres";
  object_backend: "local" | "s3";
  features: {
    ocr: boolean;
    full_text: boolean;
    resolution_providers: string[];
    telemetry: boolean;
    export_signing: string;
    /** Where manuscript text goes, and on what terms (see src/workspace/data_policy.py). */
    data_handling?: DataHandling;
  };
}

export interface DataHandling {
  terms: "unverified" | "no_training" | "local_only";
  model_recipients: string[];
  reference_lookups: string[];
}

export interface Member {
  subject: string;
  display_name: string | null;
  email: string | null;
  roles: string[];
  active: boolean;
  open_cases?: number;
}

export interface Assignment {
  audit_id: string;
  finding_id: string;
  assignee: string;
  assigned_by: string;
  note: string | null;
  due_at: string | null;
  state: "assigned" | "released";
  updated_at: string;
}

export interface RepairPlan {
  issue_category: string;
  action: string;
  title: string;
  explanation: string;
  expected_outcome: string;
  finding_ids: string[];
  reference_ids: string[];
  affected_findings: number;
  automatic: boolean;
  caveat: string | null;
  previous_attempts: number;
  options: Record<string, boolean>;
}

export interface RepairRecord {
  id: string;
  audit_id: string;
  issue_category: string;
  action: string;
  state: "planned" | "queued" | "running" | "succeeded" | "failed" | "not_applicable";
  attempt: number;
  follow_up_audit_id: string | null;
  detail: Record<string, unknown>;
  created_at: string;
  completed_at: string | null;
}

export interface RepairPlanResponse {
  audit_id: string;
  system_issue_count: number;
  plans: RepairPlan[];
  history: RepairRecord[];
  note: string;
}

export interface ExportRecord {
  id: string;
  audit_id: string;
  format: "pdf" | "html" | "json" | "bundle";
  content_sha256: string;
  size_bytes: number;
  signature_algorithm: string;
  signature_key_id: string;
  created_by: string;
  created_at: string;
  expires_at: string | null;
  manifest?: Record<string, unknown>;
}

export interface MissingCitation {
  id: string;
  title: string;
  authors: string;
  year: string;
  rationale: string;
  verification_status: string;
  already_cited: boolean;
  search_url: string;
  matched_record?: { title?: string | null; doi?: string | null; year?: number | null; id?: string | null } | null;
}

export interface CocitationCandidate {
  openalex_id: string;
  title: string | null;
  year: number | null;
  doi: string | null;
  authors: string[];
  venue: string | null;
  cited_by_count: number | null;
  support_count: number;
  supporting_references: string[];
  url: string;
}

export interface MissingCitations {
  available: boolean;
  generated: boolean;
  items: MissingCitation[];
  cocitation?: {
    status: string;
    references_with_graph: number;
    min_support: number | null;
    candidates: CocitationCandidate[];
  };
  counts?: Record<string, number>;
  analysis_status?: string;
  limitations?: string[];
  metadata: { provider?: string | null; model?: string | null; generated_at?: string | null };
  guidance: string;
  not_a_claim: string;
}

export interface SelfCitationSummary {
  method_version?: string;
  counts: Record<string, number>;
  patterns: Array<{ code: string; detail: string }>;
  questionable: Array<{ reference_id: string; marker: string; reasons: string[]; tier: string }>;
  by_author: Record<string, number>;
  orcids: { supplied?: string[]; front_matter?: string[]; attributed?: string[]; unattributed?: string[] };
  thresholds: Record<string, number>;
  references: Array<{ reference: string; tier: string; counted: boolean; questionable_reasons: string[] }>;
  identity_note?: string;
}

export interface OverlapNode {
  id: string;
  label: string;
  group: "manuscript_author" | "reference_author";
  reference_count: number;
  references?: string[];
}

export interface AuthorOverlap {
  available: boolean;
  self_citation?: SelfCitationSummary | null;
  manuscript_authors: string[];
  nodes: OverlapNode[];
  edges: Array<{ author_id: string; author: string; reference: string; kind: string }>;
  repeated_authors: OverlapNode[];
  counts: {
    total_references: number;
    references_sharing_a_manuscript_author: number;
    distinct_reference_authors: number;
    share_of_references: number;
  };
  guidance: string;
  not_a_claim: string;
}

export interface SloObjective {
  key: string;
  title: string;
  target: number;
  measured: number;
  unit: string;
  comparison: "at_most" | "at_least";
  sample_size: number;
  met: boolean;
  detail: string;
}

export interface SloReport {
  window_hours: number;
  generated_at: string;
  objectives: SloObjective[];
  totals: {
    completed_audits: number;
    succeeded_audits: number;
    failed_audits: number;
    total_cost_usd: number;
    cost_per_audit_usd: number;
    total_input_tokens: number;
    total_output_tokens: number;
    provider_requests: number;
    queue: Record<string, number | Record<string, number>>;
    stage_latency_p50_seconds: Record<string, number>;
    stage_latency_p95_seconds: Record<string, number>;
  };
  met: boolean;
  note: string;
}

/* ------------------------------ Assistant ------------------------------ */
export type AssistantTone = "warning" | "caution" | "neutral" | "ok";

export interface AssistantReference {
  id: string;
  marker: string;
  title: string;
  note: string;
  tone: AssistantTone;
  relevance_score: number | null;
  relevance_label: string | null;
}

/** A message the reader can copy and send, e.g. to the authors. */
export interface AssistantDraft {
  kind: "letter";
  subject: string;
  body: string;
}

export interface AssistantSection {
  intent: string;
  heading: string;
  text: string;
  points: string[];
  references: AssistantReference[];
  more_references: number;
  draft?: AssistantDraft | null;
}

export interface AssistantAction {
  kind: "report";
  format: "pdf";
  style: "standard" | "simple";
  label: string;
}

export interface AssistantAnswer {
  intent: string;
  text: string;
  points: string[];
  references: AssistantReference[];
  more_references: number;
  suggestions: string[];
  grounding: "audit_results" | "model_phrased";
  model: string | null;
  sections?: AssistantSection[];
  actions?: AssistantAction[];
  offer?: string | null;
  offer_text?: string | null;
  register?: "simple" | "standard";
  language?: string;
  reference_ids?: string[];
  draft?: AssistantDraft | null;
  /** The topics a several-part answer covered, so "explain it more simply" can repeat them. */
  topics?: string[];
}

/** One earlier turn, sent back so follow-ups ("why?", "the second one") make sense. */
export interface AssistantTurn {
  role: "user" | "assistant";
  text: string;
  intent?: string | null;
  reference_ids?: string[];
  offer?: string | null;
  style?: "simple" | "standard" | null;
  topics?: string[];
}

/* ------------------------------ Structure ------------------------------ */
export interface DocumentPart {
  number: number;
  page_start: number;
  page_end: number;
  pages: number;
  title: string | null;
  authors: string[];
  references: number;
  reference_heading: string;
}

export interface DocumentStructure {
  multi_part: boolean;
  parts: DocumentPart[];
  pages: number | null;
  reference_lists: number;
  note: string | null;
}
