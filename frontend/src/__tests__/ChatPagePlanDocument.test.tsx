import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ConversationThread from "../components/ConversationThread";
import { latestPlanReference } from "../pages/ChatPage";
import type { ThreadEvent } from "../types";

afterEach(cleanup);

describe("plan document chat reference", () => {
  function event(type: string, data: Record<string, unknown>, seq: number): ThreadEvent {
    return {
      schema_version: 1,
      event_id: `event-${seq}`,
      seq,
      thread_id: "thread-1",
      turn_id: `turn-${seq}`,
      type,
      occurred_at: "2026-08-19T00:00:00Z",
      actor: "worker",
      data,
    };
  }

  it("ignores an incomplete failure event instead of hiding the last valid reference", () => {
    expect(latestPlanReference([
      event("plan.document_ready", {
        plan_document_id: "plan-1",
        version_id: "version-1",
        version: 1,
        source_message_id: "assistant-plan",
        content_hash: `sha256:${"a".repeat(64)}`,
        actor: "model",
      }, 1),
      event("plan.document_failed", { reason: "no document id" }, 2),
    ])).toEqual({
      planDocumentId: "plan-1",
      versionId: "version-1",
      version: 1,
      messageId: "assistant-plan",
      status: "ready",
    });
  });

  it("returns a retry reference for a complete failure event", () => {
    const reference = latestPlanReference([
      event("plan.document_failed", {
        plan_document_id: "plan-1",
        version_id: "version-2",
        version: 2,
        source_message_id: "assistant-plan-failed",
        content_hash: `sha256:${"b".repeat(64)}`,
        actor: "model",
      }, 1),
    ]);
    expect(reference?.status).toBe("failed");
  });

  it("ignores a failure event with an incomplete hash or actor", () => {
    expect(latestPlanReference([
      event("plan.document_ready", {
        plan_document_id: "plan-1",
        version_id: "version-1",
        version: 1,
        source_message_id: "assistant-plan",
        content_hash: `sha256:${"a".repeat(64)}`,
        actor: "model",
      }, 1),
      event("plan.document_failed", {
        plan_document_id: "plan-1",
        version_id: "version-2",
        version: 2,
        source_message_id: "assistant-plan-failed",
        content_hash: "",
        actor: "",
      }, 2),
    ])?.messageId).toBe("assistant-plan");
  });

  it("shows the saved-version card and opens the stable plan id", () => {
    const openPlan = vi.fn();
    render(
      <ConversationThread
        messages={[{
          id: "assistant-plan",
          run_id: "thread-1",
          interaction_id: null,
          role: "assistant",
          content: "# Saved plan",
          created_at: "2026-08-19T00:00:00Z",
        }]}
        planReference={{ planDocumentId: "plan-1", version: 2, messageId: "assistant-plan" }}
        onOpenPlan={openPlan}
        onSubmit={() => undefined}
      />,
    );

    expect(screen.getByText("已保存到计划 · v2")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看 / 编辑计划" }));
    expect(openPlan).toHaveBeenCalledWith("plan-1");
  });

  it("shows a stable retry reference when plan projection fails", () => {
    const openPlan = vi.fn();
    render(
      <ConversationThread
        messages={[{
          id: "assistant-plan-failed",
          run_id: "thread-1",
          interaction_id: null,
          role: "assistant",
          content: "# Draft plan",
          created_at: "2026-08-19T00:00:00Z",
        }]}
        planReference={{ planDocumentId: "plan-1", version: 3, messageId: "assistant-plan-failed", status: "failed" }}
        onOpenPlan={openPlan}
        onSubmit={() => undefined}
      />,
    );

    expect(screen.getByText("计划文件写入未完成 · v3")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "打开并重试" }));
    expect(openPlan).toHaveBeenCalledWith("plan-1");
  });
});
