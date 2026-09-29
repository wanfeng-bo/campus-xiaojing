export interface AssistantFrontendBoundaryInput {
  assistant_sources: Record<string, string>;
  entry_sources: Record<string, string>;
}


/** 只扫描 K7 稳定边界；守卫自身位于 tests，不进入生产依赖图。 */
export function auditAssistantFrontendBoundary(
  input: AssistantFrontendBoundaryInput,
): string[] {
  const issues = new Set<string>();
  for (const source of Object.values(input.assistant_sources)) {
    if (/\bfrom\s+["'][^"']*(?:repairs|repair_)/i.test(source)) {
      issues.add("assistant-business-import");
    }
    if (
      /(?:\b(?:location_text|problem_description|manual_phone|reporter_phone|public_ref)\b|报修|维修|工单)/.test(
        source,
      )
    ) {
      issues.add("assistant-business-meaning");
    }
    if (/\b(?:localStorage|sessionStorage)\b/.test(source)) {
      issues.add("assistant-browser-storage");
    }
    if (/(?:\/tests?\/|\b__tests__\b|\.spec\.)/.test(source)) {
      issues.add("assistant-test-reference");
    }
  }
  for (const source of Object.values(input.entry_sources)) {
    if (/\bAssistantConversation\b|\bassistant_(?:api|contracts|session)\b/.test(source)) {
      issues.add("production-entry-wiring");
    }
  }
  return [...issues];
}
