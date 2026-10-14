import type {
  Activity, Assignment, Audit, AuthorOverlap, Decision, DecisionDisposition, ExportRecord,
  Job, Manuscript, Member, MissingCitations, ReferenceEvidence, RepairPlanResponse, Review,
  Session, SloReport, AuditStartOptions, AssistantAnswer, AssistantTurn, DocumentStructure, DocumentPart,
} from "./types";

const API = import.meta.env.VITE_CITEPRISM_API ?? "/api/v1";

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      "X-CitePrism-Actor": "local-reviewer",
      "X-CitePrism-Roles": "reviewer,operator",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(response.status, body.detail ?? body.code ?? "Request failed");
  }
  return response.json() as Promise<T>;
}

export const api = {
  manuscripts: () => request<{ items: Manuscript[] }>("/manuscripts"),
  manuscript: (id: string) => request<Manuscript>(`/manuscripts/${id}`),
  audits: (id: string) => request<{ items: Audit[] }>(`/manuscripts/${id}/audits`),
  review: (id: string) => request<Review>(`/audits/${id}/review`),
  audit: (id: string) => request<Audit>(`/audits/${id}`),
  reference: (auditId: string, referenceId: string) => request<ReferenceEvidence>(
    `/audits/${auditId}/references/${encodeURIComponent(referenceId)}`
  ),
  job: (id: string) => request<Job>(`/jobs/${id}`),
  activity: (auditId?: string) => request<{ items: Activity[] }>(
    `/activity${auditId ? `?audit_id=${encodeURIComponent(auditId)}` : ""}`
  ),
  upload: async (file: File, title?: string) => {
    const form = new FormData();
    form.append("file", file);
    const suffix = title ? `?title=${encodeURIComponent(title)}` : "";
    return request<Manuscript>(`/manuscripts${suffix}`, { method: "POST", body: form });
  },
  startAudit: (manuscriptId: string, options?: Partial<AuditStartOptions>) => request<{ audit: Audit; job: Job }>(
    `/manuscripts/${manuscriptId}/audits`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        force_parse: false, force_enrich: false, force_score: false,
        run_missing_citations: options?.run_missing_citations ?? false,
        author_orcids: options?.author_orcids ?? [],
      }),
    }
  ),
  decide: (auditId: string, findingId: string, disposition: DecisionDisposition,
           rationale: string, expected?: string) => request<Decision>(
    `/audits/${auditId}/findings/${findingId}/decisions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ disposition, rationale, expected_current_decision_id: expected ?? null }),
    }
  ),
  proposeCorrection: (auditId: string, referenceIds: string[], kind: string,
                      value: Record<string, unknown>, rationale: string) => request(
    `/audits/${auditId}/corrections`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ kind, reference_ids: referenceIds, proposed_value: value,
                             rationale, supersedes_correction_id: null }),
    }
  ),
  pdfUrl: (auditId: string) => `${API}/audits/${auditId}/manuscript.pdf`,

  /* --------------------------- Phase 4 --------------------------- */
  session: () => request<Session>("/session"),
  members: () => request<{ items: Member[] }>("/organizations/members"),
  workload: () => request<{ items: Member[] }>("/assignments/workload"),
  assignments: (auditId?: string) => request<{ items: Assignment[] }>(
    `/assignments${auditId ? `?audit_id=${encodeURIComponent(auditId)}` : ""}`
  ),
  assign: (auditId: string, findingId: string, assignee: string, note?: string) =>
    request<Assignment>(`/audits/${auditId}/assignments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ finding_id: findingId, assignee, note: note ?? null, due_at: null }),
    }),
  unassign: (auditId: string, findingId: string) =>
    request<Assignment>(`/audits/${auditId}/assignments/${encodeURIComponent(findingId)}`,
      { method: "DELETE" }),

  repairPlan: (auditId: string) => request<RepairPlanResponse>(`/audits/${auditId}/repairs`),
  runRepair: (auditId: string, categories: string[]) =>
    request<{ audit: Audit; job: Job; repaired_categories: string[]; note: string }>(
      `/audits/${auditId}/repairs`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ categories }),
      }),

  exports: (auditId: string) => request<{ items: ExportRecord[] }>(`/audits/${auditId}/exports`),
  createExport: (auditId: string, format: ExportRecord["format"],
                 options?: { variant?: "review" | "report"; style?: "standard" | "simple" }) =>
    request<ExportRecord>(`/audits/${auditId}/exports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format, variant: options?.variant ?? "review", style: options?.style ?? "standard" }),
    }),
  exportUrl: (exportId: string) => `${API}/exports/${exportId}/download`,

  missingCitations: (auditId: string) =>
    request<MissingCitations>(`/audits/${auditId}/missing-citations`),
  authorOverlap: (auditId: string) => request<AuthorOverlap>(`/audits/${auditId}/author-overlap`),

  /* ------------------------- Structure ------------------------- */
  structure: (manuscriptId: string) => request<DocumentStructure>(`/manuscripts/${manuscriptId}/structure`),
  splitIntoParts: (manuscriptId: string, parts?: number[]) =>
    request<{ items: Array<{ part: DocumentPart; manuscript: Manuscript }> }>(`/manuscripts/${manuscriptId}/parts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parts: parts ?? null }),
    }),

  /* ------------------------- Assistant ------------------------- */
  assistantSummary: (auditId: string) => request<AssistantAnswer>(`/audits/${auditId}/assistant`),
  ask: (auditId: string, question: string, history: AssistantTurn[] = []) =>
    request<AssistantAnswer>(`/audits/${auditId}/assistant`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history: history.slice(-12) }),
    }),

  slos: (hours?: number) => request<SloReport>(`/telemetry/slos${hours ? `?hours=${hours}` : ""}`),
};
