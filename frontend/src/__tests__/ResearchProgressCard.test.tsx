import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ResearchProgressCard from "../components/ResearchProgressCard";

it("renders structured research progress and cancel action", () => {
  const cancel = vi.fn();
  render(<ResearchProgressCard job={{ id:"r",thread_id:"t",source_turn_id:"x",schedule_id:null,retry_of_job_id:null,trigger_kind:"manual",topic:"研究 SQLite",source_scopes:["web"],status:"RUNNING",phase:"distilling",attempts:1,cancel_requested_at:null,created_at:"n",updated_at:"n",title:null,source_count:3,evidence_count:8,assistant_message_id:"m" }} onCancel={cancel} />);
  expect(screen.getByText("正在提炼证据")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "取消研究" }));
  expect(cancel).toHaveBeenCalled();
});
