export default function AnswerDuration({ milliseconds }: { milliseconds?: number | null }) {
  if (milliseconds == null || !Number.isFinite(milliseconds) || milliseconds < 0) return null;
  const seconds = milliseconds / 1000;
  const value = seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${Math.floor(seconds / 60)} 分 ${(seconds % 60).toFixed(1)} 秒`;
  return <div className="answer-duration">总耗时：{value}</div>;
}
