import { describe, expect, it } from "vitest";

import { auditAssistantFrontendBoundary } from "./assistant_frontend_guard";


const assistant_modules = import.meta.glob<string>(
  "../src/features/assistant/*.{ts,vue}",
  { eager: true, import: "default", query: "?raw" },
);
const entry_modules = import.meta.glob<string>(
  [
    "../src/main.ts",
    "../src/app/DingTalkEntryApp.vue",
  ],
  { eager: true, import: "default", query: "?raw" },
);


describe("assistant frontend boundary", () => {
  it("keeps the K7 shell business-free and outside all production entries", () => {
    const assistant_sources = assistant_modules;
    const entry_sources = entry_modules;

    expect(auditAssistantFrontendBoundary({ assistant_sources, entry_sources })).toEqual([]);
    expect(
      Object.keys(assistant_modules)
        .map((path) => path.split("/").at(-1) ?? "")
        .sort(),
    ).toEqual([
      "AssistantConversation.vue",
      "assistant_api.ts",
      "assistant_contracts.ts",
      "assistant_session.ts",
    ]);
  });

  it("proves the guard rejects business imports, business meaning, storage and entry wiring", () => {
    const issues = auditAssistantFrontendBoundary({
      assistant_sources: {
        "assistant_api.ts": [
          'import { isRepairDraft } from "../repairs/repair_contracts";',
          "const location_text = '报修工单';",
          "window.localStorage.setItem('draft', location_text);",
          'import probe from "../../../tests/probe";',
        ].join("\n"),
      },
      entry_sources: {
        "src/main.ts": 'import AssistantConversation from "./features/assistant/AssistantConversation.vue";',
      },
    });

    expect(issues).toEqual(expect.arrayContaining([
      "assistant-business-import",
      "assistant-business-meaning",
      "assistant-browser-storage",
      "assistant-test-reference",
      "production-entry-wiring",
    ]));
  });
});
