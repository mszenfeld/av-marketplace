import { afterEach, beforeEach, expect, test, vi } from "bun:test";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as zod from "@oh-my-pi/omptype/zod";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import approvedPlanPrompt from "@oh-my-pi/pi-coding-agent/prompts/system/plan-mode-approved" with { type: "text" };
import { USER_INTERRUPT_LABEL } from "@oh-my-pi/pi-coding-agent/session/messages";
import { EventBus } from "@oh-my-pi/pi-coding-agent/utils/event-bus";
import deliveryExtension from "../../delivery/extensions/delivery";
import planReviewExtension, { MAX_ROUNDS, PLAN_CHECK_CHANNEL } from "../extensions/plan-review";

type Hook = (event: { toolName?: string; input?: unknown; prompt?: string; systemPrompt?: string[] }, ctx: ExtensionContext) => Promise<unknown>;
type ToolResult = { content: { type: string; text: string }[] };
type Tool = {
	execute: (id: string, params: { title: string }, signal: AbortSignal | undefined, onUpdate: ((update: ToolResult) => void) | undefined, ctx: ExtensionContext) => Promise<ToolResult>;
};
type Blocked = { block: boolean; reason: string };
type SavedRecord = {
	version: string | null;
	title: string;
	url: string | null;
	round: number | null;
	startedAt: string;
	endedAt: string;
	outcome: "approve" | "approve-with-concerns" | "revise" | "unavailable" | "skipped" | "cancelled" | "exhausted" | "error" | "rejected";
	reason: string | null;
	reviewer: string | null;
	plan: { text: string; sha256: string } | null;
	answer: string | null;
	findings: { severity: "blocker" | "concern" | "nit"; location: string; issue: string; fix: string }[] | null;
	usage: unknown[] | null;
	proposeSha256: string | null;
};

const CONCERN = '```json\n{"findings": [{"severity": "concern", "location": "Task 1", "issue": "No test covers the change.", "fix": "Add a test."}]}\n```';
const BLOCKER = '{"findings": [{"severity": "blocker", "location": "Task 2", "issue": "The script it runs does not exist.", "fix": "Name the existing script."}, {"severity": "concern", "location": "Task 1", "issue": "No test covers the change.", "fix": "Add a test."}, {"severity": "nit", "location": "Summary", "issue": "Wording.", "fix": ""}]}';
const CONCERN_AND_NIT = '{"findings": [{"severity": "concern", "location": "Task 1", "issue": "No test covers\\nthe change.", "fix": "Add a test."}, {"severity": "nit", "location": "Summary", "issue": "Wording.", "fix": ""}]}';
const NIT_ONLY = 'Done.\n{"findings": [{"severity": "nit", "location": "Summary", "issue": "Wording.", "fix": ""}]}';
const USAGE = [
	{ input: 900, output: 40, cacheRead: 0, cacheWrite: 0, totalTokens: 940 },
	{ input: 300, output: 150, cacheRead: 900, cacheWrite: 0, totalTokens: 1350 },
];
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
const VERSION = JSON.parse(readFileSync(join(import.meta.dir, "..", "package.json"), "utf8")).version;
const RECORDS_ON = "[plan-review]\nrecords = true\n";
// The interpreter behind `python3`, resolved once: version-manager shims add a long start-up to every call.
const PYTHON = Bun.spawnSync(["python3", "-c", "import sys; print(sys.executable)"]).stdout.toString().trim();

let root: string;
let planPath: string;
let ctx: ExtensionContext;
let hooks: Map<string, Hook>;
let tool: Tool;
let notifications: { message: string; level: string }[];
let answers: { text: string; stdout?: string; code?: number; killed?: boolean }[];
let reviewerCalls: number;
let reviewerPrompts: string[];
let duringReview: (() => void | Promise<void>) | undefined;
let abortedStdout: string;
let seenRecords: Set<string>;
let configHome: string;
let python: "real" | "missing";
let events: EventBus;

function assistantMessage(content: unknown[], usage: unknown) {
	return { role: "assistant", provider: "openai-codex", model: "gpt-6-astra", content, usage };
}

function reviewerStdout(text: string): string {
	const toolTurn = assistantMessage([{ type: "toolCall", id: "read-1", name: "read", arguments: { path: "README.md" } }], USAGE[0]);
	const toolResult = { role: "toolResult", toolCallId: "read-1", content: [] };
	const finalTurn = assistantMessage([{ type: "thinking", thinking: "Checking the plan." }, { type: "text", text }], USAGE[1]);
	return [
		{ type: "agent_start" },
		{ type: "message_end", message: toolTurn },
		{ type: "message_end", message: toolResult },
		{ type: "message_end", message: finalTurn },
		{ type: "agent_end", messages: [{ role: "user", content: [] }, toolTurn, toolResult, finalTurn] },
	].map(event => JSON.stringify(event)).join("\n");
}

function continuedStdout(text: string): string {
	const toolTurn = assistantMessage([{ type: "toolCall", id: "read-1", name: "read", arguments: { path: "README.md" } }], USAGE[0]);
	const toolResult = { role: "toolResult", toolCallId: "read-1", content: [] };
	const finalTurn = assistantMessage([{ type: "thinking", thinking: "Checking the plan." }, { type: "text", text }], USAGE[1]);
	return [
		{ type: "agent_start" },
		{ type: "message_end", message: toolTurn },
		{ type: "message_end", message: toolResult },
		{ type: "agent_end", messages: [{ role: "user", content: [] }, toolTurn, toolResult] },
		{ type: "agent_start" },
		{ type: "message_end", message: finalTurn },
		{ type: "agent_end", messages: [finalTurn] },
	].map(event => JSON.stringify(event)).join("\n");
}

beforeEach(() => {
	root = mkdtempSync(join(tmpdir(), "plan-review-"));
	mkdirSync(join(root, "artifacts", "local"), { recursive: true });
	planPath = join(root, "artifacts", "local", "feature-plan.md");
	writeFileSync(planPath, "# Feature plan\n\nChange the code.\n");
	notifications = [];
	answers = [];
	reviewerCalls = 0;
	reviewerPrompts = [];
	duringReview = undefined;
	abortedStdout = "";
	seenRecords = new Set();
	configHome = join(root, "config-home");
	mkdirSync(configHome);
	python = "real";
	events = new EventBus();
	ctx = {
		cwd: root,
		sessionManager: {
			getBranch: () => [{ type: "mode_change", mode: "plan" }],
			getSessionId: () => "test-session",
			getArtifactsDir: () => join(root, "artifacts"),
		},
		ui: { notify: (message: string, level: string) => notifications.push({ message, level }) },
	} as unknown as ExtensionContext;
	loadExtension();
});

function loadExtension(): void {
	hooks = new Map();
	planReviewExtension({
		zod,
		events,
		on: (name: string, handler: Hook) => hooks.set(name, handler),
		registerTool: (definition: Tool) => {
			tool = definition;
		},
		exec: async (command: string, args: string[], options?: { cwd?: string; signal?: AbortSignal }) => {
			if (command === "python3") return runPython(args, options?.cwd);
			reviewerCalls++;
			reviewerPrompts.push(args.at(-1) ?? "");
			await duringReview?.();
			if (options?.signal?.aborted) return { stdout: abortedStdout, stderr: "", code: -1, killed: true };
			const answer = answers.shift();
			if (!answer) throw new Error("Unexpected reviewer call");
			if (answer.killed) return { stdout: "", stderr: "", code: -1, killed: true };
			return { stdout: answer.stdout ?? reviewerStdout(answer.text), stderr: "reviewer failed", code: answer.code ?? 0, killed: false };
		},
	} as unknown as ExtensionAPI);
}

afterEach(() => {
	vi.useRealTimers();
	rmSync(root, { recursive: true, force: true });
});

/** Reads records settings in this test's config home; only that script can simulate missing python3. */
function runPython(args: string[], cwd: string | undefined) {
	if (python === "missing" && args.includes(join(import.meta.dir, "..", "scripts", "records_setting.py")))
		throw new Error('Executable not found in $PATH: "python3"');
	const run = Bun.spawnSync([PYTHON, ...args], { cwd, env: { ...process.env, XDG_CONFIG_HOME: configHome } });
	return { stdout: run.stdout.toString(), stderr: run.stderr.toString(), code: run.exitCode ?? -1, killed: false };
}

function userConfig(table: string): void {
	mkdirSync(join(configHome, "av"), { recursive: true });
	writeFileSync(join(configHome, "av", "config.toml"), `version = 1\n\n${table}`);
}

function repositoryConfig(content: string): void {
	mkdirSync(join(root, ".av"), { recursive: true });
	writeFileSync(join(root, ".av", "config.toml"), content);
}

function propose(title = "feature"): Promise<unknown> {
	const hook = hooks.get("tool_call");
	if (!hook) throw new Error("Missing tool_call hook");
	return hook({ toolName: "write", input: { path: "xd://propose", content: title } }, ctx);
}

function startAgent(prompt: string): Promise<unknown> {
	const hook = hooks.get("before_agent_start");
	if (!hook) throw new Error("Missing before_agent_start hook");
	return hook({ prompt, systemPrompt: [] }, ctx);
}

async function review(title = "feature"): Promise<string> {
	const result = await tool.execute("call", { title }, undefined, undefined, ctx);
	return result.content[0].text;
}

function useCwdPlan(content: string): string {
	rmSync(planPath);
	const path = join(root, "PLAN.md");
	writeFileSync(path, content);
	const sessionManager = ctx.sessionManager as unknown as { getBranch: () => unknown[] };
	sessionManager.getBranch = () => [{ type: "mode_change", mode: "plan", data: { planFilePath: "PLAN.md" } }];
	return path;
}

function newRecord(): SavedRecord {
	const dir = join(root, "artifacts", "plan-review");
	const names = (existsSync(dir) ? readdirSync(dir) : []).filter(name => !seenRecords.has(name));
	for (const name of names) seenRecords.add(name);
	expect(names).toHaveLength(1);
	return JSON.parse(readFileSync(join(dir, names[0]), "utf8"));
}

test("with records turned on, every review saves a record with the text sent to the reviewer, its raw answer, findings, usage and the hash the gate accepts", async () => {
	userConfig(RECORDS_ON);
	writeFileSync(planPath, "# Feature plan\n\nChange the code.\n\n## Open review findings\n\n- [concern] Old: stale.\n");
	answers.push({ text: BLOCKER });
	await review();
	const record = newRecord();
	const text = "# Feature plan\n\nChange the code.\n";
	expect(record).toMatchObject({
		version: VERSION,
		title: "feature",
		url: "local://feature-plan.md",
		round: 1,
		outcome: "revise",
		reason: null,
		reviewer: "openai-codex/gpt-6-astra",
		plan: { text, sha256: sha(text) },
		answer: BLOCKER,
		findings: [
			{ severity: "blocker", location: "Task 2", issue: "The script it runs does not exist.", fix: "Name the existing script." },
			{ severity: "concern", location: "Task 1", issue: "No test covers the change.", fix: "Add a test." },
			{ severity: "nit", location: "Summary", issue: "Wording.", fix: "" },
		],
		usage: USAGE,
		proposeSha256: sha(text),
	});
	expect(new Date(record.startedAt).toISOString()).toBe(record.startedAt);
	expect(new Date(record.endedAt).toISOString()).toBe(record.endedAt);
	expect(record.startedAt <= record.endedAt).toBe(true);
	expect(reviewerPrompts[0]).toContain(`<plan>\n${record.plan!.text}\n</plan>`);

	writeFileSync(planPath, "# Feature plan\n\nRevised.\n");
	answers.push({ text: CONCERN });
	await review();
	const revised = newRecord();
	expect(revised).toMatchObject({
		round: 2,
		outcome: "approve-with-concerns",
		answer: CONCERN,
		findings: [{ severity: "concern", location: "Task 1", issue: "No test covers the change.", fix: "Add a test." }],
		proposeSha256: sha(readFileSync(planPath, "utf8")),
	});
	expect(revised.proposeSha256).not.toBe(revised.plan!.sha256);
	expect(await propose()).toBeUndefined();
});

test("a record keeps the usage of every reviewer turn, also of turns before a continuation", async () => {
	userConfig(RECORDS_ON);
	answers.push({ text: "", stdout: continuedStdout('{"findings": []}') });
	expect(await review()).toContain(`round 1 of ${MAX_ROUNDS}: approved.`);
	expect(newRecord()).toMatchObject({
		outcome: "approve",
		usage: USAGE,
		answer: '{"findings": []}',
		reviewer: "openai-codex/gpt-6-astra",
	});
});

test("with records turned on, calls that end without a review still save one record each", async () => {
	userConfig(RECORDS_ON);
	const records: SavedRecord[] = [];
	answers.push({ text: "", code: 1 });
	await review();
	const failed = newRecord();
	records.push(failed);
	expect(failed).toMatchObject({
		outcome: "unavailable",
		round: 1,
		reason: expect.stringContaining("exited with code 1: reviewer failed"),
		reviewer: "openai-codex/gpt-6-astra",
		answer: "",
		usage: USAGE,
		findings: null,
		proposeSha256: failed.plan!.sha256,
	});

	const text = "# Feature plan\n\nRevision 2.\n";
	writeFileSync(planPath, text);
	abortedStdout = reviewerStdout("partial");
	let controller = new AbortController();
	duringReview = () => controller.abort(USER_INTERRUPT_LABEL);
	await expect(tool.execute("call", { title: "feature" }, controller.signal, undefined, ctx)).rejects.toThrow(
		"Plan review cancelled by the user",
	);
	const skipped = newRecord();
	records.push(skipped);
	expect(skipped).toMatchObject({
		outcome: "skipped",
		round: 2,
		plan: { text },
		proposeSha256: skipped.plan!.sha256,
		reviewer: "openai-codex/gpt-6-astra",
		answer: "partial",
		usage: USAGE,
		findings: null,
	});

	abortedStdout = "";
	controller = new AbortController();
	duringReview = () => controller.abort();
	await expect(tool.execute("call", { title: "feature" }, controller.signal, undefined, ctx)).rejects.toThrow(
		/^Plan review cancelled\.$/,
	);
	const cancelled = newRecord();
	records.push(cancelled);
	expect(cancelled).toMatchObject({
		outcome: "cancelled",
		round: 2,
		proposeSha256: null,
		answer: null,
		usage: null,
	});

	duringReview = undefined;
	answers.push({ text: "", killed: true });
	await review();
	const timedOut = newRecord();
	records.push(timedOut);
	expect(timedOut).toMatchObject({
		outcome: "unavailable",
		round: 2,
		reason: expect.stringContaining("did not finish within 15 minutes"),
		reviewer: null,
		answer: null,
		usage: null,
	});

	expect(await review()).toContain(`All ${MAX_ROUNDS} plan review rounds`);
	const exhausted = newRecord();
	records.push(exhausted);
	expect(exhausted).toMatchObject({
		outcome: "exhausted",
		url: "local://feature-plan.md",
		round: null,
		plan: null,
		proposeSha256: null,
	});

	const otherPath = join(root, "artifacts", "local", "other-plan.md");
	writeFileSync(otherPath, "# Other plan\n");
	answers.push({ text: "I think the plan is fine." });
	await review("other");
	const malformed = newRecord();
	records.push(malformed);
	expect(malformed).toMatchObject({
		title: "other",
		outcome: "unavailable",
		round: 1,
		reason: expect.stringContaining("not the expected JSON"),
		answer: "I think the plan is fine.",
		findings: null,
	});

	rmSync(otherPath);
	rmSync(planPath);
	await expect(review()).rejects.toThrow("No plan file found");
	const missing = newRecord();
	records.push(missing);
	expect(missing).toMatchObject({
		outcome: "error",
		url: null,
	});
	expect(missing.reason).toStartWith('No plan file found for "feature".');
	for (const record of records)
		expect(record).toMatchObject({ title: record === malformed ? "other" : "feature", version: VERSION });
});

test.each([
	"a file in place of the records directory",
	"a session without an artifacts directory",
])("a record that cannot be saved (%s) only warns: the review result and the gate stay the same", async (failure: string) => {
	userConfig(RECORDS_ON);
	if (failure === "a file in place of the records directory") {
		writeFileSync(join(root, "artifacts", "plan-review"), "");
	} else {
		const context = ctx as unknown as { localProtocolOptions: { getArtifactsDir: () => string; getSessionId: () => string } };
		context.localProtocolOptions = { getArtifactsDir: () => join(root, "artifacts"), getSessionId: () => "test-session" };
		const sessionManager = ctx.sessionManager as unknown as { getArtifactsDir: () => string | null };
		sessionManager.getArtifactsDir = () => null;
	}
	answers.push({ text: CONCERN });
	expect(await review()).toContain(`round 1 of ${MAX_ROUNDS}: approved with open concerns.`);
	expect(notifications).toEqual([
		expect.objectContaining({ level: "warning", message: expect.stringContaining("could not save its record") }),
	]);
	expect(await propose()).toBeUndefined();
});

test.each([
	"no configuration",
	"records = false in the user file",
	"a repository configuration without a [plan-review] table",
	"records = true but no python3 to read it",
])("records stay off with %s: a review saves none and warns about nothing", async (setup: string) => {
	if (setup === "records = false in the user file") userConfig("[plan-review]\nrecords = false\n");
	if (setup === "a repository configuration without a [plan-review] table") repositoryConfig('version = 1\n\n[code-review]\nafter_review = "none"\n');
	if (setup === "records = true but no python3 to read it") {
		userConfig(RECORDS_ON);
		python = "missing";
	}
	answers.push({ text: CONCERN });
	expect(await review()).toContain(`round 1 of ${MAX_ROUNDS}: approved with open concerns.`);
	expect(notifications).toEqual([]);
	expect(existsSync(join(root, "artifacts", "plan-review"))).toBe(false);
	expect(await propose()).toBeUndefined();
});

test("records = true in the repository's .av/config.toml turns records on too", async () => {
	repositoryConfig(`version = 1\n\n${RECORDS_ON}`);
	answers.push({ text: CONCERN });
	await review();
	expect(newRecord()).toMatchObject({ outcome: "approve-with-concerns", answer: CONCERN });
	expect(notifications).toEqual([]);
});

test.each([
	["a value that is not a boolean", '[plan-review]\nrecords = "yes"\n', "plan-review.records: expected true or false"],
	["a key the user file does not allow", "[plan-review]\nrecords = true\nrecord = true\n", "plan-review.record: not allowed in the user config"],
])("an invalid [plan-review] table (%s) turns records off with a warning; the review result and the gate stay the same", async (_case: string, table: string, problem: string) => {
	userConfig(table);
	answers.push({ text: CONCERN });
	expect(await review()).toContain(`round 1 of ${MAX_ROUNDS}: approved with open concerns.`);
	expect(notifications).toEqual([expect.objectContaining({ level: "warning", message: expect.stringContaining(problem) })]);
	expect(existsSync(join(root, "artifacts", "plan-review"))).toBe(false);
	expect(await propose()).toBeUndefined();
});

test("xd://propose blocks an unreviewed plan and leaves other writes alone", async () => {
	const blocked = (await propose()) as Blocked;
	expect(blocked.block).toBe(true);
	expect(blocked.reason).toContain('write {"title": "feature"} to xd://plan_review');

	const hook = hooks.get("tool_call");
	expect(await hook?.({ toolName: "write", input: { path: "README.md", content: "feature" } }, ctx)).toBeUndefined();
});

test("another plugin's rejected plan check saves a record without using a round or running the reviewer", async () => {
	userConfig(RECORDS_ON);
	writeFileSync(planPath, "# Feature plan\n\nChange the code.\n\n## Open review findings\n\nStale findings.\n");
	let rejects = true;
	events.on(PLAN_CHECK_CHANNEL, data => {
		expect(data).toMatchObject({ url: "local://feature-plan.md", file: realpathSync(planPath), cwd: root });
		expect(readFileSync(planPath, "utf8")).not.toContain("Stale findings.");
		if (typeof data !== "object" || data === null || !("problems" in data) || !Array.isArray(data.problems))
			throw new Error("Invalid check request");
		data.problems.push(Promise.resolve(rejects ? ["", " ", "Task 1 has no Files block."] : []));
		data.problems.push(Promise.reject(new Error("An unrelated check failed")));
	});
	const rejected = await review();
	expect(rejected).toContain("another plugin's plan check rejects local://feature-plan.md");
	expect(rejected).toContain("This call used no round.");
	expect(rejected).toContain("- Task 1 has no Files block.");
	expect(rejected).toContain('write {"title": "feature"} to xd://plan_review again');
	expect(reviewerCalls).toBe(0);
	expect(newRecord()).toMatchObject({
		outcome: "rejected",
		reason: "Task 1 has no Files block.",
		round: null,
		plan: { text: "# Feature plan\n\nChange the code.\n", sha256: sha("# Feature plan\n\nChange the code.\n") },
		proposeSha256: null,
	});
	expect(existsSync(join(root, "artifacts", "plan-review-state.json"))).toBe(false);
	expect(((await propose()) as Blocked).reason).toContain("has not been reviewed");
	rejects = false;
	answers.push({ text: NIT_ONLY });
	expect(await review()).toContain(`round 1 of ${MAX_ROUNDS}: approved`);
	expect(reviewerCalls).toBe(1);
});

test("Delivery rejects invalid task structure before the final round, then its correction is reviewed and both gates accept it", async () => {
	python = "missing";
	execFileSync("git", ["init", "-q", root]);
	writeFileSync(join(root, "README.md"), "# Guide\n");
	const deliveryHooks = new Map<string, Hook>();
	deliveryExtension({
		events,
		on: (name: string, handler: Hook) => deliveryHooks.set(name, handler),
		exec: async (command: string, args: string[]) => {
			const result = Bun.spawnSync([command === "python3" ? PYTHON : command, ...args], { cwd: root });
			return { stdout: result.stdout.toString(), stderr: result.stderr.toString(), code: result.exitCode ?? -1, killed: false };
		},
	} as unknown as ExtensionAPI);
	const valid = "# Feature plan\n\n### Task 1: Update docs\n**Commit:** docs: update guide\n**Files:**\n- Modify: `README.md`\n\nUpdate the guide.\n";
	writeFileSync(planPath, valid);
	answers.push({ text: BLOCKER });
	await review();
	const statePath = join(root, "artifacts", "plan-review-state.json");
	const state = readFileSync(statePath, "utf8");
	writeFileSync(planPath, valid.replace("**Files:**\n- Modify: `README.md`\n", ""));
	const deliveryGate = deliveryHooks.get("tool_call");
	if (!deliveryGate) throw new Error("Missing Delivery gate");
	const event = { toolName: "write", input: { path: "xd://propose", content: "feature" } };
	expect(await review()).toContain("This call used no round.");
	expect(reviewerCalls).toBe(1);
	expect(readFileSync(statePath, "utf8")).toBe(state);
	expect(((await propose()) as Blocked).block).toBe(true);
	writeFileSync(planPath, valid.replace("Update the guide.", "Update the guide and check it."));
	answers.push({ text: CONCERN });
	expect(await review()).toContain(`round 2 of ${MAX_ROUNDS}: approved`);
	expect(reviewerCalls).toBe(MAX_ROUNDS);
	expect(await propose()).toBeUndefined();
	expect(await deliveryGate(event, ctx)).toBeUndefined();
});

test("a title that names no plan file is checked against the plan xd://propose falls back to", async () => {
	// OMP resolves an unusable title to the newest *plan.md; the gate must judge that same file.
	expect(((await propose("feature/Rename the script")) as Blocked).block).toBe(true);

	answers.push({ text: '{"findings": []}' });
	expect(await review("unrelated title")).toContain("Plan review of local://feature-plan.md");
	expect(await propose("feature/Rename the script")).toBeUndefined();
});

test("requested changes keep the plan blocked until a review approves its revised text", async () => {
	answers.push({ text: BLOCKER }, { text: NIT_ONLY });

	const first = await review();
	expect(first).toContain(`by openai-codex/gpt-6-astra, round 1 of ${MAX_ROUNDS}: changes requested`);
	expect(first).toContain("- [blocker] Task 2: The script it runs does not exist. Fix: Name the existing script.");
	expect(readFileSync(planPath, "utf8")).not.toContain("## Open review findings");
	expect(first).toContain("- [concern] Task 1: No test covers the change. Fix: Add a test.");
	expect(((await propose()) as Blocked).reason).toContain("has not changed since");

	writeFileSync(planPath, "# Feature plan\n\nChange the code and add a test.\n");
	expect(((await propose()) as Blocked).reason).toContain("changed since its last review");

	const second = await review();
	expect(second).toContain(`round 2 of ${MAX_ROUNDS}: approved`);
	expect(second).toContain("- [nit] Summary: Wording.");
	expect(await propose()).toBeUndefined();

	writeFileSync(planPath, "# Feature plan\n\nSomething else entirely.\n");
	expect(((await propose()) as Blocked).block).toBe(true);
});

test("a review with concerns and no blockers approves the plan and lists the concerns in the plan file", async () => {
	answers.push({ text: CONCERN_AND_NIT });
	const result = await review();
	expect(result).toContain(`round 1 of ${MAX_ROUNDS}: approved with open concerns.`);
	expect(result).toContain("- [nit] Summary: Wording.");
	expect(result).toContain('"## Open review findings" section');
	const plan = readFileSync(planPath, "utf8");
	expect(plan).toStartWith(`# Feature plan\n\nChange the code.\n\n## Open review findings\n\nPlan Review (openai-codex/gpt-6-astra, round 1 of ${MAX_ROUNDS}) found no blockers.`);
	expect(plan).toEndWith("\n\n- [concern] Task 1: No test covers the change. Fix: Add a test.\n");
	expect(plan).not.toContain("[nit]");
	expect(await propose()).toBeUndefined();

	// An edit of the plugin's section is an edit of the plan: it needs a new review.
	writeFileSync(planPath, `${plan}Edited.\n`);
	expect(((await propose()) as Blocked).reason).toContain("changed since its last review");
});

test("findings fold every JavaScript line terminator into one line in tool results and the plan", async () => {
	answers.push({
		text: JSON.stringify({
			findings: [{
				severity: "concern",
				location: "Task\r 1",
				issue: "No test\r\ncovers \n\t \n the\u2028change.",
				fix: "Add \u2029 \t a test.",
			}],
		}),
	});
	const expected = "- [concern] Task 1: No test covers the change. Fix: Add a test.";
	expect(await review()).toContain(expected);
	expect(readFileSync(planPath, "utf8")).toEndWith(`${expected}\n`);
});

test("findings preserve long interior whitespace runs without a line break", async () => {
	const issue = `No${" ".repeat(100_000)}test covers the change.`;
	answers.push({
		text: JSON.stringify({
			findings: [{ severity: "concern", location: "Task 1", issue, fix: "Add a test." }],
		}),
	});
	const expected = `- [concern] Task 1: ${issue} Fix: Add a test.`;
	expect(await review()).toContain(expected);
	expect(readFileSync(planPath, "utf8")).toEndWith(`${expected}\n`);
});

test("every review removes the previous open findings section before the reviewer reads the plan, and keeps fenced headings", async () => {
	const fenced = "```markdown\n## Open review findings\n```\n";
	writeFileSync(planPath, `# Feature plan\n\nChange the code.\n\n${fenced}`);
	answers.push({ text: CONCERN }, { text: NIT_ONLY });
	await review();
	expect(readFileSync(planPath, "utf8")).toContain("- [concern] Task 1: No test covers the change.");

	writeFileSync(planPath, readFileSync(planPath, "utf8").replace("Change the code.", "Change the code and add a test."));
	expect(await review()).toContain(`round 2 of ${MAX_ROUNDS}: approved.`);
	expect(reviewerPrompts[1]).toContain(fenced);
	expect(reviewerPrompts[1]).not.toContain("- [concern]");
	expect(readFileSync(planPath, "utf8")).toBe(`# Feature plan\n\nChange the code and add a test.\n\n${fenced}`);
	expect(await propose()).toBeUndefined();
});

test.each(["```", "~~~"])("CRLF %s fences preserve example findings headings while real sections are removed", async (fence: string): Promise<void> => {
	const body = `# Feature plan\r\n\r\n${fence}markdown\r\n## Open review findings\r\nKeep this example.\r\n${fence}\r\n\r\n## Next step\r\nKeep this step.`;
	const original = `${body}\r\n`;
	writeFileSync(planPath, original);
	answers.push({ text: NIT_ONLY }, { text: NIT_ONLY });

	await review();
	expect(reviewerPrompts[0]).toContain(original);
	expect(readFileSync(planPath, "utf8")).toBe(original);

	writeFileSync(planPath, `${body}\r\n\r\n## Open review findings\r\n\r\n- [concern] Stale review.\r\n`);
	await review();
	expect(reviewerPrompts[1]).toContain(`${body}\n`);
	expect(reviewerPrompts[1]).not.toContain("Stale review.");
	expect(readFileSync(planPath, "utf8")).toBe(`${body}\n`);
	expect(await propose()).toBeUndefined();
});

test.each(["```", "````", "~~~", "~~~~"])("an unclosed %s fence is closed before findings so later reviews replace them", async (fence: string): Promise<void> => {
	const body = `# Feature plan\n\n${fence}markdown\n## Open review findings\nKeep this example.`;
	const closed = `${body}\n${fence}\n`;
	writeFileSync(planPath, body);
	answers.push(
		{ text: CONCERN },
		{ text: CONCERN.replace("No test covers the change.", "The rollout needs a check.") },
	);

	await review();
	expect(readFileSync(planPath, "utf8")).toStartWith(`${closed}\n## Open review findings\n`);
	expect(await propose()).toBeUndefined();

	await review();
	expect(reviewerPrompts[1]).toContain(closed);
	expect(reviewerPrompts[1]).not.toContain("- [concern]");
	const replaced = readFileSync(planPath, "utf8");
	expect(replaced).toStartWith(`${closed}\n## Open review findings\n`);
	expect(replaced).toContain("- [concern] Task 1: The rollout needs a check.");
	expect(replaced).not.toContain("No test covers the change.");
	expect(replaced).not.toContain("round 1 of");
	expect(await propose()).toBeUndefined();
});

test("a plan edited while the review runs keeps the edit and needs a new review", async () => {
	duringReview = () => writeFileSync(planPath, "# Feature plan\n\nEdited during the review.\n");
	answers.push({ text: CONCERN });
	expect(await review()).toContain("changed while the review ran");
	expect(readFileSync(planPath, "utf8")).toBe("# Feature plan\n\nEdited during the review.\n");
	expect(((await propose()) as Blocked).reason).toContain("changed since its last review");
});

test("a plan outside the session's local:// directory is reviewed as it is and never written", async () => {
	const content = "# Feature plan\n\nChange the code.\n\n## Open review findings\n\nWritten by hand.\n";
	const path = useCwdPlan(content);
	answers.push({ text: CONCERN });
	const result = await review();
	expect(result).toContain(`Plan review of PLAN.md by openai-codex/gpt-6-astra, round 1 of ${MAX_ROUNDS}: approved with open concerns.`);
	expect(result).toContain("only into plan files stored in this session's local:// directory. Tell the user about them");
	expect(reviewerPrompts[0]).toContain("Written by hand.");
	expect(readFileSync(path, "utf8")).toBe(content);
	expect(await propose()).toBeUndefined();
});

test("after the last round, the agent hands the findings of a plan outside the session's local:// directory to the user", async () => {
	const path = useCwdPlan("# Feature plan\n\nRevision 0.\n");
	let last = "";
	for (let round = 1; round <= MAX_ROUNDS; round++) {
		writeFileSync(path, `# Feature plan\n\nRevision ${round}.\n`);
		answers.push({ text: BLOCKER });
		last = await review();
	}
	expect(last).toContain(`round ${MAX_ROUNDS} of ${MAX_ROUNDS}: changes requested`);
	expect(last).toContain("does not list these findings");
	expect(last).toContain("Tell the user about the blockers and concerns above");
	expect(readFileSync(path, "utf8")).toBe(`# Feature plan\n\nRevision ${MAX_ROUNDS}.\n`);
});

test("a session-local plan that links outside the session's local:// directory is never written", async () => {
	const outside = join(root, "outside-plan.md");
	writeFileSync(outside, "# Feature plan\n\nChange the code.\n");
	rmSync(planPath);
	symlinkSync(outside, planPath);
	answers.push({ text: CONCERN });
	expect(await review()).toContain("only into plan files stored in this session's local:// directory");
	expect(readFileSync(outside, "utf8")).toBe("# Feature plan\n\nChange the code.\n");
	expect(await propose()).toBeUndefined();
});

test("a plan swapped for a symlink while the review runs is never written through it", async () => {
	const outside = join(root, "outside-plan.md");
	writeFileSync(outside, "# Feature plan\n\nChange the code.\n");
	duringReview = () => {
		rmSync(planPath);
		symlinkSync(outside, planPath);
	};
	answers.push({ text: CONCERN });
	expect(await review()).toContain("changed while the review ran");
	expect(readFileSync(outside, "utf8")).toBe("# Feature plan\n\nChange the code.\n");
	expect(((await propose()) as Blocked).reason).toContain("changed since its last review");
});

test("a review whose findings cannot be written still uses its round and hands the concerns to the agent", async () => {
	chmodSync(planPath, 0o444);
	answers.push({ text: CONCERN });
	const result = await review();
	expect(result).toContain(`round 1 of ${MAX_ROUNDS}: approved with open concerns.`);
	expect(result).toContain("the plugin could not write them");
	expect(readFileSync(planPath, "utf8")).toBe("# Feature plan\n\nChange the code.\n");
	expect(await propose()).toBeUndefined();

	chmodSync(planPath, 0o644);
	writeFileSync(planPath, "# Feature plan\n\nRevised.\n");
	answers.push({ text: NIT_ONLY });
	expect(await review()).toContain(`round 2 of ${MAX_ROUNDS}: approved.`);
});

test("review progress updates immediately and every 15 seconds, then stops when the review ends", async () => {
	vi.useFakeTimers();
	const updates: string[] = [];
	let start!: () => void;
	let release!: () => void;
	const started = new Promise<void>(resolve => {
		start = resolve;
	});
	const pending = new Promise<void>(resolve => {
		release = resolve;
	});
	duringReview = () => {
		start();
		return pending;
	};
	answers.push({ text: '{"findings": []}' });
	const result = tool.execute("call", { title: "feature" }, undefined, update => updates.push(update.content[0].text), ctx);

	await started;
	vi.advanceTimersByTime(15_000);
	vi.advanceTimersByTime(15_000);
	release();
	await result;

	const updateCount = updates.length;
	vi.advanceTimersByTime(60_000);
	expect(updates.map(text => text.match(new RegExp(`^Plan review round 1/${MAX_ROUNDS}: (\\d+:\\d\\d) elapsed`))?.[1])).toEqual(["0:00", "0:15", "0:30"]);
	for (const text of updates) expect(text.split("\n")[0].length).toBeLessThan(70);
	expect(updates).toHaveLength(updateCount);
});

test("a review that cannot run lets that plan text through with a warning", async () => {
	answers.push({ text: "", code: 1 });
	expect(await review()).toContain("exited with code 1: reviewer failed");
	expect(notifications.at(-1)?.level).toBe("warning");
	expect(await propose()).toBeUndefined();

	writeFileSync(planPath, "# Feature plan\n\nRevised.\n");
	answers.push({ text: "I think the plan is fine." });
	expect(await review()).toContain("not the expected JSON");
	expect(await propose()).toBeUndefined();
});

test.each([
	{ answer: { text: "", code: 1 }, reason: "exited with code 1" },
	{ answer: { text: "", killed: true }, reason: "did not finish within 15 minutes" },
	{ answer: { text: "I think the plan is fine." }, reason: "not the expected JSON" },
])("a reviewer run that $reason uses one of the rounds", async ({ answer, reason }) => {
	for (let round = 1; round <= MAX_ROUNDS; round++) {
		writeFileSync(planPath, `# Feature plan\n\nRevision ${round}.\n`);
		answers.push(answer);
		const result = await review();
		expect(result).toContain(`round ${round} of ${MAX_ROUNDS}`);
		expect(result).toContain(reason);
	}

	expect(await review()).toContain(`All ${MAX_ROUNDS} plan review rounds`);
	expect(reviewerCalls).toBe(MAX_ROUNDS);
	expect(await propose()).toBeUndefined();
});

test("an abort with the user-interrupt reason lets that plan text through until the plan changes", async () => {
	const controller = new AbortController();
	duringReview = () => controller.abort(USER_INTERRUPT_LABEL);
	await expect(tool.execute("call", { title: "feature" }, controller.signal, undefined, ctx)).rejects.toThrow(
		"Plan review cancelled by the user",
	);
	expect(notifications.at(-1)?.level).toBe("warning");
	expect(await propose()).toBeUndefined();

	duringReview = undefined;
	writeFileSync(planPath, "# Feature plan\n\nRevised after cancelling.\n");
	expect(((await propose()) as Blocked).reason).toContain("changed since its last review");
	answers.push({ text: '{"findings": []}' });
	expect(await review()).toContain(`round 1 of ${MAX_ROUNDS}: approved`);
});

test("an abort without the user-interrupt reason keeps the plan blocked", async () => {
	const controller = new AbortController();
	duringReview = () => controller.abort();
	await expect(tool.execute("call", { title: "feature" }, controller.signal, undefined, ctx)).rejects.toThrow(
		/^Plan review cancelled\.$/,
	);
	expect(notifications).toEqual([]);
	expect(((await propose()) as Blocked).reason).toContain("This plan has not been reviewed");
});

test("after the last round its open blockers and concerns go to approval in the plan file, with a warning and no further review", async () => {
	for (let round = 1; round <= MAX_ROUNDS; round++) {
		writeFileSync(planPath, `# Feature plan\n\nRevision ${round}.\n`);
		answers.push({ text: BLOCKER });
		const result = await review();
		expect(result).toContain(`round ${round} of ${MAX_ROUNDS}: changes requested`);
		if (round === MAX_ROUNDS) expect(result).toContain('"## Open review findings" section');
	}
	const plan = readFileSync(planPath, "utf8");
	expect(plan).toContain("still found blockers in its last round");
	expect(plan).toContain("- [blocker] Task 2: The script it runs does not exist. Fix: Name the existing script.");
	expect(plan).toContain("- [concern] Task 1: No test covers the change. Fix: Add a test.");
	expect(plan).not.toContain("[nit]");
	expect(await review()).toContain(`All ${MAX_ROUNDS} plan review rounds`);
	expect(reviewerCalls).toBe(MAX_ROUNDS);

	expect(await propose()).toBeUndefined();
	expect(notifications.at(-1)).toEqual(expect.objectContaining({ level: "warning" }));
});

test("a plan edited during the last round is restored with that round's findings and the edit listed", async (): Promise<void> => {
	const edited = "# Feature plan\n\nEdited during the last review.\n";
	let last = "";
	for (let round = 1; round <= MAX_ROUNDS; round++) {
		writeFileSync(planPath, `# Feature plan\n\nRevision ${round}.\n`);
		if (round === MAX_ROUNDS) duringReview = (): void => writeFileSync(planPath, edited);
		answers.push({ text: BLOCKER });
		last = await review();
	}
	expect(last).toContain(`round ${MAX_ROUNDS} of ${MAX_ROUNDS}: changes requested`);
	expect(last).toContain("restores the text this review covered");
	expect(readFileSync(planPath, "utf8")).toBe(edited);
	expect(((await propose()) as Blocked).reason).toContain("changed after the last one");
	expect(await review()).toContain("listed your 1 later edit");
	expect(reviewerCalls).toBe(MAX_ROUNDS);
	const restored = readFileSync(planPath, "utf8");
	expect(restored).toStartWith(`# Feature plan\n\nRevision ${MAX_ROUNDS}.\n\n## Open review findings\n`);
	expect(restored).toContain("still found blockers in its last round");
	expect(restored).toContain("- [blocker] Task 2:");
	expect(restored).toContain("- [concern] Task 1:");
	expect(restored).toEndWith(`- [edit] Feature plan: changed after the last review round, not reviewed.\n  \`\`\`diff\n  @@ -3,1 +3,1 @@\n  -Revision ${MAX_ROUNDS}.\n  +Edited during the last review.\n  \`\`\`\n`);
	expect(await propose()).toBeUndefined();
});

test("after the last round xd://propose accepts only the reviewed text, and plan_review restores it with later edits listed", async () => {
	userConfig(RECORDS_ON);
	writeFileSync(planPath, "# Feature plan\n\n## Context\n\nChange the code.\n\n### Task 1: Change the code\n\nEdit src/a.ts.\n\n## Verification\n\nRun the tests.\n");
	answers.push({ text: CONCERN }, { text: CONCERN });
	await review();
	newRecord();
	writeFileSync(planPath, readFileSync(planPath, "utf8").replace("Edit src/a.ts.", "Edit src/a.ts and src/b.ts."));
	expect(await review()).toContain("This was the last review round");
	newRecord();
	const reviewed = readFileSync(planPath, "utf8");
	writeFileSync(planPath, reviewed
		.replace("Change the code.\n", "")
		.replace("Edit src/a.ts and src/b.ts.", "Edit src/a.ts, src/b.ts and src/c.ts.")
		.replace("Run the tests.", "Run the tests.\nRun the linter."));
	const blocked = (await propose()) as Blocked;
	expect(blocked.block).toBe(true);
	expect(blocked.reason).toContain("changed after the last one");
	expect(blocked.reason).toContain("xd://plan_review");
	expect(await review()).toContain("listed your 3 later edits");
	expect(reviewerCalls).toBe(MAX_ROUNDS);
	const restored = readFileSync(planPath, "utf8");
	expect(restored).toStartWith(`${reviewed.trimEnd()}\n\nThe planner changed the plan after Plan Review's last round (${MAX_ROUNDS} of ${MAX_ROUNDS}).`);
	expect(restored).toEndWith([
		"- [edit] Context: changed after the last review round, not reviewed.",
		"  ```diff",
		"  @@ -5,1 +4,0 @@",
		"  -Change the code.",
		"  ```",
		"- [edit] Task 1: Change the code: changed after the last review round, not reviewed.",
		"  ```diff",
		"  @@ -9,1 +8,1 @@",
		"  -Edit src/a.ts and src/b.ts.",
		"  +Edit src/a.ts, src/b.ts and src/c.ts.",
		"  ```",
		"- [edit] Verification: changed after the last review round, not reviewed.",
		"  ```diff",
		"  @@ -13,0 +13,1 @@",
		"  +Run the linter.",
		"  ```",
		"",
	].join("\n"));
	expect(newRecord()).toMatchObject({ outcome: "exhausted", round: null, plan: null, proposeSha256: sha(restored) });
	expect(await propose()).toBeUndefined();
	expect(await review()).toContain('are used. Write "feature" to xd://propose');
	expect(newRecord()).toMatchObject({ outcome: "exhausted", proposeSha256: null });
	const body = restored.slice(0, restored.indexOf("\n\n## Open review findings"));
	for (const ending of ["", "\n", "\n\n", "\n\n\n"]) {
		writeFileSync(planPath, `${body}${ending}`);
		expect(await review()).toContain('only its "## Open review findings" section changed');
		expect(readFileSync(planPath, "utf8")).toBe(restored);
		expect(newRecord().proposeSha256).toBe(sha(restored));
		expect(await propose()).toBeUndefined();
	}
	expect(reviewerCalls).toBe(MAX_ROUNDS);
});

test("after the last round an edited plan outside the session's local:// directory stays blocked until the agent undoes the edits", async () => {
	const path = useCwdPlan("# Feature plan\n\nRevision 0.\n");
	for (let round = 1; round <= MAX_ROUNDS; round++) {
		writeFileSync(path, `# Feature plan\n\nRevision ${round}.\n`);
		answers.push({ text: CONCERN });
		await review();
	}
	const reviewed = readFileSync(path, "utf8");
	const edited = reviewed.replace(`Revision ${MAX_ROUNDS}.`, "Revised after the last round.");
	writeFileSync(path, edited);
	const result = await review();
	expect(result).toContain("undo these edits");
	expect(result).toContain("The plugin writes only plan files stored in this session's local:// directory");
	expect(result).toContain("- [edit] Feature plan:");
	expect(result).toContain(`  -Revision ${MAX_ROUNDS}.`);
	expect(result).toContain("  +Revised after the last round.");
	expect(readFileSync(path, "utf8")).toBe(edited);
	expect(((await propose()) as Blocked).reason).toContain("changed after the last one");
	writeFileSync(path, reviewed);
	expect(await propose()).toBeUndefined();
	expect(reviewerCalls).toBe(MAX_ROUNDS);
});

test.each([
	{
		name: "text before any heading",
		before: "Before.\n## Later\nAfter.\n",
		after: "Changed.\n## Later\nAfter.\n",
		location: "Plan",
		hunk: "@@ -1,1 +1,1 @@\n  -Before.\n  +Changed.",
	},
	{
		name: "a removed heading",
		before: "# Feature plan\n\n## Old section\nBody.\n",
		after: "# Feature plan\n\n## New section\nBody.\n",
		location: "Old section",
		hunk: "@@ -3,1 +3,1 @@\n  -## Old section\n  +## New section",
	},
	{
		name: "an insertion before a heading",
		before: "# Feature plan\n\n## Next\nBody.\n",
		after: "# Feature plan\n\nInserted.\n## Next\nBody.\n",
		location: "Feature plan",
		hunk: "@@ -2,0 +3,1 @@\n  +Inserted.",
	},
	{
		name: "a heading inside a backtick fence",
		before: "# Feature plan\n\n```markdown\n## Example\nOld.\n```\n",
		after: "# Feature plan\n\n```markdown\n## Example\nNew.\n```\n",
		location: "Feature plan",
		hunk: "@@ -5,1 +5,1 @@\n  -Old.\n  +New.",
	},
	{
		name: "a heading inside a CRLF tilde fence",
		before: "# Feature plan\r\n\r\n~~~markdown\r\n## Example\r\nOld.\r\n~~~\r\n",
		after: "# Feature plan\r\n\r\n~~~markdown\r\n## Example\r\nNew.\r\n~~~\r\n",
		location: "Feature plan",
		hunk: "@@ -5,1 +5,1 @@\n  -Old.\\r\n  +New.\\r",
	},
	{
		name: "adjacent changes including an empty line and fence markers",
		before: "# Feature plan\n\nOld.\n\n```\nEnd.\n",
		after: "# Feature plan\n\nNew.\n~~~\nEnd.\n",
		location: "Feature plan",
		hunk: "@@ -3,3 +3,2 @@\n  -Old.\n  -\n  -```\n  +New.\n  +~~~",
	},
])("later edits locate $name outside fenced examples and keep their diff in one list item", async ({ before, after, location, hunk }) => {
	writeFileSync(planPath, after);
	writeFileSync(join(root, "artifacts", "plan-review-state.json"), JSON.stringify({
		"local://feature-plan.md": { hash: sha(before), outcome: "approve", rounds: MAX_ROUNDS, text: before },
	}));
	expect(await review()).toContain("listed your 1 later edit");
	const restored = readFileSync(planPath, "utf8");
	expect(restored).toStartWith(before.trimEnd());
	expect(restored).toEndWith(`- [edit] ${location}: changed after the last review round, not reviewed.\n  \`\`\`diff\n  ${hunk}\n  \`\`\`\n`);
	expect(await propose()).toBeUndefined();
	expect(reviewerCalls).toBe(0);
});

test.each(["added", "removed"])("lone carriage returns in %s diff lines cannot introduce an unreviewed Delivery task", async (change: string): Promise<void> => {
	const body = "# Feature plan\n\n### Task 1: Change the code\n\n**Files:**\n- Create: `src/reviewed.py`\n\nChange the code.\n";
	const payload = "Change the code.\r  ```\r### Task 2: Unreviewed task\r\r**Files:**\r- Create: `src/evil.py`\r\r  ```diff";
	// In the removed-line case the reviewed payload is inert inside a longer example fence.
	const before = change === "added" ? body : `${body}\n\`\`\`\`text\n${payload}\n\`\`\`\`\n`;
	const after = change === "added" ? body.replace("Change the code.\n", `${payload}\n`) : `${body}\n\`\`\`\`text\nExample removed.\n\`\`\`\`\n`;
	writeFileSync(planPath, after);
	writeFileSync(join(root, "artifacts", "plan-review-state.json"), JSON.stringify({
		"local://feature-plan.md": { hash: sha(before), outcome: "approve", rounds: MAX_ROUNDS, text: before },
	}));
	expect(((await propose()) as Blocked).block).toBe(true);
	expect(await review()).toContain("listed your 1 later edit");
	const restored = readFileSync(planPath, "utf8");
	expect(restored).toStartWith(before.trimEnd());
	const findings = restored.slice(restored.indexOf("\n\n## Open review findings"));
	expect(findings).not.toContain("\r");
	expect(findings).toContain(`  ${change === "added" ? "+" : "-"}${payload.replace(/\r/g, "\\r")}`);
	expect(await propose()).toBeUndefined();
	const routed = runPython([join(import.meta.dir, "..", "..", "delivery", "scripts", "route_task.py"), "plan", root, planPath], root);
	expect(routed.code).toBe(0);
	expect(JSON.parse(routed.stdout)).toEqual([
		expect.objectContaining({ task: 1, title: "Change the code", files: ["src/reviewed.py"], agent: "python-developer:developer" }),
	]);
	expect(reviewerCalls).toBe(0);
});

test.each(["a failed write", "a concurrent edit"])("restoration refused after the last round for %s leaves the file and review state unchanged", async (failure: string) => {
	for (let round = 1; round <= MAX_ROUNDS; round++) {
		writeFileSync(planPath, `# Feature plan\n\nRevision ${round}.\n`);
		answers.push({ text: CONCERN });
		await review();
	}
	const statePath = join(root, "artifacts", "plan-review-state.json");
	const state = readFileSync(statePath, "utf8");
	const reviewed = readFileSync(planPath, "utf8");
	const edited = "# Feature plan\n\nEdited after the last review.\n";
	const concurrent = "# Feature plan\n\nEdited again while restoring.\n";
	writeFileSync(planPath, edited);
	const originalFile = Bun.file;
	const realPlanPath = realpathSync(planPath);
	let reads = 0;
	const spy = failure === "a concurrent edit"
		? vi.spyOn(Bun, "file").mockImplementation((...args) => {
			if ((args[0] === planPath || args[0] === realPlanPath) && ++reads === 2) writeFileSync(planPath, concurrent);
			return originalFile(...args);
		})
		: undefined;
	if (failure === "a failed write") chmodSync(planPath, 0o444);
	try {
		const result = await review();
		expect(result).toContain("undo these edits");
		expect(result).toContain(failure === "a failed write" ? "The plugin could not restore it" : "The plan file changed while the plugin read it");
		expect(readFileSync(planPath, "utf8")).toBe(failure === "a failed write" ? edited : concurrent);
		expect(readFileSync(statePath, "utf8")).toBe(state);
		expect(((await propose()) as Blocked).block).toBe(true);
	} finally {
		spy?.mockRestore();
		chmodSync(planPath, 0o644);
	}
	writeFileSync(planPath, reviewed);
	expect(await propose()).toBeUndefined();
	expect(reviewerCalls).toBe(MAX_ROUNDS);
});

test("each session keeps its own review rounds: another session starts fresh, switching back restores them", async () => {
	for (let round = 1; round <= MAX_ROUNDS; round++) {
		writeFileSync(planPath, `# Feature plan\n\nRevision ${round}.\n`);
		answers.push({ text: BLOCKER });
		expect(await review()).toContain(`round ${round} of ${MAX_ROUNDS}: changes requested`);
	}

	const sessionManager = ctx.sessionManager as unknown as { getArtifactsDir: () => string };
	sessionManager.getArtifactsDir = () => join(root, "other-artifacts");
	mkdirSync(join(root, "other-artifacts", "local"), { recursive: true });
	writeFileSync(join(root, "other-artifacts", "local", "feature-plan.md"), "# Feature plan\n\nAnother session.\n");

	const hook = hooks.get("session_switch");
	if (!hook) throw new Error("Missing session_switch hook");
	await hook({}, ctx);

	expect(((await propose()) as Blocked).reason).toContain("This plan has not been reviewed");
	answers.push({ text: NIT_ONLY });
	expect(await review()).toContain(`round 1 of ${MAX_ROUNDS}: approved`);
	expect(reviewerCalls).toBe(MAX_ROUNDS + 1);

	sessionManager.getArtifactsDir = () => join(root, "artifacts");
	await hook({}, ctx);
	expect(await review()).toContain(`All ${MAX_ROUNDS} plan review rounds`);
	expect(reviewerCalls).toBe(MAX_ROUNDS + 1);
});

test("review rounds survive an OMP restart that resumes the session", async () => {
	answers.push({ text: BLOCKER });
	await review();

	loadExtension();
	expect(((await propose()) as Blocked).reason).toContain("has not changed since");
	writeFileSync(planPath, "# Feature plan\n\nRevised after restart.\n");
	answers.push({ text: NIT_ONLY });
	expect(await review()).toContain(`round 2 of ${MAX_ROUNDS}: approved`);
	expect(await propose()).toBeUndefined();
	expect(reviewerCalls).toBe(2);
});

test.each(["without a restart", "after a restart before approval", "after a restart after approval"])("approval ends only that plan's review cycle %s: re-entering the same file starts round 1", async (restart: string) => {
	python = "missing";
	const sessionManager = ctx.sessionManager as unknown as { getBranch: () => unknown[] };
	let mode = "plan";
	sessionManager.getBranch = () => [{ type: "mode_change", mode }];
	writeFileSync(join(root, "artifacts", "local", "other-plan.md"), "# Other plan\n");
	answers.push({ text: NIT_ONLY });
	await review("other");
	for (let round = 1; round <= MAX_ROUNDS; round++) {
		writeFileSync(planPath, `# Feature plan\n\nRevision ${round}.\n`);
		answers.push({ text: BLOCKER });
		expect(await review()).toContain(`round ${round} of ${MAX_ROUNDS}: changes requested`);
	}
	expect(await propose()).toBeUndefined();
	const reviewed = readFileSync(planPath, "utf8");
	const statePath = join(root, "artifacts", "plan-review-state.json");
	const otherReview = JSON.parse(readFileSync(statePath, "utf8"))["local://other-plan.md"];
	if (restart === "after a restart before approval") loadExtension();
	mode = "build";
	const prompt = approvedPlanPrompt.replaceAll("{{planFilePath}}", "local://feature-plan.md").replace("{{planContent}}", reviewed);
	expect(await startAgent(prompt)).toBeUndefined();
	expect(readFileSync(planPath, "utf8")).toBe(reviewed);
	expect(JSON.parse(readFileSync(statePath, "utf8"))).toEqual({ "local://other-plan.md": otherReview });
	if (restart === "after a restart after approval") loadExtension();

	mode = "plan";
	expect(await startAgent("Revise the plan for the next request.")).toMatchObject({ systemPrompt: [expect.stringContaining("# Plan review")] });
	const nextPlan = "# Feature plan\n\nDeliver the new request.\n";
	writeFileSync(planPath, nextPlan);
	expect(((await propose()) as Blocked).reason).toContain("This plan has not been reviewed");
	answers.push({ text: NIT_ONLY });
	expect(await review()).toContain(`round 1 of ${MAX_ROUNDS}: approved`);
	expect(reviewerCalls).toBe(MAX_ROUNDS + 2);
	expect(reviewerPrompts.at(-1)).toContain(nextPlan);
	expect(readFileSync(planPath, "utf8")).toBe(nextPlan);
	expect(await propose()).toBeUndefined();
	expect(JSON.parse(readFileSync(statePath, "utf8"))["local://feature-plan.md"].rounds).toBe(1);
});

test("ordinary prompts and malformed approvals keep the review cycle and its saved state", async () => {
	python = "missing";
	answers.push({ text: NIT_ONLY });
	await review();
	const statePath = join(root, "artifacts", "plan-review-state.json");
	const state = readFileSync(statePath, "utf8");
	for (const prompt of [
		'Keep planning.\n<plan path="local://feature-plan.md">\n',
		"Plan approved.\nNo plan tag.",
		'Plan approved.\n<plan path="local://feature-plan.md"\n',
		'Plan approved.\n<plan path="local://missing-plan.md">\n',
	]) {
		await startAgent(prompt);
		expect(readFileSync(statePath, "utf8")).toBe(state);
		expect(await propose()).toBeUndefined();
	}
	writeFileSync(planPath, "# Feature plan\n\nStill the same planning cycle.\n");
	answers.push({ text: NIT_ONLY });
	expect(await review()).toContain(`round 2 of ${MAX_ROUNDS}: approved`);
});

test.each([
	"not json",
	'{"local://feature-plan.md": {"hash": 1}}',
	"null",
	"[]",
	'"state"',
	"1",
])("an unreadable review state (%s) warns and the session's plans get fresh rounds", async (state: string) => {
	writeFileSync(join(root, "artifacts", "plan-review-state.json"), state);
	answers.push({ text: NIT_ONLY });
	expect(await review()).toContain(`round 1 of ${MAX_ROUNDS}: approved`);
	expect(notifications).toEqual([
		expect.objectContaining({
			level: "warning",
			message: expect.stringContaining("could not read its saved review state"),
		}),
	]);
});

test("invalid saved entries are skipped with one warning while valid reviews are restored", async () => {
	const text = readFileSync(planPath, "utf8");
	const valid = { hash: sha(text), outcome: "revise", rounds: 1, text };
	const invalid = [
		null,
		{ ...valid, hash: 1 },
		{ ...valid, text: null },
		{ ...valid, outcome: "constructor" },
		{ ...valid, outcome: "__proto__" },
		{ ...valid, rounds: -1 },
		{ ...valid, rounds: 0.5 },
		{ ...valid, rounds: "1" },
	];
	writeFileSync(join(root, "artifacts", "plan-review-state.json"), JSON.stringify({
		"local://feature-plan.md": valid,
		...Object.fromEntries(invalid.map((entry, index) => [`local://invalid-${index}-plan.md`, entry])),
	}));
	expect(((await propose()) as Blocked).reason).toContain("has not changed since");
	expect(notifications).toHaveLength(1);
	expect(notifications[0].level).toBe("warning");
	expect(notifications[0].message).toEndWith(
		invalid.map((_, index) => `invalid entry for local://invalid-${index}-plan.md`).join(", "),
	);
	writeFileSync(planPath, "# Feature plan\n\nRevised.\n");
	answers.push({ text: NIT_ONLY });
	expect(await review()).toContain(`round 2 of ${MAX_ROUNDS}: approved`);
	const state = JSON.parse(readFileSync(join(root, "artifacts", "plan-review-state.json"), "utf8"));
	expect(Object.keys(state)).toEqual(["local://feature-plan.md"]);
});

test("concurrent calls share one saved-state load", async () => {
	writeFileSync(join(root, "artifacts", "plan-review-state.json"), "not json");
	const results = await Promise.all([propose(), propose()]);
	for (const result of results) expect((result as Blocked).reason).toContain("This plan has not been reviewed");
	expect(notifications).toHaveLength(1);
});

test("a review state that cannot be saved warns, and the gate keeps it until OMP exits", async () => {
	chmodSync(join(root, "artifacts"), 0o555);
	try {
		answers.push({ text: CONCERN });
		expect(await review()).toContain("approved with open concerns");
		expect(notifications).toEqual([
			expect.objectContaining({
				level: "warning",
				message: expect.stringContaining("could not save its review state"),
			}),
		]);
		expect(await propose()).toBeUndefined();
		expect(readdirSync(join(root, "artifacts"))).toEqual(["local"]);
	} finally {
		chmodSync(join(root, "artifacts"), 0o755);
	}
});

test("saved review state holds the annotated text and matching hash, separately from review records", async () => {
	userConfig(RECORDS_ON);
	answers.push({ text: CONCERN });
	await review();
	const text = readFileSync(planPath, "utf8");
	const statePath = join(root, "artifacts", "plan-review-state.json");
	expect(JSON.parse(readFileSync(statePath, "utf8"))).toEqual({
		"local://feature-plan.md": { hash: sha(text), outcome: "approve-with-concerns", rounds: 1, text },
	});
	expect(readFileSync(statePath, "utf8")).toEndWith("\n");
	expect(newRecord().proposeSha256).toBe(sha(text));
	expect(readdirSync(join(root, "artifacts")).sort()).toEqual(["local", "plan-review", "plan-review-state.json"]);
	loadExtension();
	expect(await propose()).toBeUndefined();
});

test("without an artifacts directory review state stays in memory without warnings", async () => {
	const context = ctx as unknown as { localProtocolOptions: { getArtifactsDir: () => string; getSessionId: () => string } };
	context.localProtocolOptions = { getArtifactsDir: () => join(root, "artifacts"), getSessionId: () => "test-session" };
	const sessionManager = ctx.sessionManager as unknown as { getArtifactsDir: () => string | null };
	sessionManager.getArtifactsDir = () => null;
	answers.push({ text: NIT_ONLY });
	expect(await review()).toContain(`round 1 of ${MAX_ROUNDS}: approved`);
	expect(await propose()).toBeUndefined();
	expect(notifications).toEqual([]);
	expect(existsSync(join(root, "artifacts", "plan-review-state.json"))).toBe(false);
	loadExtension();
	expect(((await propose()) as Blocked).reason).toContain("This plan has not been reviewed");
});

test("a final-round symlink swap saves a canonical snapshot with a usable hash for manual restoration", async () => {
	userConfig(RECORDS_ON);
	const outside = join(root, "outside-plan.md");
	const edited = "# Feature plan\n\nEdited during the last review.\n";
	writeFileSync(outside, edited);
	for (let round = 1; round <= MAX_ROUNDS; round++) {
		writeFileSync(planPath, `# Feature plan\n\nRevision ${round}.\n`);
		if (round === MAX_ROUNDS) {
			duringReview = () => {
				rmSync(planPath);
				symlinkSync(outside, planPath);
			};
		}
		answers.push({ text: BLOCKER });
		await review();
		const record = newRecord();
		if (round === MAX_ROUNDS) expect(record.proposeSha256).toBeNull();
	}
	const state = JSON.parse(readFileSync(join(root, "artifacts", "plan-review-state.json"), "utf8"))["local://feature-plan.md"];
	expect(state.rounds).toBe(MAX_ROUNDS);
	expect(state.outcome).toBe("revise");
	expect(state.text).toStartWith(`# Feature plan\n\nRevision ${MAX_ROUNDS}.\n\n## Open review findings\n`);
	expect(state.text).toContain("still found blockers in its last round");
	expect(state.text).toContain("- [blocker] Task 2:");
	expect(state.text).toContain("- [concern] Task 1:");
	expect(state.hash).toBe(sha(state.text));

	loadExtension();
	expect(((await propose()) as Blocked).reason).toContain("changed after the last one");
	const refused = await review();
	expect(refused).toContain("undo these edits");
	expect(refused).toContain("The plugin writes only plan files stored in this session's local:// directory");
	expect(readFileSync(outside, "utf8")).toBe(edited);
	expect(((await propose()) as Blocked).block).toBe(true);
	expect(JSON.parse(readFileSync(join(root, "artifacts", "plan-review-state.json"), "utf8"))["local://feature-plan.md"]).toEqual(state);
	writeFileSync(outside, state.text);
	expect(await propose()).toBeUndefined();
	expect(reviewerCalls).toBe(MAX_ROUNDS);
});
