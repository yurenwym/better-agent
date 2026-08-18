import { FormEvent, KeyboardEvent, useState } from "react";
import { presentMessage } from "../conversation";
import MarkdownMessage from "./MarkdownMessage";
import type { MessageRecord, SkillDefinition } from "../types";

interface ConversationThreadProps {
  messages: MessageRecord[];
  busy?: boolean;
  title?: string;
  description?: string;
  composerDisabled?: boolean;
  composerHint?: string;
  skills?: SkillDefinition[];
  selectedSkills?: string[];
  onToggleSkill?: (name: string) => void;
  decision?: {
    title: string;
    description: string;
    primaryLabel: string;
    secondaryLabel: string;
    busy?: boolean;
    onPrimary: () => void;
    onSecondary: () => void;
  };
  onSubmit: (content: string) => Promise<boolean | void> | boolean | void;
}

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

export default function ConversationThread({ messages, busy = false, title = "推动当前目标", description = "模型的每次返回都会留在这里，你可以直接根据它继续补充或调整。", composerDisabled = false, composerHint = "输入信息，Enter发送，Shift+Enter换行", skills = [], selectedSkills = [], onToggleSkill = () => undefined, decision, onSubmit }: ConversationThreadProps) {
  const [draft, setDraft] = useState("");
  const [pendingUser, setPendingUser] = useState("");
  const [skillsOpen, setSkillsOpen] = useState(false);

  async function submit() {
    const content = draft.trim();
    if (!content || busy || composerDisabled) return;
    setPendingUser(content);
    const accepted = await onSubmit(content);
    if (accepted !== false) setDraft("");
    setPendingUser("");
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void submit();
  }

  return (
    <section className="conversation-surface" aria-label="当前目标对话">
      <div className="conversation-header">
        <div>
          <span className="eyebrow">CONVERSATION / LIVE</span>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
        <span className="conversation-count">{messages.length} 条消息</span>
      </div>

      <div className="conversation-thread" aria-live="polite" aria-label="消息列表">
        {messages.length === 0 && (
          <div className="conversation-empty">
            <span className="conversation-empty-mark" aria-hidden="true">BA</span>
            <h3>把下一步交给对话</h3>
            <p>描述你想达成的结果、边界和优先级。模型回复后，你可以在同一处继续推动。</p>
          </div>
        )}
        {messages.map((message) => {
          const view = presentMessage(message);
          const assistant = message.role === "assistant";
          return (
            <article className={`message-row message-${message.role}`} key={message.id}>
              <div className="message-avatar" aria-hidden="true">{assistant ? "BA" : "YOU"}</div>
              <div className="message-body">
                <div className="message-meta">
                  <strong>{assistant ? "Better Agent" : "你"}</strong>
                  <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>
                </div>
                <div className="message-card">
                  <MarkdownMessage content={view.summary} className="message-summary" />
                  {view.detail && <MarkdownMessage content={view.detail} className="message-detail" />}
                  {view.bullets.length > 0 && <ul className="message-bullets">{view.bullets.map((bullet) => <li key={bullet}>{bullet}</li>)}</ul>}
                </div>
              </div>
            </article>
          );
        })}
        {pendingUser && (
          <article className="message-row message-user message-pending-user">
            <div className="message-avatar" aria-hidden="true">YOU</div>
            <div className="message-body"><div className="message-meta"><strong>你</strong><span>发送中</span></div><div className="message-card"><MarkdownMessage content={pendingUser} className="message-summary" /></div></div>
          </article>
        )}
        {busy && (
          <article className="message-row message-assistant message-pending" role="status">
            <div className="message-avatar" aria-hidden="true">BA</div>
            <div className="message-body">
              <div className="message-meta"><strong>Better Agent</strong><span>正在响应</span></div>
              <div className="message-card"><MarkdownMessage content="正在等待模型返回下一步结果…" className="message-summary" /><span className="typing-indicator" aria-hidden="true"><i /><i /><i /></span></div>
            </div>
          </article>
        )}
      </div>

      {decision && (
        <div className="conversation-decision" role="region" aria-label={decision.title}>
          <div>
            <span className="eyebrow">NEXT DECISION</span>
            <strong>{decision.title}</strong>
            <p>{decision.description}</p>
          </div>
          <div className="button-row">
            <button className="button button-secondary" disabled={busy || decision.busy} type="button" onClick={decision.onSecondary}>{decision.secondaryLabel}</button>
            <button className="button button-primary" disabled={busy || decision.busy} type="button" onClick={decision.onPrimary}>{decision.busy ? "正在应用…" : decision.primaryLabel}</button>
          </div>
        </div>
      )}

      <form className="conversation-composer" onSubmit={(event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void submit(); }}>
        <label className="sr-only" htmlFor="conversation-input">输入消息</label>
        <textarea
          id="conversation-input"
          disabled={composerDisabled}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入信息，Enter发送，Shift+Enter换行"
          rows={3}
        />
        <div className="composer-footer">
          <div className="composer-tools">
            <div className="composer-skill-menu">
              <button
                aria-controls="conversation-skills"
                aria-expanded={skillsOpen}
                className="button button-secondary composer-skill-trigger"
                disabled={composerDisabled}
                type="button"
                onClick={() => setSkillsOpen((open) => !open)}
              >
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m12 3 1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7L12 3Z" /><path d="m19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7L19 16Z" /></svg>
                <span>技能</span>
                {selectedSkills.length > 0 && <span className="composer-skill-count">{selectedSkills.length}</span>}
              </button>
              {skillsOpen && (
                <div className="composer-skills-panel" id="conversation-skills" role="dialog" aria-label="当前对话技能">
                  <div className="composer-skills-heading">
                    <div><strong>已安装的 Skill</strong><span>仅作用于当前对话</span></div>
                    <button className="composer-skills-close" type="button" aria-label="关闭技能选择" onClick={() => setSkillsOpen(false)}>×</button>
                  </div>
                  {skills.length === 0 ? (
                    <p className="composer-skills-empty">暂无已安装 Skill</p>
                  ) : (
                    <div className="composer-skills-list" role="group" aria-label="已安装 Skill">
                      {skills.map((skill) => (
                        <label className="composer-skill-option" key={skill.name}>
                          <input
                            aria-describedby={`skill-description-${skill.name}`}
                            aria-label={skill.title}
                            type="checkbox"
                            checked={selectedSkills.includes(skill.name)}
                            disabled={!skill.enabled || composerDisabled}
                            onChange={() => onToggleSkill(skill.name)}
                          />
                          <span><strong>{skill.title}</strong><small id={`skill-description-${skill.name}`}>{skill.description}</small></span>
                        </label>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
            <span className="composer-hint">{composerHint}</span>
          </div>
          <button className="button button-primary" disabled={busy || composerDisabled || !draft.trim()} type="submit">发送</button>
        </div>
      </form>
    </section>
  );
}
