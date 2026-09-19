import { useEffect, useState } from "react";
import { getResearchSources } from "../api";
import type { ResearchSource, ResearchTraceability } from "../types";

function hostOf(url: string | null): string {
  if (!url) return "";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function shortDate(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
}

function qualityLabel(score: number | null): string {
  return typeof score === "number" ? `质量 ${Math.round(score * 100)}%` : "";
}

export default function ResearchSourcesPanel({ jobId, traceability }: { jobId: string; traceability?: ResearchTraceability[] }) {
  const [sources, setSources] = useState<ResearchSource[]>([]);
  const [sourcesFailed, setSourcesFailed] = useState(false);
  useEffect(() => {
    let active = true;
    setSources([]);
    setSourcesFailed(false);
    getResearchSources(jobId)
      .then((response) => {
        if (active) setSources(response.sources);
      })
      .catch(() => {
        if (active) setSourcesFailed(true);
      });
    return () => {
      active = false;
    };
  }, [jobId]);
  const traced = (traceability ?? []).filter((item) => item.requirement || item.conclusion);
  if (!sources.length && !traced.length && !sourcesFailed) return null;
  return (
    <section className="research-sources" aria-label="来源与可追溯性">
      {sourcesFailed && sources.length === 0 && (
        <div className="research-sources-block">
          <h3>来源</h3>
          <p className="error-message" role="status">来源暂时无法加载</p>
        </div>
      )}
      {sources.length > 0 && (
        <div className="research-sources-block">
          <h3>来源 <span>{sources.length}</span></h3>
          <ol className="research-source-list">
            {sources.map((source) => (
              <li key={source.id}>
                <span className="research-source-ordinal" aria-hidden="true">{source.ordinal}</span>
                <div>
                  {source.canonical_url ? (
                    <a href={source.canonical_url} rel="noreferrer" target="_blank">{source.title || source.canonical_url}</a>
                  ) : (
                    <strong>{source.title || source.locator || "本地资料"}</strong>
                  )}
                  <small>
                    {[
                      hostOf(source.canonical_url),
                      source.published_at ? `发表 ${shortDate(source.published_at)}` : "",
                      `抓取 ${shortDate(source.retrieved_at)}`,
                      qualityLabel(source.quality_score),
                    ].filter(Boolean).join(" · ")}
                  </small>
                </div>
              </li>
            ))}
          </ol>
        </div>
      )}
      {traced.length > 0 && (
        <div className="research-sources-block">
          <h3>可追溯性 <span>{traced.filter((item) => item.supported).length}/{traced.length} 条要求有证据支撑</span></h3>
          <ul className="research-trace-list">
            {traced.map((item, index) => (
              <li key={`${item.requirement}-${index}`}>
                <div className="research-trace-head">
                  <strong>{item.requirement}</strong>
                  <em className={item.supported ? "trace-supported" : "trace-unsupported"}>{item.supported ? "有证据支撑" : "证据不足"}</em>
                </div>
                {item.conclusion && <p>{item.conclusion}</p>}
                <small>证据 {item.evidence_ids.length} 条 · 来源 {item.source_ids.length} 个</small>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
