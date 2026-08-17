import { describe, expect, it, vi } from "vitest";
import { createGoal, getBootstrap } from "../api";

describe("REST client", () => {
  it("sends JSON and CSRF headers for goal creation", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: "g1" }) });

    await createGoal({ title: "Ship", description: "A proposal" }, "csrf", fetcher);

    expect(fetcher).toHaveBeenCalledWith(
      "/api/goals",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": "csrf" },
      }),
    );
  });

  it("reads bootstrap data without exposing an API key", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ csrf_token: "csrf" }) });

    const result = await getBootstrap(fetcher);

    expect(result.csrf_token).toBe("csrf");
    expect(fetcher).toHaveBeenCalledWith("/api/bootstrap");
  });
});
