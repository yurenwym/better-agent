import { useEffect, useRef } from "react";

type ToastAction = { label: string; onClick: () => void };

export default function AppToast({ message, tone = "success", action, durationMs, onDismiss }: { message: string; tone?: "success" | "error"; action?: ToastAction; durationMs?: number; onDismiss: () => void }) {
  const dismissRef = useRef(onDismiss);
  dismissRef.current = onDismiss;
  const effectiveDuration = durationMs ?? (tone === "error" ? 8000 : 4000);
  useEffect(() => {
    // effectiveDuration <= 0 keeps the toast until the user dismisses it or takes the action.
    if (effectiveDuration <= 0) return;
    const timer = window.setTimeout(() => dismissRef.current(), effectiveDuration);
    return () => window.clearTimeout(timer);
  }, [message, effectiveDuration]);
  return <div className={`app-toast app-toast-${tone}`} role={tone === "error" ? "alert" : "status"}><span aria-hidden="true" className="app-toast-icon">{tone === "success" ? "✓" : "!"}</span><span>{message}</span>{action && <button className="app-toast-action" type="button" onClick={() => { onDismiss(); action.onClick(); }}>{action.label}</button>}<button aria-label="关闭提示" type="button" onClick={onDismiss}>×</button></div>;
}
