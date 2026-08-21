import { useEffect, useRef, useState, type FormEvent } from "react";
import { parseMarkdownForEditor, serializeEditorBlocks, type EditorBlock } from "../planEditor";

interface PlanVisualEditorProps {
  title?: string;
  onTitleChange?: (title: string) => void;
  markdown: string;
  onChange: (markdown: string) => void;
  disabled?: boolean;
}

function editableProps(
  label: string | undefined,
  disabled: boolean,
  onInput: (text: string) => void,
): {
  "aria-label"?: string;
  contentEditable: boolean;
  role?: "textbox";
  suppressContentEditableWarning: boolean;
  onInput: (event: FormEvent<HTMLElement>) => void;
} {
  return {
    ...(label ? { "aria-label": label } : {}),
    contentEditable: !disabled,
    ...(label ? { role: "textbox" as const } : {}),
    suppressContentEditableWarning: true,
    onInput: (event) => onInput(event.currentTarget.textContent ?? ""),
  };
}

function PlanVisualEditor({ title, onTitleChange, markdown, onChange, disabled = false }: PlanVisualEditorProps) {
  const [blocks, setBlocks] = useState<EditorBlock[]>(() => parseMarkdownForEditor(markdown));
  const lastEmittedMarkdown = useRef(markdown);

  useEffect(() => {
    if (markdown === lastEmittedMarkdown.current) return;
    lastEmittedMarkdown.current = markdown;
    setBlocks(parseMarkdownForEditor(markdown));
  }, [markdown]);

  function emit(nextBlocks: EditorBlock[]) {
    const nextMarkdown = serializeEditorBlocks(nextBlocks);
    lastEmittedMarkdown.current = nextMarkdown;
    setBlocks(nextBlocks);
    onChange(nextMarkdown);
  }

  function updateBlock(index: number, update: (block: EditorBlock) => EditorBlock) {
    emit(blocks.map((block, blockIndex) => blockIndex === index ? update(block) : block));
  }

  function updateText(index: number, text: string) {
    updateBlock(index, (block) => {
      if ("text" in block) return { ...block, text } as EditorBlock;
      return block;
    });
  }

  function updateListItem(index: number, itemIndex: number, text: string) {
    updateBlock(index, (block) => {
      if (block.type !== "bullet-list" && block.type !== "ordered-list") return block;
      return { ...block, items: block.items.map((item, currentIndex) => currentIndex === itemIndex ? text : item) };
    });
  }

  function updateTableCell(index: number, rowIndex: number, cellIndex: number, text: string) {
    updateBlock(index, (block) => {
      if (block.type !== "table") return block;
      if (rowIndex === -1) {
        return { ...block, headers: block.headers.map((header, currentIndex) => currentIndex === cellIndex ? text : header) };
      }
      return {
        ...block,
        rows: block.rows.map((row, currentRow) => currentRow === rowIndex
          ? row.map((cell, currentCell) => currentCell === cellIndex ? text : cell)
          : row),
      };
    });
  }

  function addParagraph() {
    emit([...blocks, { id: `block-${Date.now()}`, type: "paragraph", text: "" }]);
  }

  function renderBlock(block: EditorBlock, index: number) {
    let content;
    switch (block.type) {
      case "heading": {
        const Heading = `h${block.level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
        content = <Heading {...editableProps(undefined, disabled, (text) => updateText(index, text))}>{block.text}</Heading>;
        break;
      }
      case "paragraph":
        content = <p {...editableProps(`段落 ${index + 1}`, disabled, (text) => updateText(index, text))}>{block.text}</p>;
        break;
      case "bullet-list":
        content = (
          <ul>
            {block.items.map((item, itemIndex) => (
              <li key={`${block.id}-${itemIndex}`}>
                <span {...editableProps(`列表项 ${itemIndex + 1}`, disabled, (text) => updateListItem(index, itemIndex, text))}>{item}</span>
              </li>
            ))}
          </ul>
        );
        break;
      case "ordered-list":
        content = (
          <ol>
            {block.items.map((item, itemIndex) => (
              <li key={`${block.id}-${itemIndex}`}>
                <span {...editableProps(`列表项 ${itemIndex + 1}`, disabled, (text) => updateListItem(index, itemIndex, text))}>{item}</span>
              </li>
            ))}
          </ol>
        );
        break;
      case "blockquote":
        content = <blockquote {...editableProps(`引用 ${index + 1}`, disabled, (text) => updateText(index, text))}>{block.text}</blockquote>;
        break;
      case "table":
        content = (
          <div className="plan-editor-table-wrap">
            <table className="plan-editor-table">
              <thead>
                <tr>
                  {block.headers.map((header, cellIndex) => (
                    <th key={`${block.id}-header-${cellIndex}`} {...editableProps(undefined, disabled, (text) => updateTableCell(index, -1, cellIndex, text))}>{header}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, rowIndex) => (
                  <tr key={`${block.id}-row-${rowIndex}`}>
                    {row.map((cell, cellIndex) => (
                      <td key={`${block.id}-${rowIndex}-${cellIndex}`} {...editableProps(undefined, disabled, (text) => updateTableCell(index, rowIndex, cellIndex, text))}>{cell}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
        break;
      case "code":
        content = <pre {...editableProps("代码块", disabled, (text) => updateText(index, text))}>{block.text}</pre>;
        break;
      case "rule":
        content = <hr />;
        break;
      case "raw":
        content = <p className="plan-editor-raw" {...editableProps(`未识别内容 ${index + 1}`, disabled, (text) => updateText(index, text))}>{block.text}</p>;
        break;
    }

    return <article className={`plan-editor-block plan-editor-block-${block.type}`} key={block.id}>{content}</article>;
  }

  return (
    <section className="plan-visual-editor" aria-label="可视化计划编辑器">
      {title !== undefined && onTitleChange && (
        <div className="plan-visual-editor-title">
          <label className="field-label" htmlFor="visual-plan-title">计划名称</label>
          <input id="visual-plan-title" aria-label="计划名称" maxLength={120} value={title} disabled={disabled} onChange={(event) => onTitleChange(event.target.value)} />
        </div>
      )}
      <div className="plan-visual-editor-toolbar" role="toolbar" aria-label="计划编辑工具">
        <span className="muted">直接点击文字编辑计划内容</span>
        <button className="button button-secondary" disabled={disabled} type="button" onClick={addParagraph}>添加段落</button>
      </div>
      {blocks.length === 0 ? <p className="plan-editor-empty">计划内容为空，点击“添加段落”开始编辑。</p> : blocks.map(renderBlock)}
    </section>
  );
}

export default PlanVisualEditor;
