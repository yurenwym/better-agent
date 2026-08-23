export function splitHumanBubbles(content: string): string[] {
  const parts = content.split(/\s*\[\[next\]\]\s*/g).map((item) => item.trim()).filter(Boolean);
  if (parts.length <= 3) return parts;
  return [parts[0], parts[1], parts.slice(2).join("\n\n")];
}
