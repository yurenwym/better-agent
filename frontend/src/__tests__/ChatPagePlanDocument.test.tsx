import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ConversationThread from "../components/ConversationThread";
import { latestPlanReference } from "../pages/ChatPage";
import type { ThreadEvent } from "../types";

afterEach(cleanup);

describe("plan document chat reference", () => {
  const answer = { id: "answer-1", run_id: "thread-1", interaction_id: null, role: "assistant" as const, content: "# 学习安排\n每天练习一道题", created_at: "2026-09-13T00:00:00Z" };

  it("requires explicit confirmation and title, then saves exactly one selected answer", async () => {
    let finish!: (value: boolean) => void;
    const save = vi.fn(() => new Promise<boolean>(resolve => { finish = resolve; }));
    render(<ConversationThread messages={[answer]} onSaveMessagePlan={save} onSubmit={() => undefined} />);
    expect(screen.queryByRole("button", { name: "保存并安排日程" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "将此回答设为计划" }));
    expect((screen.getByRole("button", { name: "保存并安排日程" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("计划名称"), { target: { value: "一周练习" } });
    fireEvent.click(screen.getByLabelText("确认将这条回答全文作为计划正文"));
    fireEvent.click(screen.getByRole("button", { name: "保存并安排日程" }));
    fireEvent.click(screen.getByRole("button", { name: "正在保存…" }));
    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith("answer-1", "一周练习");
    await act(async () => finish(true));
  });

  it("keeps the confirmed answer available after a save failure", async () => {
    render(<ConversationThread messages={[answer]} onSaveMessagePlan={async () => false} onSubmit={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "将此回答设为计划" }));
    fireEvent.change(screen.getByLabelText("计划名称"), { target: { value: "练习" } });
    fireEvent.click(screen.getByLabelText("确认将这条回答全文作为计划正文"));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "保存并安排日程" })));
    expect(screen.getByRole("region", { name: "确认计划正文" })).toBeTruthy();
    expect((screen.getByLabelText("计划名称") as HTMLInputElement).value).toBe("练习");
  });

  it.each([{ streaming: true }, { status: "failed" }, { status: "cancelled" }, { status: "interrupted" }, { presentation: "human_bubbles" as const }, { research_job_id: "job-1" }, { plan_document_version_id: "version-1" }])("does not offer conversion for a non-final or already saved answer %j", patch => {
    render(<ConversationThread messages={[{ ...answer, ...patch }]} onSaveMessagePlan={vi.fn()} onSubmit={() => undefined} />);
    expect(screen.queryByRole("button", { name: "将此回答设为计划" })).toBeNull();
  });

  it("opens execution settings for a saved answer without another save", () => {
    const arrange = vi.fn();
    const save = vi.fn();
    render(<ConversationThread messages={[answer]} planReference={{ planDocumentId: "plan-1", version: 1, messageId: answer.id, status: "ready" }} onArrangePlan={arrange} onSaveMessagePlan={save} onSubmit={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "安排日程" }));
    expect(arrange).toHaveBeenCalledWith("plan-1");
    expect(save).not.toHaveBeenCalled();
  });
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

  it("clears a stale saved-plan reference after the document is deleted", () => {
    expect(latestPlanReference([
      event("plan.document_ready", {
        plan_document_id: "plan-1",
        version_id: "version-1",
        version: 1,
        source_message_id: "assistant-plan",
        content_hash: `sha256:${"a".repeat(64)}`,
        actor: "model",
      }, 1),
      event("plan.document_deleted", { plan_document_id: "plan-1" }, 2),
    ])).toBeNull();
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
