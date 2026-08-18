import { FormEvent, useState } from "react";
import { presentMessage } from "../conversation";
import type { MessageRecord } from "../types";

interface ConversationThreadProps {
  messages: MessageRecord[];
  busy?: boolean;
  title?: string;
  description?: string;
  onSubmit: (content: string) => Promise<boolean | void> | boolean | void;
}

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

export default function ConversationThread({ messages, busy = false, title = "推动当前目标", description = "模型的每次返回都会留在这里，你可以直接根据它继续补充或调整。", onSubmit }: ConversationThreadProps) {
  const [draft, setDraft] = useState("");
  const [pendingUser, setPendingUser] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || busy) return;
    setPendingUser(content);
    const accepted = await onSubmit(content);
    if (accepted !== false) setDraft("");
    setPendingUser("");
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
                  <p className="message-summary">{view.summary}</p>
                  {view.detail && <p className="message-detail">{view.detail}</p>}
                  {view.bullets.length > 0 && <ul className="message-bullets">{view.bullets.map((bullet) => <li key={bullet}>{bullet}</li>)}</ul>}
                  {view.raw && (
                    <details className="model-raw">
                      <summary>查看模型原始结果</summary>
                      <pre>{view.raw}</pre>
                    </details>
                  )}
                </div>
              </div>
            </article>
          );
        })}
        {pendingUser && (
          <article className="message-row message-user message-pending-user">
            <div className="message-avatar" aria-hidden="true">YOU</div>
            <div className="message-body"><div className="message-meta"><strong>你</strong><span>发送中</span></div><div className="message-card"><p className="message-summary">{pendingUser}</p></div></div>
          </article>
        )}
        {busy && (
          <article className="message-row message-assistant message-pending" role="status">
            <div className="message-avatar" aria-hidden="true">BA</div>
            <div className="message-body">
              <div className="message-meta"><strong>Better Agent</strong><span>正在响应</span></div>
              <div className="message-card"><p className="message-summary">正在等待模型返回下一步结果…</p><span className="typing-indicator" aria-hidden="true"><i /><i /><i /></span></div>
            </div>
          </article>
        )}
      </div>

      <form className="conversation-composer" onSubmit={(event) => void submit(event)}>
        <label htmlFor="conversation-input">继续推动目标</label>
        <textarea
          id="conversation-input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="补充信息、回答模型的问题，或调整下一步……"
          rows={3}
        />
        <div className="composer-footer">
          <span>Enter 发送 · 你的回复会进入同一条 Run</span>
          <button className="button button-primary" disabled={busy || !draft.trim()} type="submit">发送</button>
        </div>
      </form>
    </section>
  );
}
