import type { ReactNode } from "react";

interface MarkdownMessageProps {
  content: string;
  className?: string;
}

const FENCE_PATTERN = /^\s*\x60{3,}[^\s]*\s*$/;
const HEADING_PATTERN = /^\s*(#{1,6})\s+(.+?)\s*#*\s*$/;
const UNORDERED_ITEM_PATTERN = /^\s*[-+*]\s+(.+)$/;
const ORDERED_ITEM_PATTERN = /^\s*\d+[.)]\s+(.+)$/;
const INLINE_CODE_MARK = String.fromCharCode(96);
const INLINE_PATTERN = new RegExp(
  INLINE_CODE_MARK + "[^" + INLINE_CODE_MARK + "\\n]+" + INLINE_CODE_MARK
    + "|\\*\\*[^*\\n]+\\*\\*"
    + "|__[^_\\n]+__"
    + "|\\*[^*\\n]+\\*"
    + "|_[^_\\n]+_",
  "g",
);

function unwrapOuterMarkdownFence(content: string): string {
  const normalized = content.replace(/\r\n?/g, "\n").trim();
  const opening = normalized.match(/^`{3,}[ \t]*(?:markdown|md)[ \t]*\n/i);
  const closing = normalized.match(/\n`{3,}[ \t]*$/);

  if (!opening || !closing || closing.index === undefined || closing.index < opening[0].length) {
    return content;
  }

  return normalized.slice(opening[0].length, closing.index).trim();
}

function splitTableCells(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}

function isTableSeparator(line: string): boolean {
  const cells = splitTableCells(line);
  return cells.length >= 2 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function isTableStart(lines: string[], index: number): boolean {
  return lines[index].includes("|")
    && index + 1 < lines.length
    && isTableSeparator(lines[index + 1]);
}

function isHorizontalRule(line: string): boolean {
  return /^(?:---+|\*\*\*+|___+)$/.test(line.trim());
}

function isQuote(line: string): boolean {
  return /^\s*>\s?/.test(line);
}

function renderInline(value: string): ReactNode[] {
  const breakParts=value.split(/<br\s*\/?\s*>/gi);
  if(breakParts.length>1)return breakParts.flatMap((part,index)=>[
    ...(index>0?[<br key={`inline-html-break-${index}`}/>]:[]),
    ...renderInline(part),
  ]);
  const nodes: ReactNode[] = [];
  let lastIndex = 0;

  for (const match of value.matchAll(INLINE_PATTERN)) {
    const token = match[0];
    const index = match.index ?? 0;
    if (index > lastIndex) nodes.push(value.slice(lastIndex, index));

    const key = "inline-" + index;
    if (token.startsWith(INLINE_CODE_MARK)) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("**") || token.startsWith("__")) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("*") || token.startsWith("_")) {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>);
    } else {
      nodes.push(token);
    }
    lastIndex = index + token.length;
  }

  if (lastIndex < value.length) nodes.push(value.slice(lastIndex));
  return nodes;
}

function renderLines(lines: string[], keyPrefix: string): ReactNode[] {
  return lines.flatMap((line, index) => [
    ...(index > 0 ? [<br key={keyPrefix + "-break-" + index} />] : []),
    ...renderInline(line),
  ]);
}

function renderTable(lines: string[], index: number): { node: ReactNode; nextIndex: number } {
  const header = splitTableCells(lines[index]);
  const rows: string[][] = [];
  let cursor = index + 2;

  while (
    cursor < lines.length
    && lines[cursor].trim()
    && lines[cursor].includes("|")
    && !isHorizontalRule(lines[cursor])
  ) {
    rows.push(splitTableCells(lines[cursor]));
    cursor += 1;
  }

  return {
    node: (
      <div className="message-table-scroll" key={"table-" + index}>
        <table>
          <thead>
            <tr>
              {header.map((cell, cellIndex) => (
                <th key={"table-head-" + index + "-" + cellIndex} scope="col">
                  {renderInline(cell)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={"table-row-" + index + "-" + rowIndex}>
                {header.map((_, cellIndex) => (
                  <td key={"table-cell-" + index + "-" + rowIndex + "-" + cellIndex}>
                    {renderInline(row[cellIndex] ?? "")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    ),
    nextIndex: cursor,
  };
}

function renderCodeBlock(lines: string[], index: number): { node: ReactNode; nextIndex: number } {
  const codeLines: string[] = [];
  let cursor = index + 1;

  while (cursor < lines.length && !FENCE_PATTERN.test(lines[cursor])) {
    codeLines.push(lines[cursor]);
    cursor += 1;
  }

  return {
    node: (
      <pre className="message-code-block" key={"code-" + index}>
        <code>{codeLines.join("\n")}</code>
      </pre>
    ),
    nextIndex: cursor < lines.length ? cursor + 1 : cursor,
  };
}

function startsBlock(lines: string[], index: number): boolean {
  const line = lines[index];
  return FENCE_PATTERN.test(line)
    || HEADING_PATTERN.test(line)
    || isTableStart(lines, index)
    || UNORDERED_ITEM_PATTERN.test(line)
    || ORDERED_ITEM_PATTERN.test(line)
    || isQuote(line)
    || isHorizontalRule(line);
}

function renderBlocks(content: string): ReactNode[] {
  const lines = content.split(/\r?\n/);
  const nodes: ReactNode[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    if (FENCE_PATTERN.test(line)) {
      const block = renderCodeBlock(lines, index);
      nodes.push(block.node);
      index = block.nextIndex;
      continue;
    }

    const heading = line.match(HEADING_PATTERN);
    if (heading) {
      const level = heading[1].length <= 2 ? 2 : 3;
      const Heading = level === 2 ? "h2" : "h3";
      nodes.push(
        <Heading key={"heading-" + index}>
          {renderInline(heading[2])}
        </Heading>,
      );
      index += 1;
      continue;
    }

    if (isTableStart(lines, index)) {
      const table = renderTable(lines, index);
      nodes.push(table.node);
      index = table.nextIndex;
      continue;
    }

    if (isHorizontalRule(line)) {
      nodes.push(<hr key={"rule-" + index} />);
      index += 1;
      continue;
    }

    const unordered = line.match(UNORDERED_ITEM_PATTERN);
    if (unordered) {
      const items: string[] = [];
      let cursor = index;
      while (cursor < lines.length) {
        const item = lines[cursor].match(UNORDERED_ITEM_PATTERN);
        if (!item) break;
        items.push(item[1]);
        cursor += 1;
      }
      nodes.push(
        <ul key={"unordered-" + index}>
          {items.map((item, itemIndex) => (
            <li key={"unordered-" + index + "-" + itemIndex}>{renderInline(item)}</li>
          ))}
        </ul>,
      );
      index = cursor;
      continue;
    }

    const ordered = line.match(ORDERED_ITEM_PATTERN);
    if (ordered) {
      const items: string[] = [];
      let cursor = index;
      while (cursor < lines.length) {
        const item = lines[cursor].match(ORDERED_ITEM_PATTERN);
        if (!item) break;
        items.push(item[1]);
        cursor += 1;
      }
      nodes.push(
        <ol key={"ordered-" + index}>
          {items.map((item, itemIndex) => (
            <li key={"ordered-" + index + "-" + itemIndex}>{renderInline(item)}</li>
          ))}
        </ol>,
      );
      index = cursor;
      continue;
    }

    if (isQuote(line)) {
      const quoteLines: string[] = [];
      let cursor = index;
      while (cursor < lines.length && isQuote(lines[cursor])) {
        quoteLines.push(lines[cursor].replace(/^\s*>\s?/, ""));
        cursor += 1;
      }
      nodes.push(
        <blockquote key={"quote-" + index}>
          {renderLines(quoteLines, "quote-" + index)}
        </blockquote>,
      );
      index = cursor;
      continue;
    }

    const paragraphLines = [line];
    let cursor = index + 1;
    while (cursor < lines.length && lines[cursor].trim() && !startsBlock(lines, cursor)) {
      paragraphLines.push(lines[cursor]);
      cursor += 1;
    }
    nodes.push(
      <p key={"paragraph-" + index}>
        {renderLines(paragraphLines, "paragraph-" + index)}
      </p>,
    );
    index = cursor;
  }

  return nodes;
}

export default function MarkdownMessage({ content, className }: MarkdownMessageProps) {
  const classes = className ? "message-markdown " + className : "message-markdown";
  return <div className={classes}>{renderBlocks(unwrapOuterMarkdownFence(content))}</div>;
}
