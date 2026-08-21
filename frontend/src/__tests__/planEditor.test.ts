import { describe, expect, it } from "vitest";
import { parseMarkdownForEditor, serializeEditorBlocks, type EditorBlock } from "../planEditor";

describe("plan editor markdown model", () => {
  it("parses the structures shown by the visual editor", () => {
    expect(parseMarkdownForEditor(
      "# Trip\n\nOverview\n\n- Train\n- Hotel\n\n1. Pack\n2. Leave\n\n> Book early\n\n| Day | Place |\n| --- | --- |\n| 1 | 桂林 |\n\n```text\nhello\n```\n\n---",
    )).toEqual([
      { id: "block-0", type: "heading", level: 1, text: "Trip" },
      { id: "block-1", type: "paragraph", text: "Overview" },
      { id: "block-2", type: "bullet-list", items: ["Train", "Hotel"] },
      { id: "block-3", type: "ordered-list", items: ["Pack", "Leave"] },
      { id: "block-4", type: "blockquote", text: "Book early" },
      { id: "block-5", type: "table", headers: ["Day", "Place"], rows: [["1", "桂林"]] },
      { id: "block-6", type: "code", language: "text", text: "hello" },
      { id: "block-7", type: "rule" },
    ]);
  });

  it("serializes edited rendered blocks back to Markdown", () => {
    const blocks: EditorBlock[] = [
      { id: "block-0", type: "heading", level: 2, text: "Updated title" },
      { id: "block-1", type: "paragraph", text: "Updated body." },
      { id: "block-2", type: "bullet-list", items: ["One", "Two"] },
      { id: "block-3", type: "table", headers: ["Day", "Place"], rows: [["1", "桂林"]] },
    ];

    expect(serializeEditorBlocks(blocks)).toBe(
      "## Updated title\n\nUpdated body.\n\n- One\n- Two\n\n| Day | Place |\n| --- | --- |\n| 1 | 桂林 |",
    );
  });

  it("preserves unsupported lines as editable raw blocks", () => {
    const blocks = parseMarkdownForEditor(":::warning\nKeep this block\n:::");

    expect(blocks).toEqual([
      { id: "block-0", type: "raw", text: ":::warning" },
      { id: "block-1", type: "paragraph", text: "Keep this block" },
      { id: "block-2", type: "raw", text: ":::" },
    ]);
    expect(serializeEditorBlocks(blocks)).toBe(":::warning\n\nKeep this block\n\n:::");
  });
});
