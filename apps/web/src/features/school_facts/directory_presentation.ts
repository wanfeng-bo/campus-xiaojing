import { isDirectoryBundle, isDirectoryQuery, normalizeDirectoryFact, type DirectoryQuery, type DirectoryRecord } from "./directory_contracts";

interface Candidate { reference: string; label: string }
interface CandidateEntry { result: DirectoryQuery; pending: boolean; candidates: Candidate[] }
interface EvidenceGroup { label: string; results: DirectoryQuery[] }
export interface DirectoryPresentation { evidenceGroups: EvidenceGroup[]; candidates: CandidateEntry[] }
const needsCandidate = (query: DirectoryQuery) => ["ambiguous", "selection_required"].includes(query.status);
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);

function names(rows: DirectoryRecord[]): Candidate[] {
  return rows.map((row, index) => {
    const name = row.fields.display_name;
    return { reference: row.reference, label: typeof name === "string" && name.trim()
      ? `候选对象${index + 1}：${name}` : `候选对象${index + 1}（名称未提供）` };
  });
}

/** Only classify this result's evidence. Later calls and answer wording never resolve a choice. */
export function directoryPresentation(facts: unknown[]): DirectoryPresentation {
  const evidenceGroups: EvidenceGroup[] = [];
  const candidates: CandidateEntry[] = [];
  for (const [index, fact] of facts.entries()) {
    if (isDirectoryQuery(fact)) {
      evidenceGroups.push({ label: "正式查询依据", results: [fact] });
      if (needsCandidate(fact)) candidates.push({ result: fact, pending: false, candidates: names(fact.records) });
      continue;
    }
    if (!isDirectoryBundle(fact)) continue;
    const results = fact.results.filter(isDirectoryQuery).map(row => normalizeDirectoryFact(row) as DirectoryQuery);
    if (results.length) evidenceGroups.push({ label: fact.subject_label || `查询依据 ${index + 1}`, results });
    const meta = fact as unknown as Record<string, unknown>;
    const requested = meta.requested_fact_kinds;
    const hasRequested = Array.isArray(requested) && requested.length > 0 && requested.every(kind => typeof kind === "string");
    const subject = meta.subject_reference;
    const subjectRows = results.flatMap(result => result.records).filter(row => row.reference === subject);
    const resolved = typeof subject === "string" && hasRequested && subjectRows.length > 0
      && subjectRows.every(row => JSON.stringify(row) === JSON.stringify(subjectRows[0]))
      && ["completed", "incomplete"].includes(String(meta.query_completion))
      && !["ambiguous", "selection_required"].includes(fact.status);
    const pending = !subject && hasRequested && meta.query_completion === "subject_unresolved"
      && ["ambiguous", "selection_required"].includes(fact.status);
    const rawCandidates = meta.candidates;
    const candidateRefs = Array.isArray(rawCandidates) && rawCandidates.length > 0
      && rawCandidates.every(row => object(row) && typeof row.reference === "string")
      ? new Set(rawCandidates.map(row => (row as Record<string, unknown>).reference as string)) : null;
    const selectableResults = results.filter(needsCandidate);
    const hasCandidateSubset = pending && candidateRefs !== null && [...candidateRefs].every(reference =>
      selectableResults.some(result => result.records.some(row => row.reference === reference)));
    for (const result of selectableResults) {
      // The domain resolved this bundle's subject; requested fact ambiguity stays available.
      if (resolved && !(requested as string[]).includes(result.fact_kind)) continue;
      const rows = hasCandidateSubset ? result.records.filter(row => candidateRefs!.has(row.reference)) : result.records;
      if (hasCandidateSubset && rows.length === 0) continue;
      candidates.push({ result, pending, candidates: names(rows) });
    }
  }
  return { evidenceGroups, candidates };
}
