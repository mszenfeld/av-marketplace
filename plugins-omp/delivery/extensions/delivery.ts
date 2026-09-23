/**
 * Delivery extension: plan mode → delivery.
 *
 * - Plan mode in a git repository: appends the delivery task format to the system prompt.
 * - `write xd://propose`: rejects a plan whose `### Task` blocks the router cannot route.
 * - Plan approval (the interactive "Plan approved." prompt): when the plan has `### Task`
 *   headings, adds a message that hands it to the Delivery run of skill://delivery:orchestration.
 */
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const ROUTER = path.join(import.meta.dir, "..", "scripts", "route_task.py");
const TASK_HEADING = /^### Task \d+:\s*.+/;
const FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
const PROPOSE_TARGET = /^xd:\/\/propose\/?$/;
const APPROVED_PLAN_OPEN = /<plan path="(local:\/\/[^"]+)">\n/;

const PLAN_FORMAT = `# Delivery plans

This session has the delivery plugin. An approved plan that changes files in this git repository is delivered task by task: each \`### Task\` goes to the developer agent that owns its files and is reviewed and committed on its own; then the plan's \`## Verification\` runs and a full code review closes the delivery. Write such a plan's Approach as numbered tasks in exactly this shape:

### Task 1: <short title>
**Commit:** <conventional commit subject>

**Files:**
- Create: \`path/to/new_module.py\`
- Modify: \`path/to/existing.py\`
- Test: \`tests/test_new_module.py\`
- Delete: \`path/to/obsolete.py\`

<concrete steps; when behavior changes, the failing test comes first>

Rules:
- Every file change belongs to a task. Text outside tasks is context; nobody implements it.
- Number tasks 1, 2, 3… in execution order, each number once; producers before consumers.
- One stack per task: Python, React/TypeScript frontend, PHP, or everything else (docs, CI, configuration). A change that spans backend and frontend is two or more tasks.
- List every file the task creates, modifies, tests or deletes: repository-relative, in backticks. The file list picks the implementing agent.
- Each task stands alone: its agent implements only that task. Name the functions, types and signatures that later tasks rely on.
- Inside a task use no \`##\` or \`###\` headings outside fenced code blocks; the next one ends the task.
- Writing the slug to xd://propose checks these rules and lists every violation.
- A plan that changes no files (research, analysis, an answer) has no \`### Task\` headings and runs without delivery.`;

function run(command: string, args: string[], timeout: number): Promise<string> {
	const { promise, resolve, reject } = Promise.withResolvers<string>();
	execFile(command, args, { timeout, encoding: "utf8" }, (error, stdout) => (error ? reject(error) : resolve(stdout)));
	return promise;
}

async function gitRoot(cwd: string): Promise<string | undefined> {
	try {
		return (await run("git", ["-C", cwd, "rev-parse", "--show-toplevel"], 5_000)).trim() || undefined;
	} catch {
		return undefined;
	}
}

function inPlanMode(ctx: ExtensionContext): boolean {
	const branch = ctx.sessionManager.getBranch();
	for (let i = branch.length - 1; i >= 0; i--) {
		const entry = branch[i];
		if (entry.type === "mode_change") return entry.mode === "plan";
	}
	return false;
}

/** On-disk path of a `local://` URL under the session's local root (mirrors OMP's local-protocol.ts). */
function localPath(url: string, ctx: ExtensionContext): string | undefined {
	const rel = url.replace(/^local:\/+/, "");
	if (!rel || path.isAbsolute(rel) || rel.split("/").includes("..")) return undefined;
	const artifacts = ctx.localProtocolOptions?.getArtifactsDir?.() ?? ctx.sessionManager.getArtifactsDir();
	const sessionId = (ctx.sessionManager.getSessionId() || "session").replace(/[^a-zA-Z0-9_.-]/g, "_");
	const root = artifacts ? path.resolve(artifacts, "local") : path.join(os.tmpdir(), "omp-local", sessionId);
	return path.resolve(root, rel);
}

/** `local://<slug>-plan.md` for a title written to xd://propose (mirrors OMP's approved-plan.ts normalization). */
function proposedPlanUrl(title: string): string | undefined {
	const trimmed = title.trim();
	if (!trimmed || /[\\/]/.test(trimmed) || trimmed.includes("..")) return undefined;
	const slug = trimmed
		.replace(/\.md$/i, "")
		.replace(/\s+/g, "-")
		.replace(/[^A-Za-z0-9_-]/g, "")
		.replace(/-{2,}/g, "-")
		.replace(/^-+|-+$/g, "");
	if (!slug) return undefined;
	return `local://${slug.replace(/-plan$/i, "") || slug}-plan.md`;
}

/** The approved plan's URL and task count, when the prompt is OMP's interactive plan-approved prompt. */
function approvedPlan(prompt: string): { url: string; tasks: number } | undefined {
	if (!prompt.startsWith("Plan approved.")) return undefined;
	const open = APPROVED_PLAN_OPEN.exec(prompt);
	if (!open) return undefined;
	const start = open.index + open[0].length;
	const end = prompt.lastIndexOf("\n</plan>");
	if (end < start) return undefined;
	// `### Task` headings outside fenced code blocks, as route_task.py parse_plan counts them.
	let fence: string | undefined;
	let tasks = 0;
	for (const line of prompt.slice(start, end).split("\n")) {
		const marker = FENCE.exec(line);
		if (marker && !fence) fence = marker[1];
		else if (marker && fence && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = undefined;
		else if (!fence && TASK_HEADING.test(line)) tasks++;
	}
	return tasks > 0 ? { url: open[1], tasks } : undefined;
}

export default function deliveryExtension(pi: ExtensionAPI): void {
	pi.on("before_agent_start", async (event, ctx) => {
		if (inPlanMode(ctx)) {
			if (!(await gitRoot(ctx.cwd))) return undefined;
			return { systemPrompt: [...event.systemPrompt, PLAN_FORMAT] };
		}
		const plan = approvedPlan(event.prompt);
		if (!plan) return undefined;
		if (!(await gitRoot(ctx.cwd))) {
			ctx.ui.notify("Delivery skipped: not a git repository. The plan runs without delivery.", "warning");
			return undefined;
		}
		const file = localPath(plan.url, ctx);
		const planFile = file && existsSync(file) ? file : undefined;
		ctx.ui.notify(`Delivery: ${plan.tasks} task(s) — starting the delivery run.`, "info");
		return {
			message: {
				customType: "delivery-run",
				content: [
					"<critical>",
					`Delivery takes over this approved plan: it has ${plan.tasks} task(s) under \`### Task\` headings. This replaces the instruction above to execute the plan step by step yourself. Implementing this plan means running the delivery: each task goes to the developer agent that owns its files, is reviewed and committed; then the plan's Verification and the full code review run. Do not edit project files yourself.`,
					"",
					"`read skill://delivery:orchestration` and run its **Delivery run** section with:",
					`- PLAN_SOURCE: ${plan.url}`,
					...(planFile ? [`- PLAN_FILE: ${planFile}`] : []),
					"</critical>",
				].join("\n"),
				display: true,
				attribution: "agent",
			},
		};
	});

	pi.on("tool_call", async (event, ctx) => {
		if (event.toolName !== "write") return undefined;
		const { path: target, content } = event.input as { path?: unknown; content?: unknown };
		if (typeof target !== "string" || typeof content !== "string" || !PROPOSE_TARGET.test(target.trim())) return undefined;
		const url = proposedPlanUrl(content);
		const file = url ? localPath(url, ctx) : undefined;
		if (!url || !file || !existsSync(file)) return undefined;
		const root = await gitRoot(ctx.cwd);
		if (!root) return undefined;
		let result: { tasks: number; problems: string[] };
		try {
			result = JSON.parse(await run("python3", [ROUTER, "check", root, file], 10_000));
		} catch {
			return undefined;
		}
		if (result.tasks === 0 || result.problems.length === 0) return undefined;
		return {
			block: true,
			reason: [
				`Delivery plan check failed for ${url}:`,
				...result.problems.map(problem => `- ${problem}`),
				"Fix these tasks in the plan file, then write the slug to xd://propose again.",
			].join("\n"),
		};
	});
}
