import { useEffect, useMemo, useState } from "react";
import type { AskAnswer, PendingAsk } from "../types";

interface AskCardProps {
  ask: PendingAsk;
  busy?: boolean;
  onSubmit: (answers: AskAnswer[]) => void | Promise<void>;
  onCancel: () => void;
}

type SelectionState = Record<string, string[]>;
type TextState = Record<string, string>;

export default function AskCard({ ask, busy = false, onSubmit, onCancel }: AskCardProps) {
  const [selected, setSelected] = useState<SelectionState>({});
  const [freeText, setFreeText] = useState<TextState>({});

  useEffect(() => {
    setSelected({});
    setFreeText({});
  }, [ask.id]);

  const answers = useMemo<AskAnswer[]>(
    () => ask.questions.map((question) => ({
      question_id: question.id,
      selected_options: selected[question.id] ?? [],
      free_text: freeText[question.id]?.trim() ?? "",
    })),
    [ask.questions, freeText, selected],
  );

  const complete = answers.every((answer) => answer.selected_options.length > 0 || answer.free_text.length > 0);

  function toggleOption(questionId: string, label: string, multiSelect: boolean) {
    setSelected((current) => {
      const existing = current[questionId] ?? [];
      if (!multiSelect) return { ...current, [questionId]: [label] };
      return {
        ...current,
        [questionId]: existing.includes(label)
          ? existing.filter((item) => item !== label)
          : [...existing, label],
      };
    });
  }

  return (
    <section className="ask-card" role="region" aria-label="等待你的回答">
      <div className="ask-card-heading">
        <div>
          <span className="eyebrow">NEED YOUR INPUT</span>
          <h3>先回答几个问题</h3>
          <p>选择或补充信息后，我会继续当前目标。</p>
        </div>
        <span className="ask-card-progress">{ask.questions.length} 个问题</span>
      </div>
      <p className="ask-card-hint">输入框暂时锁定；如果想开始新的目标，请先停止询问。</p>
      <div className="ask-question-list">
        {ask.questions.map((question) => {
          const current = selected[question.id] ?? [];
          return (
            <fieldset className="ask-question" key={question.id}>
              <legend><span>{question.header}</span>{question.multi_select && <small>可多选</small>}</legend>
              <p>{question.question}</p>
              {question.options.length > 0 && (
                <div className="ask-options" role="group" aria-label={question.header}>
                  {question.options.map((option) => {
                    const active = current.includes(option.label);
                    return (
                      <button
                        aria-pressed={active}
                        aria-label={`${question.header}：${option.label}`}
                        className={`ask-option${active ? " is-selected" : ""}`}
                        disabled={busy}
                        key={option.label}
                        type="button"
                        onClick={() => toggleOption(question.id, option.label, question.multi_select)}
                      >
                        <strong>{option.label}</strong>
                        {option.description && <small>{option.description}</small>}
                      </button>
                    );
                  })}
                </div>
              )}
              {question.allow_free_text && (
                <textarea
                  aria-label={`${question.header}：补充信息`}
                  className="ask-free-text"
                  disabled={busy}
                  placeholder="也可以直接补充说明"
                  rows={2}
                  value={freeText[question.id] ?? ""}
                  onChange={(event) => setFreeText((currentState) => ({ ...currentState, [question.id]: event.target.value }))}
                />
              )}
            </fieldset>
          );
        })}
      </div>
      <div className="ask-card-actions">
        <button className="button button-quiet" disabled={busy} type="button" onClick={onCancel}>停止询问</button>
        <button className="button button-primary" disabled={busy || !complete} type="button" onClick={() => void onSubmit(answers)}>
          {busy ? "正在提交…" : "提交回答"}
        </button>
      </div>
    </section>
  );
}
