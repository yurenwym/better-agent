import { useEffect, useRef } from "react";

export default function AppToast({ message, tone = "success", onDismiss }: { message: string; tone?: "success" | "error"; onDismiss: () => void }) {
  const dismissRef = useRef(onDismiss);
  dismissRef.current = onDismiss;
  useEffect(() => {
    const timer = window.setTimeout(() => dismissRef.current(), 4000);
    return () => window.clearTimeout(timer);
  }, [message]);
  return <div className={`app-toast app-toast-${tone}`} role={tone === "error" ? "alert" : "status"}><span aria-hidden="true" className="app-toast-icon">{tone === "success" ? "✓" : "!"}</span><span>{message}</span><button aria-label="关闭提示" type="button" onClick={onDismiss}>×</button></div>;
}
