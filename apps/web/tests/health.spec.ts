import { describe, expect, it, vi } from "vitest";

import { fetchHealth } from "../src/app/health";


describe("fetchHealth", () => {
  it("requests the fixed health endpoint and returns its payload", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "ok", database: "ok" }), { status: 200 }),
    );

    await expect(fetchHealth(fetcher)).resolves.toEqual({ status: "ok", database: "ok" });
    expect(fetcher).toHaveBeenCalledWith("/api/v1/health", {
      headers: { Accept: "application/json" },
    });
  });

  it("rejects an unavailable health response", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response("", { status: 503 }));

    await expect(fetchHealth(fetcher)).rejects.toThrow("503");
  });
});
