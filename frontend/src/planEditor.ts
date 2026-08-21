export type EditorBlock =
  | { id: string; type: "heading"; level: number; text: string }
  | { id: string; type: "paragraph"; text: string }
  | { id: string; type: "bullet-list"; items: string[] }
  | { id: string; type: "ordered-list"; items: string[] }
  | { id: string; type: "blockquote"; text: string }
  | { id: string; type: "table"; headers: string[]; rows: string[][] }
  | { id: string; type: "code"; language: string; text: string }
  | { id: string; type: "rule" }
  | { id: string; type: "raw"; text: string };

type WithoutId<T> = T extends { id: string } ? Omit<T, "id"> : never;
type EditorBlockInput = WithoutId<EditorBlock>;

function addBlock(blocks: EditorBlock[], block: EditorBlockInput): void {
  blocks.push({ id: `block-${blocks.length}`, ...block } as EditorBlock);
}

function splitTableRow(line: string): string[] {
  const value = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells: string[] = [];
  let cell = "";
  let escaped = false;
  for (const character of value) {
    if (character === "|" && !escaped) {
      cells.push(cell.trim().replace(/\\\|/g, "|"));
      cell = "";
      continue;
    }
    cell += character;
    escaped = character === "\\" && !escaped;
    if (character !== "\\") escaped = false;
  }
  cells.push(cell.trim().replace(/\\\|/g, "|"));
  return cells;
}

function isTableSeparator(line: string): boolean {
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function isHeading(line: string): boolean {
  return /^\s{0,3}#{1,6}\s+/.test(line);
}

function isHorizontalRule(line: string): boolean {
  return /^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$/.test(line);
}

function isUnorderedItem(line: string): boolean {
  return /^\s*[-*+]\s+/.test(line);
}

function isOrderedItem(line: string): boolean {
  return /^\s*\d+[.)]\s+/.test(line);
}

function isBlockquote(line: string): boolean {
  return /^\s*>/.test(line);
}

function isCodeFence(line: string): boolean {
  return /^\s*(```|~~~)/.test(line);
}

function isTableStart(lines: string[], index: number): boolean {
  return index + 1 < lines.length && lines[index].includes("|") && isTableSeparator(lines[index + 1]);
}

function isStructuredStart(lines: string[], index: number): boolean {
  const line = lines[index];
  return isHeading(line)
    || isHorizontalRule(line)
    || isUnorderedItem(line)
    || isOrderedItem(line)
    || isBlockquote(line)
    || isCodeFence(line)
    || isTableStart(lines, index)
    || line.trim().startsWith(":::");
}

function headingText(line: string): { level: number; text: string } {
  const match = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
  return { level: match?.[1].length ?? 1, text: match?.[2].trim() ?? line.trim() };
}

function listItemText(line: string): string {
  return line.replace(/^\s*(?:[-*+]|\d+[.)])\s+/, "").trim();
}

export function parseMarkdownForEditor(markdown: string): EditorBlock[] {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const blocks: EditorBlock[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    if (isCodeFence(line)) {
      const opening = line.match(/^\s*(```|~~~)\s*(.*)$/);
      const fence = opening?.[1] ?? "```";
      const language = opening?.[2]?.trim() ?? "";
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !new RegExp(`^\\s*${fence}`).test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      addBlock(blocks, { type: "code", language, text: codeLines.join("\n") });
      continue;
    }

    if (isHeading(line)) {
      addBlock(blocks, { type: "heading", ...headingText(line) });
      index += 1;
      continue;
    }

    if (isHorizontalRule(line)) {
      addBlock(blocks, { type: "rule" });
      index += 1;
      continue;
    }

    if (isTableStart(lines, index)) {
      const headers = splitTableRow(line);
      index += 2;
      const rows: string[][] = [];
      while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
        rows.push(splitTableRow(lines[index]));
        index += 1;
      }
      addBlock(blocks, { type: "table", headers, rows });
      continue;
    }

    if (isUnorderedItem(line)) {
      const items: string[] = [];
      while (index < lines.length && isUnorderedItem(lines[index])) {
        items.push(listItemText(lines[index]));
        index += 1;
      }
      addBlock(blocks, { type: "bullet-list", items });
      continue;
    }

    if (isOrderedItem(line)) {
      const items: string[] = [];
      while (index < lines.length && isOrderedItem(lines[index])) {
        items.push(listItemText(lines[index]));
        index += 1;
      }
      addBlock(blocks, { type: "ordered-list", items });
      continue;
    }

    if (isBlockquote(line)) {
      const quoteLines: string[] = [];
      while (index < lines.length && isBlockquote(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      addBlock(blocks, { type: "blockquote", text: quoteLines.join("\n") });
      continue;
    }

    if (line.trim().startsWith(":::")) {
      addBlock(blocks, { type: "raw", text: line });
      index += 1;
      continue;
    }

    const paragraphLines = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !isStructuredStart(lines, index)) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    addBlock(blocks, { type: "paragraph", text: paragraphLines.join("\n").trim() });
  }

  return blocks;
}

function escapeTableCell(value: string): string {
  return value.trim().replace(/\|/g, "\\|").replace(/\r?\n/g, " ");
}

function serializeBlock(block: EditorBlock): string {
  switch (block.type) {
    case "heading":
      return block.text.trim() ? `${"#".repeat(Math.min(6, Math.max(1, block.level)))} ${block.text.trim()}` : "";
    case "paragraph":
      return block.text.trim();
    case "bullet-list":
      return block.items.filter((item) => item.trim()).map((item) => `- ${item.trim()}`).join("\n");
    case "ordered-list":
      return block.items.filter((item) => item.trim()).map((item, index) => `${index + 1}. ${item.trim()}`).join("\n");
    case "blockquote":
      return block.text.trim() ? block.text.trim().split("\n").map((line) => `> ${line.trim()}`).join("\n") : "";
    case "table": {
      const width = Math.max(block.headers.length, ...block.rows.map((row) => row.length), 1);
      const headers = Array.from({ length: width }, (_, index) => escapeTableCell(block.headers[index] ?? ""));
      const separator = Array.from({ length: width }, () => "---");
      const rows = block.rows.map((row) => Array.from({ length: width }, (_, index) => escapeTableCell(row[index] ?? "")));
      return [
        `| ${headers.join(" | ")} |`,
        `| ${separator.join(" | ")} |`,
        ...rows.map((row) => `| ${row.join(" | ")} |`),
      ].join("\n");
    }
    case "code":
      return `\`\`\`${block.language.trim()}\n${block.text}\n\`\`\``;
    case "rule":
      return "---";
    case "raw":
      return block.text;
  }
}

export function serializeEditorBlocks(blocks: EditorBlock[]): string {
  return blocks.map(serializeBlock).filter(Boolean).join("\n\n").trim();
}
