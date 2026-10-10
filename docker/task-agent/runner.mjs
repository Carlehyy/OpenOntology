/**
 * 任务实例 agent-runner — 容器内执行体（设计方案 §6）。
 *
 * 宿主以 bind mount 提供 /workspace：
 *   .ti-task.json     任务描述（system/prompt/max_turns）
 *   .steering/*.json  宿主落盘的插话（fs.watch 拉入运行中的开放输入流）
 *   .ti-output/output.json  本 runner/模型写入的最终产出
 *   output/*          产物文件（output.json 的 artifacts 列表引用）
 *
 * 执行体 = Claude Agent SDK（ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL 由容器
 * 环境注入，不落盘）。mid-turn 插话能力已被 R1 spike 实证。
 */
import { query } from "@anthropic-ai/claude-agent-sdk";
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, watch, writeFileSync } from "node:fs";

const WS = "/workspace";
const TASK = JSON.parse(readFileSync(`${WS}/.ti-task.json`, "utf8"));
mkdirSync(`${WS}/.ti-output`, { recursive: true });
mkdirSync(`${WS}/.steering`, { recursive: true });
mkdirSync(`${WS}/output`, { recursive: true });

class Pushable {
  constructor() { this.queue = []; this.wake = null; }
  push(value) {
    this.queue.push(value);
    if (this.wake) { const wake = this.wake; this.wake = null; wake(); }
  }
  [Symbol.asyncIterator]() {
    const self = this;
    return {
      async next() {
        while (!self.queue.length) {
          await new Promise((resolve) => { self.wake = resolve; });
        }
        return { value: self.queue.shift(), done: false };
      },
    };
  }
}

const pushable = new Pushable();
const uuid = () => crypto.randomUUID();
pushable.push({
  type: "user",
  message: { role: "user", content: TASK.prompt },
  uuid: uuid(),
});

// 插话：宿主写 .steering/<hash>.json → 移入 .steering/consumed/ 后注入
function drainSteering() {
  for (const file of readdirSync(`${WS}/.steering`)) {
    if (!file.endsWith(".json")) continue;
    const source = `${WS}/.steering/${file}`;
    const consumed = `${WS}/.steering/consumed-${file}`;
    try { renameSync(source, consumed); } catch { continue; }
    try {
      const payload = JSON.parse(readFileSync(consumed, "utf8"));
      pushable.push({
        type: "user",
        message: { role: "user", content: `[中途插话] ${payload.content}` },
        uuid: uuid(),
      });
      process.stdout.write(`[runner] steering folded: ${payload.id}\n`);
    } catch (error) {
      process.stderr.write(`[runner] bad steering ${file}: ${error}\n`);
    }
  }
}
mkdirSync(`${WS}/.steering`, { recursive: true });
watch(`${WS}/.steering`, () => setTimeout(drainSteering, 50));
drainSteering();

function extractJson(text) {
  if (!text) return null;
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/);
  const candidates = fenced ? [fenced[1]] : [];
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start >= 0 && end > start) candidates.push(text.slice(start, end + 1));
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === "object") return parsed;
    } catch { /* 尝试下一个候选 */ }
  }
  return null;
}

let lastAssistantText = "";
let isError = false;
try {
  const q = query({
    prompt: pushable,
    options: {
      cwd: WS,
      permissionMode: "bypassPermissions",
      allowedTools: ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "LS"],
      maxTurns: TASK.max_turns || 40,
      systemPrompt: TASK.system,
    },
  });
  for await (const message of q) {
    if (message.type === "assistant") {
      for (const block of message.message?.content || []) {
        if (block.type === "text" && block.text) lastAssistantText = block.text;
      }
    } else if (message.type === "result") {
      isError = Boolean(message.is_error);
      process.stdout.write(`[runner] result subtype=${message.subtype} turns=${message.num_turns}\n`);
    }
  }
} catch (error) {
  process.stderr.write(`[runner] query failed: ${error}\n`);
  process.exit(2);
}

// 产出落盘：模型未写 output.json 时，从最后的 assistant 文本提取 JSON 兜底
if (!existsSync(`${WS}/.ti-output/output.json`)) {
  const extracted = extractJson(lastAssistantText);
  if (!extracted) {
    process.stderr.write("[runner] no output.json and no JSON in final message\n");
    process.exit(3);
  }
  writeFileSync(`${WS}/.ti-output/output.json`,
                JSON.stringify(extracted, null, 2), "utf8");
}
process.exit(isError ? 4 : 0);
