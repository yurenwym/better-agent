import type { Run, Stats } from "../types";

interface StatsBarProps {
  stats?: Stats | null;
  run?: Run | null;
}

function valueOrUnavailable(value: number | null | undefined, format: (value: number) => string): string {
  return value === null || value === undefined ? "不可用" : format(value);
}

function seconds(value: number): string {
  return `${value.toFixed(1)}s`;
}

function integer(value: number): string {
  return Math.round(value).toLocaleString("zh-CN");
}

function tokens(value: number): string {
  return `${value >= 1000 ? `${(value / 1000).toFixed(1)}K` : integer(value)} tok`;
}

export default function StatsBar({ stats, run }: StatsBarProps) {
  const metrics = [
    ["交互", valueOrUnavailable(stats?.interactions, integer)],
    ["计划进度", stats?.plan_completed === null || stats?.plan_completed === undefined || stats?.plan_total === null || stats?.plan_total === undefined ? "不可用" : `${stats.plan_completed}/${stats.plan_total}`],
    ["LLM 请求", valueOrUnavailable(stats?.model_attempts, integer)],
    ["LLM 时间", valueOrUnavailable(stats?.model_seconds, seconds)],
    ["工具执行", valueOrUnavailable(stats?.tool_calls, integer)],
    ["工具时间", valueOrUnavailable(stats?.tool_seconds, seconds)],
    ["TTFT 平均", valueOrUnavailable(stats?.ttft_seconds, seconds)],
    ["TPS", valueOrUnavailable(stats?.tps, (value) => `${value.toFixed(1)} tok/s`)],
    ["缓存命中", valueOrUnavailable(stats?.cache_hit_rate, (value) => `${(value * 100).toFixed(0)}%`)],
    ["输入 Token", valueOrUnavailable(stats?.input_tokens, tokens)],
    ["输出 Token", valueOrUnavailable(stats?.output_tokens, tokens)],
    ["当前状态", stats?.state ?? run?.state ?? "不可用"],
  ] as const;

  return (
    <section className="stats-grid" aria-label="运行统计">
      {metrics.map(([label, value]) => (
        <div className="stat-cell" key={label}>
          <span className="stat-label">{label}</span>
          <strong className="stat-value">{value}</strong>
        </div>
      ))}
    </section>
  );
}
