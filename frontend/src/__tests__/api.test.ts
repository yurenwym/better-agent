import { describe, expect, it, vi } from "vitest";
import { answerAsk, createGoal, createResearch, deletePlanDocument, deleteThread, getBootstrap, getPendingAsk, getPlanDocument, getSkills, getThreadPlan, listEvolutionCandidates, listThreads, putPlanDocument, sendMessage, submitTurn, subscribeToEvents, subscribeToThreadEvents } from "../api";
import type { EventRecord } from "../types";

describe("REST client", () => {
  it("preserves the evolution reason, proposed change and evaluation progress", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ candidates: [{
      id:"candidate-1",candidate_type:"prompt",reason:"研究范围被扩大",proposed_content:{prompts:"scope-bounded"},
      status:"EVALUATED",version:1,experience_ids:["one","two","three"],permission_diff:{added:[]},
      evaluation:{status:"COMPLETED",deterministic_pass:true,checks:{safety:true},metrics:{passed:12,total:12}},
      rollback:{kind:"manual",actor:"user",reason:"user rollback",occurred_at:"2026-08-25T07:36:55Z"},
      record_origin:"demo",evidence_source_kinds:["manual"],
    }] }) });

    const { candidates } = await listEvolutionCandidates(fetcher);

    expect(candidates[0]).toMatchObject({reason:"研究范围被扩大",proposed_content:{prompts:"scope-bounded"},evidence_count:3,evaluation:{passed:12,total:12},rollback:{kind:"manual",reason:"user rollback"},record_origin:"demo",evidence_source_kinds:["manual"]});
  });
  it("loads and saves a conversation-owned Markdown plan with CAS metadata", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ plan: null }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: "plan-1" }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ version: 2 }) });

    await getThreadPlan("thread-1", fetcher);
    await getPlanDocument("plan-1", fetcher);
    await putPlanDocument("plan-1", {
      expected_version: 1,
      expected_content_hash: "sha256:hash",
      title: "Plan",
      markdown: "# Plan\n",
      change_summary: "edit",
    }, "csrf", fetcher);

    expect(fetcher).toHaveBeenNthCalledWith(1, "/api/threads/thread-1/plan");
    expect(fetcher).toHaveBeenNthCalledWith(2, "/api/plans/plan-1");
    expect(fetcher).toHaveBeenNthCalledWith(3, "/api/plans/plan-1", expect.objectContaining({
      method: "PUT",
      body: JSON.stringify({
        expected_version: 1,
        expected_content_hash: "sha256:hash",
        title: "Plan",
        markdown: "# Plan\n",
        change_summary: "edit",
      }),
    }));
  });

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

  it("loads conversation history without mutation headers", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ threads: [] }) });
    await listThreads(fetcher);
    expect(fetcher).toHaveBeenCalledWith("/api/threads");
  });

  it("deletes a conversation thread with CSRF protection", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true });
    await deleteThread("thread-1", "csrf", fetcher);
    expect(fetcher).toHaveBeenCalledWith("/api/threads/thread-1", expect.objectContaining({
      method: "DELETE",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf" }),
    }));
  });

  it("starts research in a dedicated thread with explicit source scopes",async()=>{
    const fetcher=vi.fn().mockResolvedValue({ok:true,json:async()=>({job_id:"research-1"})});
    await createResearch("thread-1",{topic:"SQLite",client_request_id:"request-1",source_scopes:["web"]},"csrf",fetcher);
    expect(fetcher).toHaveBeenCalledWith("/api/threads/thread-1/research",expect.objectContaining({method:"POST",body:JSON.stringify({topic:"SQLite",client_request_id:"request-1",source_scopes:["web"]})}));
  });

  it("deletes a plan with CAS metadata and CSRF protection", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true });

    await deletePlanDocument("plan-1", {
      expected_version: 2,
      expected_content_hash: "sha256:v2",
    }, "csrf", fetcher);

    expect(fetcher).toHaveBeenCalledWith(
      "/api/plans/plan-1",
      expect.objectContaining({
        method: "DELETE",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": "csrf" },
        body: JSON.stringify({ expected_version: 2, expected_content_hash: "sha256:v2" }),
      }),
    );
  });

  it("sends the selected skills only with the current message", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ state: "RECEIVED" }) });

    await sendMessage("goal-1", "Continue", "csrf", ["reflection"], fetcher);

    expect(fetcher).toHaveBeenCalledWith(
      "/api/goals/goal-1/messages",
      expect.objectContaining({ body: JSON.stringify({ content: "Continue", skill_names: ["reflection"] }) }),
    );
  });

  it("loads installed skills from the local runtime", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ skills: [] }) });

    await getSkills(fetcher);

    expect(fetcher).toHaveBeenCalledWith("/api/skills");
  });

  it("submits a structured ask answer with version and CSRF protection", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ask_id: "ask-1", turn: { id: "turn-2" } }) });

    await answerAsk("turn-1", {
      expected_version: 2,
      idempotency_key: "answer-1",
      answers: [{ question_id: "level", selected_options: ["新手"], free_text: "" }],
    }, "csrf", fetcher);

    expect(fetcher).toHaveBeenCalledWith(
      "/api/turns/turn-1/ask/answer",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": "csrf" },
        body: JSON.stringify({
          expected_version: 2,
          idempotency_key: "answer-1",
          answers: [{ question_id: "level", selected_options: ["新手"], free_text: "" }],
        }),
      }),
    );
  });

  it("loads a pending ask without sending mutation headers", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: "ask-1" }) });

    await getPendingAsk("turn-1", fetcher);

    expect(fetcher).toHaveBeenCalledWith("/api/turns/turn-1/ask");
  });

  it("unwraps FastAPI detail errors instead of exposing the raw JSON body", async () => {
    const fetcher = vi.fn().mockResolvedValue({
      ok: false,
      text: async () => JSON.stringify({ detail: "answer the pending ask before sending another message" }),
    });

    await expect(submitTurn("thread-1", {
      client_turn_id: "client-1",
      content: "新的目标",
      skill_names: [],
    }, "csrf", fetcher)).rejects.toThrow("answer the pending ask before sending another message");
  });

  it("keeps the event stream in follow mode and stops reconnecting after a terminal event", () => {
    let handler: ((event: Event) => void) | undefined;
    const close = vi.fn();
    class FakeEventSource {
      static instance: FakeEventSource;
      url: string;
      constructor(url: string) {
        this.url = url;
        FakeEventSource.instance = this;
      }
      addEventListener(_type: string, callback: (event: Event) => void) {
        handler = callback;
      }
      close = close;
    }
    vi.stubGlobal("EventSource", FakeEventSource);

    subscribeToEvents("run-1", 4, () => undefined);

    expect(FakeEventSource.instance.url).toContain("follow=1");
    const event: EventRecord = {
      schema_version: 1,
      event_id: "evt-5",
      seq: 5,
      run_id: "run-1",
      goal_id: "goal-1",
      type: "run.completed",
      occurred_at: "2026-08-18T00:00:00Z",
      actor: "runtime",
      correlation: {},
      data: {},
    };
    handler?.({ data: JSON.stringify(event), lastEventId: "5" } as MessageEvent<string>);

    expect(close).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("keeps the thread stream open after a completed parent turn", () => {
    let handler: ((event: Event) => void) | undefined;
    const close = vi.fn();
    class FakeEventSource {
      static instance: FakeEventSource;
      constructor(public url: string) {
        FakeEventSource.instance = this;
      }
      addEventListener(_type: string, callback: (event: Event) => void) {
        handler = callback;
      }
      close = close;
    }
    vi.stubGlobal("EventSource", FakeEventSource);

    subscribeToThreadEvents("thread-1", 4, () => undefined);
    const completed: EventRecord = {
      schema_version: 1,
      event_id: "evt-5",
      seq: 5,
      run_id: "thread-1",
      goal_id: "",
      type: "turn.completed",
      occurred_at: "2026-08-18T00:00:00Z",
      actor: "user",
      correlation: {},
      data: { continuation_turn_id: "turn-2" },
    };
    handler?.({ data: JSON.stringify(completed), lastEventId: "5" } as MessageEvent<string>);

    expect(FakeEventSource.instance.url).toContain("follow=1");
    expect(close).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("closes the thread stream after a final completed turn", () => {
    let handler: ((event: Event) => void) | undefined;
    const close = vi.fn();
    class FakeEventSource {
      static instance: FakeEventSource;
      constructor(public url: string) {
        FakeEventSource.instance = this;
      }
      addEventListener(_type: string, callback: (event: Event) => void) {
        handler = callback;
      }
      close = close;
    }
    vi.stubGlobal("EventSource", FakeEventSource);

    subscribeToThreadEvents("thread-1", 4, () => undefined);
    const completed: EventRecord = {
      schema_version: 1,
      event_id: "evt-5-final",
      seq: 5,
      run_id: "thread-1",
      goal_id: "",
      type: "turn.completed",
      occurred_at: "2026-08-18T00:00:00Z",
      actor: "worker",
      correlation: {},
      data: {},
    };
    handler?.({ data: JSON.stringify(completed), lastEventId: "5" } as MessageEvent<string>);

    expect(close).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
