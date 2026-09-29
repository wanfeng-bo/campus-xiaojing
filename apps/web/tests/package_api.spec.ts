import { afterEach, describe, expect, it, vi } from "vitest";
import { createPackageApi, PackageApiError } from "../src/features/school_facts/package_api";
afterEach(()=>vi.unstubAllGlobals());
describe("F7 package API",()=>{
  it("uploads source bytes only to the identity-bound Product package route",async()=>{
    const fetcher=vi.fn().mockResolvedValue(new Response(JSON.stringify({source_ref:"sf3source_1234567890",source_sha256:"a".repeat(64),expires_at:"2099-01-01T00:00:00Z"})));
    vi.stubGlobal("fetch",fetcher);
    await createPackageApi().upload(new File(["x"],"people.json"));
    expect(fetcher).toHaveBeenCalledWith("/api/v1/school-fact-product/imports/sources",expect.objectContaining({method:"POST",credentials:"same-origin",cache:"no-store"}));
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({filename_summary:"people.json",content_base64:"eA=="});
  });
  it("queries the original locator with GET and preserves permission errors without server text",async()=>{
    const fetcher=vi.fn().mockResolvedValue(new Response(JSON.stringify({message:"PRIVATE_ERROR"}),{status:403}));vi.stubGlobal("fetch",fetcher);
    await expect(createPackageApi().result("sf3plan_1234567890")).rejects.toEqual(expect.objectContaining({status:403}));
    expect(fetcher.mock.calls[0][0]).toBe("/api/v1/school-fact-product/imports/plans/sf3plan_1234567890/result");
    expect(fetcher.mock.calls[0][1].method).toBe("GET");
    expect(new PackageApiError(403).message).not.toContain("PRIVATE_ERROR");
  });
});
