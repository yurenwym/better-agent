import { useEffect, useMemo, useState } from "react";
import { splitHumanBubbles } from "../humanBubbles";
import MarkdownMessage from "./MarkdownMessage";

interface Props { messageId: string; content: string; origin?: "history" | "live"; complete: boolean; }

export default function HumanBubbles({ messageId, content, origin = "history", complete }: Props) {
  const parts = useMemo(() => splitHumanBubbles(content), [content]);
  const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const [visible, setVisible] = useState(origin === "history" || reduced ? 3 : 0);
  useEffect(() => {
    if (origin === "history" || reduced) { setVisible(3); return; }
    const available = complete ? parts.length : Math.max(parts.length - 1, 0);
    if (visible >= available) return;
    const next = parts[visible] ?? "";
    const delay = visible === 0 ? 750 : Math.min(700 + next.length * 20, 3000);
    const timer = window.setTimeout(() => setVisible((value) => value + 1), delay);
    return () => window.clearTimeout(timer);
  }, [complete, origin, parts, reduced, visible]);
  const shown = parts.slice(0, visible);
  return <div className="human-bubbles">{shown.map((part, index) => <div className="human-bubble" key={`${messageId}-${index}`}><MarkdownMessage content={part} /></div>)}{visible < (complete ? parts.length : Math.max(parts.length - 1, 0)) && <span className="human-typing" role="status" aria-label="Better Agent 正在输入"><i /><i /><i /></span>}</div>;
}
