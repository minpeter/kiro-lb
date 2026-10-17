export type ModelFamily = "auto" | "claude" | "gpt" | "glm" | "deepseek" | "kimi" | "minimax" | "qwen" | "other";

const PATTERNS: [ModelFamily, RegExp][] = [
  ["auto", /^auto(-kiro)?$/],
  ["claude", /claude/],
  ["gpt", /(^|[^a-z])(gpt|o[134])([^a-z]|$)|openai/],
  ["glm", /glm|zhipu/],
  ["deepseek", /deepseek/],
  ["kimi", /kimi|moonshot/],
  ["minimax", /minimax/],
  ["qwen", /qwen/],
];

export function modelFamily(model: string): ModelFamily {
  const name = model.trim().toLowerCase();
  return PATTERNS.find(([, re]) => re.test(name))?.[0] ?? "other";
}

const FAMILY_ORDER: ModelFamily[] = ["claude", "gpt", "deepseek", "kimi", "glm", "minimax", "qwen", "other", "auto"];

function versionKey(model: string): number[] {
  return (model.match(/\d+(?:\.\d+)*/g) ?? []).flatMap((part) => part.split(".").map(Number));
}

function compareVersionsDesc(a: string, b: string): number {
  const va = versionKey(a);
  const vb = versionKey(b);
  for (let i = 0; i < Math.max(va.length, vb.length); i++) {
    const diff = (vb[i] ?? -1) - (va[i] ?? -1);
    if (diff !== 0) return diff;
  }
  return a.localeCompare(b);
}

export function compareModels(a: string, b: string): number {
  const fa = FAMILY_ORDER.indexOf(modelFamily(a));
  const fb = FAMILY_ORDER.indexOf(modelFamily(b));
  return fa !== fb ? fa - fb : compareVersionsDesc(a, b);
}
