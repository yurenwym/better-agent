import { FormEvent, type KeyboardEvent as ReactKeyboardEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { presentMessage } from "../conversation";
import MarkdownMessage from "./MarkdownMessage";
import AnswerDuration from "./AnswerDuration";
import AskCard from "./AskCard";
import HumanBubbles from "./HumanBubbles";
import ResearchProgressCard from "./ResearchProgressCard";
import { CalendarPlus, FilePlus2 } from "lucide-react";
import type { AskAnswer, MessageRecord, PendingAsk, ResearchJob, SkillDefinition } from "../types";

interface ConversationThreadProps {
  embedded?:boolean;
  draftKey?: string;
  messages: MessageRecord[];
  busy?: boolean;
  title?: string;
  description?: string;
  composerDisabled?: boolean;
  cancelBusy?: boolean;
  cancelLabel?: string;
  onCancel?: () => void;
  pendingAsk?: PendingAsk | null;
  askBusy?: boolean;
  onAskAnswer?: (answers: AskAnswer[]) => void | Promise<void>;
  onAskCancel?: () => void;
  planReference?: { planDocumentId: string; versionId?: string; version: number; messageId: string; status?: "ready" | "failed" | "conflict" } | null;
  onOpenPlan?: (planDocumentId: string) => void;
  onArrangePlan?: (planDocumentId: string) => void;
  onSaveMessagePlan?: (messageId: string, title: string) => Promise<boolean>;
  researchJobs?: ResearchJob[];
  onCancelResearch?: (jobId: string) => void;
  onRetryResearch?: (jobId: string) => void;
  onOpenResearch?: (jobId: string) => void;
  skills?: SkillDefinition[];
  selectedSkills?: string[];
  onToggleSkill?: (name: string) => void;
  deepProcessing?: boolean;
  expertBusy?: boolean;
  onDeepProcessingChange?: (enabled: boolean) => void;
  expertPanel?: ReactNode;
  activeView?: "conversation" | "trajectory";
  onViewChange?: (view: "conversation" | "trajectory") => void;
  trajectoryPanel?: ReactNode;
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

const STARTER_PROMPTS = [
  "帮我制定一个可执行的学习计划",
  "帮我把一个长期目标拆成今天能做的行动",
  "分析我现在遇到的问题，并给出下一步建议",
  "规划一次旅行，并列出预算和注意事项",
];

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

const conversationDrafts = new Map<string, string>();
export function moveConversationDraft(from: string, to: string) {
  const value=conversationDrafts.get(from);
  if(value)conversationDrafts.set(to,value);
  conversationDrafts.delete(from);
}
export function clearConversationDraft(key: string) { conversationDrafts.delete(key); }

export default function ConversationThread({ embedded=false, draftKey, messages, busy = false, title = "推动当前目标", description = "模型的每次返回都会留在这里，你可以直接根据它继续补充或调整。", composerDisabled = false, cancelBusy = false, cancelLabel = "取消任务", onCancel, pendingAsk = null, askBusy = false, onAskAnswer, onAskCancel, planReference = null, onOpenPlan, onArrangePlan, onSaveMessagePlan, researchJobs = [], onCancelResearch, onRetryResearch, onOpenResearch, skills = [], selectedSkills = [], onToggleSkill = () => undefined, deepProcessing = false, expertBusy = false, onDeepProcessingChange, expertPanel, activeView = "conversation", onViewChange, trajectoryPanel, decision, onSubmit }: ConversationThreadProps) {
  const [planCandidate, setPlanCandidate] = useState<string | null>(null);
  const [planTitle, setPlanTitle] = useState("");
  const [planConfirmed, setPlanConfirmed] = useState(false);
  const [savingPlan, setSavingPlan] = useState(false);
  const saveLock = useRef(false);
  useEffect(() => { setPlanCandidate(null); setPlanTitle(""); setPlanConfirmed(false); }, [draftKey]);
  async function saveCandidate(messageId: string) {
    if (!onSaveMessagePlan || !planConfirmed || !planTitle.trim() || saveLock.current || busy) return;
    saveLock.current = true;
    setSavingPlan(true);
    try { if (await onSaveMessagePlan(messageId, planTitle.trim())) setPlanCandidate(null); }
    finally { saveLock.current = false; setSavingPlan(false); }
  }
  const latestMessage = messages.at(-1);
  const [draft, updateDraft] = useState(() => draftKey ? conversationDrafts.get(draftKey) ?? "" : "");
  const draftIdentity = useRef(draftKey);
  useEffect(() => { draftIdentity.current=draftKey; updateDraft(draftKey ? conversationDrafts.get(draftKey) ?? "" : ""); }, [draftKey]);
  function setDraft(value: string) {
    updateDraft(value);
    if(draftKey){if(value)conversationDrafts.set(draftKey,value);else conversationDrafts.delete(draftKey);}
  }
  const [pendingUser, setPendingUser] = useState("");
  const [skillsOpen, setSkillsOpen] = useState(false);
  const scrollContentRef = useRef<HTMLDivElement | null>(null);
  const bottomSentinelRef = useRef<HTMLDivElement | null>(null);
  const skillsPanelRef = useRef<HTMLDivElement | null>(null);
  const skillsTriggerRef = useRef<HTMLButtonElement | null>(null);
  const composerLocked = composerDisabled || Boolean(pendingAsk);

  useEffect(() => {
    const container = scrollContentRef.current;
    if (!container) return;
    let nearBottom = true;
    try {
      nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 120;
    } catch {
      return;
    }
    if (!nearBottom) return;
    try {
      bottomSentinelRef.current?.scrollIntoView({ block: "end" });
    } catch {
      // jsdom has no layout; auto-scroll is best-effort.
    }
  }, [messages, pendingUser, busy]);

  useEffect(() => {
    if (!skillsOpen) return;
    const closePanel = () => setSkillsOpen(false);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closePanel();
    };
    const onMouseDown = (event: MouseEvent) => {
      const target = event.target instanceof Node ? event.target : null;
      if (!target) return;
      if (skillsPanelRef.current?.contains(target)) return;
      if (skillsTriggerRef.current?.contains(target)) return;
      closePanel();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onMouseDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onMouseDown);
    };
  }, [skillsOpen]);

  async function submit() {
    const content = draft.trim();
    const submittedKey = draftKey;
    if (!content || busy || composerLocked) return;
    setPendingUser(content);
    const accepted = await onSubmit(content);
    if (accepted !== false) {
      if(submittedKey)conversationDrafts.delete(submittedKey);
      if(draftIdentity.current===submittedKey || draftIdentity.current && conversationDrafts.get(draftIdentity.current) === undefined)updateDraft("");
    }
    setPendingUser("");
  }

  function handleKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void submit();
  }

  function handleTabKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (!onViewChange || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const next = activeView === "conversation" ? "trajectory" : "conversation";
    onViewChange(next);
    document.getElementById(`conversation-${next}-tab`)?.focus();
  }

  return (
    <section className={`conversation-surface${messages.length === 0 && !pendingAsk ? " conversation-surface-empty" : ""}${pendingAsk ? " conversation-surface-asking" : ""}`} aria-label="当前目标对话">
      {!embedded?<div className="conversation-header">
        <div>
          <span className="eyebrow">实时对话</span>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
        <div className="conversation-header-meta">
          {trajectoryPanel && onViewChange && (
            <div className="conversation-view-tabs" role="tablist" aria-label="对话视图">
              <button
                aria-controls="conversation-panel"
                aria-selected={activeView === "conversation"}
                className={activeView === "conversation" ? "active" : ""}
                id="conversation-conversation-tab"
                role="tab"
                tabIndex={activeView === "conversation" ? 0 : -1}
                type="button"
                onClick={() => onViewChange("conversation")}
                onKeyDown={handleTabKeyDown}
              >事实对话</button>
              <button
                aria-controls="trajectory-panel"
                aria-selected={activeView === "trajectory"}
                className={activeView === "trajectory" ? "active" : ""}
                id="conversation-trajectory-tab"
                role="tab"
                tabIndex={activeView === "trajectory" ? 0 : -1}
                type="button"
                onClick={() => onViewChange("trajectory")}
                onKeyDown={handleTabKeyDown}
              >轨迹</button>
            </div>
          )}
          <span className="conversation-count">{messages.length} 条消息</span>
          {onCancel && !pendingAsk && <button className="button button-danger conversation-cancel" disabled={cancelBusy} type="button" onClick={onCancel}>{cancelBusy ? "正在停止…" : cancelLabel}</button>}
        </div>
      </div>:onCancel&&!pendingAsk?<div className="conversation-embedded-controls"><button className="button button-danger conversation-cancel" disabled={cancelBusy} type="button" onClick={onCancel}>{cancelBusy?"正在停止…":cancelLabel}</button></div>:null}

      {activeView === "conversation" ? <div className="conversation-content" id="conversation-panel" ref={scrollContentRef} role={trajectoryPanel ? "tabpanel" : undefined} aria-labelledby={trajectoryPanel ? "conversation-conversation-tab" : undefined}>
        <div className={`conversation-thread${messages.length === 0 ? " conversation-thread-empty" : ""}`} aria-live="polite" aria-label="消息列表">
        {messages.length === 0 && !pendingAsk && (
          <div className="conversation-empty">
            <span className="conversation-empty-mark" aria-hidden="true">BA</span>
            <h3>你想实现什么？</h3>
            <p>描述你想达成的结果、边界和优先级，模型会据此开始澄清和规划。</p>
            <div className="conversation-starters" aria-label="示例问题">
              <span>你可以这样问</span>
              <div>
                {STARTER_PROMPTS.map((prompt) => (
                  <button key={prompt} type="button" onClick={() => setDraft(prompt)}>{prompt}</button>
                ))}
              </div>
            </div>
          </div>
        )}
        {messages.map((message) => {
          const view = presentMessage(message);
          const assistant = message.role === "assistant";
          const avatar = <div className="message-avatar" aria-hidden="true">{assistant ? "BA" : "YOU"}</div>;
          const body = (
            <div className="message-body">
              <div className="message-meta">
                <strong>{assistant ? "Better Agent" : "你"}</strong>
                <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>
              </div>
              <div className="message-card">
                {message.presentation === "human_bubbles" && assistant
                  ? <HumanBubbles messageId={message.id} content={message.content} origin={message.origin} complete={!message.streaming} />
                  : <MarkdownMessage content={view.summary} className="message-summary" />}
                {view.detail && <MarkdownMessage content={view.detail} className="message-detail" />}
                {view.bullets.length > 0 && <ul className="message-bullets">{view.bullets.map((bullet) => <li key={bullet}>{bullet}</li>)}</ul>}
                {message.streaming && !(assistant && message.presentation === "human_bubbles") && (
                  <span aria-hidden="true" className="stream-cursor" />
                )}
              </div>
              {assistant && message.research_job_id && researchJobs.find((job) => job.id === message.research_job_id) && <ResearchProgressCard job={researchJobs.find((job) => job.id === message.research_job_id)!} onCancel={onCancelResearch ? () => onCancelResearch(message.research_job_id!) : undefined} onRetry={onRetryResearch ? () => onRetryResearch(message.research_job_id!) : undefined} onOpen={onOpenResearch ? () => onOpenResearch(message.research_job_id!) : undefined} />}
              {assistant && !message.streaming && <AnswerDuration milliseconds={message.total_ms} />}
              {assistant && planReference?.messageId === message.id && (
                <div className="plan-reference-card" role="status">
                  {planReference.status === "failed" || planReference.status === "conflict" ? (
                    <div><strong>计划文件写入未完成 · v{planReference.version}</strong><span>计划地址已保留，可打开计划页重试写入</span></div>
                  ) : (
                    <div><strong>已保存到计划 · v{planReference.version}</strong><span>这份回答已作为可编辑 Markdown 版本保存</span></div>
                  )}
                  <button className="button button-secondary" type="button" onClick={() => onOpenPlan?.(planReference.planDocumentId)}>{planReference.status === "failed" || planReference.status === "conflict" ? "打开并重试" : "查看 / 编辑计划"}</button>
                  {onArrangePlan && (!planReference.status || planReference.status === "ready") && <button className="button button-primary" type="button" disabled={busy} onClick={() => onArrangePlan(planReference.planDocumentId)}><CalendarPlus size={16} aria-hidden="true" />安排日程</button>}
                </div>
              )}
              {onSaveMessagePlan && assistant && latestMessage?.id === message.id && !message.streaming && (!message.status || message.status === "ready") && message.presentation !== "human_bubbles" && !message.research_job_id && !message.plan_document_version_id && planReference?.messageId !== message.id && message.content.trim() && (
                planCandidate === message.id ? <section className="plan-reference-card" aria-label="确认计划正文">
                  <form onSubmit={event => { event.preventDefault(); void saveCandidate(message.id); }}>
                    <label>计划名称<input maxLength={200} required disabled={savingPlan} value={planTitle} onChange={event => setPlanTitle(event.target.value)} /></label>
                    <details><summary>核对要保存的正文</summary><MarkdownMessage content={message.content} /></details>
                    <label><input type="checkbox" checked={planConfirmed} disabled={savingPlan} onChange={event => setPlanConfirmed(event.target.checked)} />确认将这条回答全文作为计划正文</label>
                    <div className="button-row"><button className="button button-secondary" type="button" disabled={savingPlan} onClick={() => setPlanCandidate(null)}>取消</button><button className="button button-primary" type="submit" disabled={busy || savingPlan || !planConfirmed || !planTitle.trim()}><CalendarPlus size={16} aria-hidden="true" />{savingPlan ? "正在保存…" : "保存并安排日程"}</button></div>
                  </form>
                </section> : <button className="button button-quiet" type="button" disabled={busy || Boolean(pendingAsk)} onClick={() => { setPlanCandidate(message.id); setPlanTitle(""); setPlanConfirmed(false); }}><FilePlus2 size={16} aria-hidden="true" />将此回答设为计划</button>
              )}
            </div>
          );
          return (
            <article aria-busy={message.streaming ? true : undefined} className={`message-row message-${message.role}`} key={message.id}>
              {assistant ? <>{avatar}{body}</> : <>{body}{avatar}</>}
            </article>
          );
        })}
        {pendingUser && !messages.some(message => message.role === "user" && message.content === pendingUser) && (
          <article className="message-row message-user message-pending-user">
            <div className="message-body"><div className="message-meta"><strong>你</strong><span>发送中</span></div><div className="message-card"><MarkdownMessage content={pendingUser} className="message-summary" /></div></div>
            <div className="message-avatar" aria-hidden="true">你</div>
          </article>
        )}
        {busy && !pendingAsk && !messages.some((message) => message.role === "assistant" && message.streaming) && (
          <article className="message-row message-assistant message-pending" role="status">
            <div className="message-avatar" aria-hidden="true">BA</div>
            <div className="message-body">
              <div className="message-meta"><strong>Better Agent</strong><span>正在响应</span></div>
              <div className="message-card"><MarkdownMessage content="正在等待模型返回下一步结果…" className="message-summary" /><span className="typing-indicator" aria-hidden="true"><i /><i /><i /></span></div>
            </div>
          </article>
        )}
        {expertPanel && <div className="conversation-expert-panel">{expertPanel}</div>}
        {pendingAsk && onAskAnswer && onAskCancel && (
          <AskCard key={pendingAsk.id} ask={pendingAsk} busy={askBusy} onSubmit={onAskAnswer} onCancel={onAskCancel} />
        )}

        {decision && (
          <div className="conversation-decision" role="region" aria-label={decision.title}>
            <div>
              <span className="eyebrow">下一步决定</span>
              <strong>{decision.title}</strong>
              <p>{decision.description}</p>
            </div>
            <div className="button-row">
              <button className="button button-secondary" disabled={busy || decision.busy} type="button" onClick={decision.onSecondary}>{decision.secondaryLabel}</button>
              <button className="button button-primary" disabled={busy || decision.busy} type="button" onClick={decision.onPrimary}>{decision.busy ? "正在应用…" : decision.primaryLabel}</button>
            </div>
          </div>
        )}
        <div ref={bottomSentinelRef} aria-hidden="true" className="conversation-scroll-sentinel" />
        </div>
      </div> : <div className="conversation-content conversation-trajectory-panel" id="trajectory-panel" role="tabpanel" aria-labelledby="conversation-trajectory-tab">{trajectoryPanel}</div>}

      <form className="conversation-composer" onSubmit={(event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void submit(); }}>
        <label className="sr-only" htmlFor="conversation-input">输入消息</label>
        <textarea
          id="conversation-input"
          disabled={composerLocked}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={pendingAsk ? "请先回答上方问题，或停止询问后开始新的目标" : "输入信息，Enter发送，Shift+Enter换行"}
          rows={3}
        />
        <div className="composer-footer">
          <div className="composer-tools">
            <div className="composer-skill-menu">
              <button
                aria-controls="conversation-skills"
                aria-expanded={skillsOpen}
                className="button button-secondary composer-skill-trigger"
                disabled={composerLocked}
                ref={skillsTriggerRef}
                type="button"
                onClick={() => setSkillsOpen((open) => !open)}
              >
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m12 3 1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7L12 3Z" /><path d="m19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7L19 16Z" /></svg>
                <span>技能</span>
                {selectedSkills.length > 0 && <span className="composer-skill-count">{selectedSkills.length}</span>}
              </button>
              {skillsOpen && (
                <div className="composer-skills-panel" id="conversation-skills" ref={skillsPanelRef} role="dialog" aria-label="当前对话技能">
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
                            disabled={!skill.enabled || composerLocked}
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
            {onDeepProcessingChange && (
              <label className="composer-expert-toggle">
                <input
                  aria-label="深入处理"
                  checked={deepProcessing}
                  disabled={expertBusy || composerLocked}
                  role="switch"
                  type="checkbox"
                  onChange={(event) => onDeepProcessingChange(event.target.checked)}
                />
                <span aria-hidden="true" />
                <strong>深入处理</strong>
              </label>
            )}
          </div>
          <button className="button button-primary composer-submit" disabled={busy || expertBusy || composerLocked || !draft.trim()} type="submit">
            {expertBusy ? "正在启动…" : deepProcessing ? "启动专家协同" : "发送"}
          </button>
        </div>
      </form>
    </section>
  );
}
