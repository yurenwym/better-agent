import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ResearchSourcesPanel from "../components/ResearchSourcesPanel";

const api = vi.hoisted(() => ({ getResearchSources: vi.fn() }));
vi.mock("../api", () => ({ ...api }));
afterEach(cleanup);

const source = {
  id: "source-1", ordinal: 1, kind: "web",
  canonical_url: "https://www.postgresql.org/docs/current/sql-explain.html",
  locator: null, title: "PostgreSQL: Documentation: EXPLAIN",
  published_at: null, retrieved_at: "2026-09-08T07:39:36.173696+00:00", quality_score: 0.5275,
};

describe("ResearchSourcesPanel", () => {
  it("lists fetched sources with host and quality", async () => {
    api.getResearchSources.mockResolvedValue({ sources: [source] });
    render(<ResearchSourcesPanel jobId="job-1" />);

    const link = await screen.findByRole("link", { name: "PostgreSQL: Documentation: EXPLAIN" });
    expect(link.getAttribute("href")).toBe(source.canonical_url);
    expect(screen.getByText(/postgresql\.org/)).toBeTruthy();
    expect(screen.getByText(/质量 53%/)).toBeTruthy();
  });

  it("shows traceability conclusions with their supported state", async () => {
    api.getResearchSources.mockResolvedValue({ sources: [] });
    render(<ResearchSourcesPanel jobId="job-1" traceability={[{
      requirement: "解释 EXPLAIN 的输出", conclusion: "EXPLAIN 展示实际执行计划。",
      evidence_ids: ["e1", "e2"], source_ids: ["s1"], citation_source_ids: ["s1"],
      source_versions: [], evidence_locations: [], supported: true,
    }]} />);

    expect(await screen.findByText("解释 EXPLAIN 的输出")).toBeTruthy();
    expect(screen.getByText("有证据支撑")).toBeTruthy();
    expect(screen.getByText("证据 2 条 · 来源 1 个")).toBeTruthy();
  });

  it("renders nothing when the job has neither sources nor traceability", async () => {
    api.getResearchSources.mockResolvedValue({ sources: [] });
    const { container } = render(<ResearchSourcesPanel jobId="job-1" />);

    await waitFor(() => expect(api.getResearchSources).toHaveBeenCalledWith("job-1"));
    expect(container.querySelector(".research-sources")).toBeNull();
  });
});
