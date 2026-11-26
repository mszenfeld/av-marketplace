/**
 * Plan review extension: a second model reviews every plan before it reaches the approval dialog.
 *
 * - Plan mode: appends the review procedure to the system prompt.
 * - `plan_review <slug>` (a tool OMP runs as a `bash` command while tool commands are on): runs a headless `omp -p` on the `advisor` model role
 *   with read-only tools over the plan file, and returns its findings to the agent.
 * - While the reviewer runs, the tool reports its round and elapsed time every PROGRESS_INTERVAL_MS.
 * - `propose` (the tool, or the command in `bash`): blocked until a review found no blockers in the current plan text, the review could not run
 *   for that text, its run was aborted with OMP's user-interrupt reason (Esc, leaving plan mode, an RPC or ACP
 *   cancel, or an extension's ctx.abort()). A run that fails or times out counts as one of MAX_ROUNDS runs;
 *   after them only the last round's reviewed text, with its findings section, reaches approval.
 * - Open findings: a review without blockers writes its concerns, and a last round with blockers its blockers and
 *   concerns, to a `## Open review findings` section at the end of the plan file, which the approval dialog shows.
 *   Every review attempt first removes that section; the recorded hash covers the file with the new section. Only a
 *   plan file whose real path lies in the session's `local://` directory is written, and never one edited while the
 *   reviewer ran.
 * - After the last round, plan_review runs no reviewer: it restores the reviewed text and lists later changes as
 *   unreviewed `[edit]` findings for the user to decide on at approval.
 * - State: each plan file's rounds, hash, outcome and reviewed text are saved in `plan-review-state.json` in the session's
 *   artifacts directory, so restarting OMP and resuming the session does not reset its rounds. Approval ends that plan's
 *   review cycle and clears its state; reusing the file starts fresh rounds. A session without a session file keeps this
 *   state only in the process.
 * - Records: with `[plan-review] records = true` in the av configuration (off by default; a contributor's tool for
 *   Plan Review's eval and cost measurement), every plan_review call, whatever its result, saves one JSON record in the
 *   session's artifacts directory (`plan-review/<start>-<id>.json`): the plan text sent to the reviewer, its raw
 *   answer, findings, outcome and token usage. A record that cannot be saved only warns; it never changes the result
 *   or the gate.
 */
import { createHash, randomUUID } from "node:crypto";
import { readFileSync, realpathSync } from "node:fs";
import { mkdir, rename, rm } from "node:fs/promises";
import { join, sep } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { ExecResult } from "@oh-my-pi/pi-coding-agent/exec/exec";
import { type LocalProtocolOptions, resolveLocalRoot } from "@oh-my-pi/pi-coding-agent/internal-urls/local-protocol";
import { resolveApprovedPlan } from "@oh-my-pi/pi-coding-agent/plan-mode/approved-plan";
import { listPlanFiles, readPlanFile, resolvePlanFilePath } from "@oh-my-pi/pi-coding-agent/plan-mode/plan-files";
import { USER_INTERRUPT_LABEL } from "@oh-my-pi/pi-coding-agent/session/messages";
import { diffLineRuns } from "@oh-my-pi/pi-natives";

export const MAX_ROUNDS = 2;
/** Other plugins register asynchronous structural checks synchronously on this shared event. */
export const PLAN_CHECK_CHANNEL = "plan-review:check-plan";
export type PlanCheckRequest = { url: string; file: string; cwd: string; problems: Promise<string[]>[] };
/** Plan path OMP's plan mode starts with when none was chosen. */
const DEFAULT_PLAN_URL = "local://PLAN.md";
// OMP's plan-mode-approved prompt; tests use the upstream template, as Delivery's approval hook does.
const APPROVED_PLAN_PREFIX = "Plan approved.";
const PLAN_TAG_START = '<plan path="';
const PLAN_TAG_END = '">';
const OPEN_FINDINGS_HEADING = "## Open review findings";
const REVIEW_TIMEOUT_MS = 15 * 60_000;
const PROGRESS_INTERVAL_MS = 15_000;
const REVIEWER_ARGS = [
	"-p",
	"--no-session",
	"--no-extensions",
	"--no-skills",
	"--no-title",
	"--no-lsp",
	"--mode",
	"json",
	"--model",
	"@advisor",
	"--tools",
	"read,grep,glob",
];

type Severity = "blocker" | "concern" | "nit";
const SEVERITIES: Record<Severity, true> = { blocker: true, concern: true, nit: true };

type Finding = { severity: Severity; location: string; issue: string; fix: string };
type Review = {
	hash: string;
	outcome: "approve" | "approve-with-concerns" | "revise" | "unavailable" | "skipped";
	rounds: number;
	text: string;
};
const REVIEW_OUTCOMES: Record<Review["outcome"], true> = {
	approve: true,
	"approve-with-concerns": true,
	revise: true,
	unavailable: true,
	skipped: true,
};

const RECORDS_DIR = "plan-review";
const STATE_FILE = "plan-review-state.json";
/** Reads `[plan-review] records` through the shared av_config loader, which is Python. */
const RECORDS_SETTING = join(import.meta.dir, "..", "scripts", "records_setting.py");
const RECORDS_SETTING_TIMEOUT_MS = 30_000;

type Outcome = Review["outcome"] | "cancelled" | "exhausted" | "error" | "rejected";
/** One plan_review call as saved in the session's artifacts directory; the field order is the file's. */
type ReviewRecord = {
	version: string | null;
	title: string;
	url: string | null;
	round: number | null;
	startedAt: string;
	endedAt: string;
	outcome: Outcome;
	reason: string | null;
	reviewer: string | null;
	plan: { text: string; sha256: string } | null;
	answer: string | null;
	findings: Finding[] | null;
	usage: unknown[] | null;
	proposeSha256: string | null;
};

const PLAN_REVIEW_PROMPT = `# Plan review

This session has the plan-review plugin: a reviewer model checks every plan before it goes to approval. When the plan file is ready, run \`plan_review <slug>\` with the slug you will submit to \`propose\`. Only blockers request another round. When a review reports a blocker, resolve every blocker and concern it reports in the plan file, or state in the plan why a finding does not apply, then run the review again. A review without blockers approves the plan: if it reports concerns, the plugin lists them in a \`${OPEN_FINDINGS_HEADING}\` section at the end of the plan file, where the user decides on them in the approval dialog. Propose the plan without revising it for those concerns, and never write or edit that section yourself. \`propose\` is blocked until a review finds no blockers in the current plan text or ${MAX_ROUNDS} review rounds are used; when the last round still finds blockers, the plugin lists its blockers and concerns in the same section. After the last round, \`propose\` accepts only the text that round reviewed, so do not edit the plan after it. If you do, run \`plan_review <slug>\` again: no reviewer runs; the plugin restores the reviewed text and lists your edits in that section, unreviewed. If the user cancels a running review (Esc), do not review that unchanged plan text again unless the user asks: run \`propose <slug>\` when the user wants the plan proposed, and that text goes to approval unreviewed. Until the rounds are used, review the plan again after any edit.`;

const REVIEWER_PROMPT = `You review an implementation plan before a human approves it. The plan is quoted below; the repository is the current directory.

Check the plan against the repository with read, grep and glob: the files, symbols, commands and behaviour it relies on exist and work as it assumes. Look for wrong assumptions about the code, missing steps or files, steps in the wrong order, missing or unverifiable verification, risky or destructive steps without a safeguard, work beyond the plan's stated goal, and ambiguity that would stop an implementer.

Severity:
- blocker: executing the plan as written fails or causes damage.
- concern: a material gap or risk the author should fix before approval.
- nit: an optional improvement.

Report only findings you can tie to a plan section or a repository path. Do not rewrite the plan and do not edit files.

Answer with one JSON object and nothing else:
{"findings": [{"severity": "blocker" | "concern" | "nit", "location": "<plan section or repository path>", "issue": "<what is wrong>", "fix": "<what the plan should say instead>"}]}
An empty findings list approves the plan.`;

/** The branch's latest `mode_change` entry when it is plan mode. Interactive plan mode records one; `--plan-yolo` does not. */
function planModeChange(ctx: ExtensionContext): { data?: Record<string, unknown> } | undefined {
	const branch = ctx.sessionManager.getBranch();
	for (let i = branch.length - 1; i >= 0; i--) {
		const entry = branch[i];
		if (entry.type === "mode_change") return entry.mode === "plan" ? entry : undefined;
	}
	return undefined;
}

/** The plan whose approval ends a review cycle, using the same prompt contract as Delivery. */
function approvedPlanUrl(prompt: string): string | undefined {
	if (!prompt.startsWith(APPROVED_PLAN_PREFIX)) return undefined;
	const tagStart = prompt.indexOf(PLAN_TAG_START);
	if (tagStart < 0) return undefined;
	const tagEnd = prompt.indexOf("\n", tagStart);
	const tag = prompt.slice(tagStart, tagEnd < 0 ? undefined : tagEnd);
	return tag.endsWith(PLAN_TAG_END) ? tag.slice(PLAN_TAG_START.length, -PLAN_TAG_END.length) : undefined;
}

/**
 * `file` resolved through symlinks when it lies inside this session's `local://` directory, the only place the plugin
 * writes plan files (its review records go to the artifacts directory's `plan-review/`, see `saveRecord`): plan_review
 * is a read-tier tool, so it must not change repository files.
 */
function sessionFile(file: string, localProtocolOptions: LocalProtocolOptions): string | undefined {
	try {
		const root = realpathSync(resolveLocalRoot(localProtocolOptions));
		const real = realpathSync(file);
		return real.startsWith(`${root}${sep}`) ? real : undefined;
	} catch {
		return undefined;
	}
}

/** True when `file` still holds `text` at the same real path, so a write cannot follow a symlink swapped in meanwhile. */
async function stillHolds(file: string, text: string): Promise<boolean> {
	try {
		return realpathSync(file) === file && (await Bun.file(file).text()) === text;
	} catch {
		return false;
	}
}

/**
 * The plan `propose` takes for this title. It runs OMP's own lookup (the title's slug, the plan-mode plan path,
 * then the newest `*plan.md`), so a title that names no file cannot slip an unreviewed plan past the gate. `slug` is
 * the clean title that names the resolved file, for the agent's next call.
 */
async function resolvePlan(
	title: string,
	ctx: ExtensionContext,
): Promise<{ url: string; slug: string; content: string; file: string; writable: string | undefined } | undefined> {
	const localProtocolOptions = ctx.localProtocolOptions ?? {
		getArtifactsDir: () => ctx.sessionManager.getArtifactsDir(),
		getSessionId: () => ctx.sessionManager.getSessionId(),
	};
	const statePath = planModeChange(ctx)?.data?.planFilePath;
	try {
		const { planFilePath, planContent } = await resolveApprovedPlan({
			suppliedTitle: title,
			statePlanFilePath: typeof statePath === "string" ? statePath : DEFAULT_PLAN_URL,
			readPlan: url => readPlanFile(url, { localProtocolOptions, cwd: ctx.cwd }),
			listPlanFiles: () => listPlanFiles({ localProtocolOptions }),
		});
		const slug = planFilePath.replace(/^.*\//, "").replace(/\.md$/i, "").replace(/-plan$/i, "");
		const file = realpathSync(resolvePlanFilePath(planFilePath, { localProtocolOptions, cwd: ctx.cwd }));
		return { url: planFilePath, slug, content: planContent, file, writable: sessionFile(file, localProtocolOptions) };
	} catch {
		return undefined;
	}
}

function asFinding(value: unknown): Finding | undefined {
	if (typeof value !== "object" || value === null) return undefined;
	const { severity, location, issue, fix } = value as Record<string, unknown>;
	if (typeof severity !== "string" || !Object.hasOwn(SEVERITIES, severity) || typeof issue !== "string" || !issue.trim())
		return undefined;
	return {
		severity: severity as Severity,
		location: typeof location === "string" ? location.trim() : "",
		issue: issue.trim(),
		fix: typeof fix === "string" ? fix.trim() : "",
	};
}

/** Findings from the reviewer's answer: a bare JSON object, a fenced one, or one embedded in prose. */
function parseFindings(output: string): Finding[] | undefined {
	const text = output.trim();
	const candidates = [text, text.match(/```(?:json)?\s*([\s\S]*?)```/)?.[1], text.slice(text.indexOf("{"), text.lastIndexOf("}") + 1)];
	for (const candidate of candidates) {
		if (!candidate) continue;
		let parsed: unknown;
		try {
			parsed = JSON.parse(candidate);
		} catch {
			continue;
		}
		const findings = (parsed as { findings?: unknown } | null)?.findings;
		if (!Array.isArray(findings)) continue;
		const valid = findings.map(asFinding);
		if (valid.every(finding => finding !== undefined)) return valid as Finding[];
	}
	return undefined;
}

type ReviewerMessage = { role?: string; provider?: string; model?: string; content?: { type?: string; text?: string }[]; usage?: unknown };

/**
 * The reviewer's stdout from `omp -p --mode json`: the model that answered and its final text, from the last
 * `agent_end` event, and the `usage` of every assistant `message_end` event. An `agent_end` lists only its own loop's
 * messages, and a continuation starts a new loop, so usage comes from the whole stream.
 */
function reviewerOutput(stdout: string): { answer: { reviewer: string; text: string } | undefined; usage: unknown[] | null } {
	let end: { messages?: ReviewerMessage[] } | undefined;
	let usage: unknown[] | null = null;
	for (const line of stdout.split("\n")) {
		let event: { type?: string; message?: ReviewerMessage; messages?: ReviewerMessage[] } | null;
		try {
			event = JSON.parse(line);
		} catch {
			continue;
		}
		if (event?.type === "agent_end") end = event;
		else if (event?.type === "message_end" && event.message?.role === "assistant") {
			usage ??= [];
			if (event.message.usage !== undefined) usage.push(event.message.usage);
		}
	}
	const message = end?.messages?.findLast(entry => entry.role === "assistant");
	if (!message) return { answer: undefined, usage };
	const text = (message.content ?? []).flatMap(part => (part.type === "text" && part.text ? [part.text] : [])).join("\n");
	return { answer: { reviewer: `${message.provider}/${message.model}`, text }, usage };
}

function sha256(text: string): string {
	return createHash("sha256").update(text).digest("hex");
}

/** One finding as a list item; line breaks inside the reviewer's text are folded so the item stays one line. */
function formatFinding({ severity, location, issue, fix }: Finding): string {
	const item = `- [${severity}]${location ? ` ${location}:` : ""} ${issue}${fix ? ` Fix: ${fix}` : ""}`;
	return item.split(/[\r\n\u2028\u2029]/).map(line => line.trim()).filter(Boolean).join(" ");
}

/** Track the fence enclosing a line, using the same rules when removing and appending findings. */
function fenceAfterLine(line: string, fence: string | undefined): string | undefined {
	const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line.trimEnd());
	if (fence !== undefined) {
		return marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim() ? undefined : fence;
	}
	return marker?.[1];
}

/**
 * The plan without its `## Open review findings` sections. A section runs from its heading to the next `#` or `##`
 * heading or the end of the file; lines inside fenced code blocks are content. Returns the input itself when it has
 * no such section, so an unchanged plan keeps its hash.
 */
function withoutOpenFindings(content: string): string {
	const kept: string[] = [];
	let fence: string | undefined;
	let inSection = false;
	let removed = false;
	for (const line of content.split("\n")) {
		fence = fenceAfterLine(line, fence);
		if (fence === undefined) {
			if (line.trimEnd() === OPEN_FINDINGS_HEADING) {
				inSection = true;
				removed = true;
				continue;
			} else if (inSection && /^#{1,2}\s/.test(line)) {
				inSection = false;
			}
		}
		if (!inSection) kept.push(line);
	}
	return removed ? `${kept.join("\n").trimEnd()}\n` : content;
}

/** Append findings outside any fence the plan left open, so a later review can remove the section. */
function withOpenFindings(body: string, block: string): string {
	let fence: string | undefined;
	for (const line of body.split("\n")) fence = fenceAfterLine(line, fence);
	const closed = `${body.trimEnd()}${fence ? `\n${fence}` : ""}`;
	return `${closed}\n\n${OPEN_FINDINGS_HEADING}\n\n${block}\n`;
}

const EDITS_INTRO = `The planner changed the plan after Plan Review's last round (${MAX_ROUNDS} of ${MAX_ROUNDS}). The plan-review plugin restored the text that round reviewed and lists those changes here, unreviewed: decide on them when you approve the plan.`;

/** Format native line-diff runs as one Delivery finding per contiguous change, outside fenced example headings. */
function planEdits(before: string, after: string): string[] {
	const oldLines = before.split("\n");
	const newLines = after.split("\n");
	const edits: string[] = [];
	let oldIndex = 0;
	let newIndex = 0;
	let oldStart = 0;
	let newStart = 0;
	let removed = 0;
	let added = 0;
	let scanned = 0;
	let fence: string | undefined;
	let location = "Plan";
	const flush = (): void => {
		if (removed === 0 && added === 0) return;
		const end = oldStart + (removed > 0 ? 1 : 0);
		for (; scanned < end; scanned++) {
			const line = oldLines[scanned];
			fence = fenceAfterLine(line, fence);
			const heading = fence === undefined ? /^#{1,6}\s+(.+)$/.exec(line.trimEnd()) : null;
			if (heading) location = heading[1].trim();
		}
		const lines = [
			`- [edit] ${location}: changed after the last review round, not reviewed.`,
			"  ```diff",
			`  @@ -${oldStart + (removed > 0 ? 1 : 0)},${removed} +${newStart + (added > 0 ? 1 : 0)},${added} @@`,
		];
		// Markdown and universal-newline readers must not turn one prefixed diff line into multiple lines.
		for (let i = oldStart; i < oldStart + removed; i++) lines.push(`  -${oldLines[i].replace(/\r/g, "\\r")}`);
		for (let i = newStart; i < newStart + added; i++) lines.push(`  +${newLines[i].replace(/\r/g, "\\r")}`);
		lines.push("  ```");
		edits.push(lines.join("\n"));
		removed = 0;
		added = 0;
	};
	for (const run of diffLineRuns(before, after)) {
		if (!run.added && !run.removed) {
			flush();
			oldIndex += run.count;
			newIndex += run.count;
			continue;
		}
		if (removed === 0 && added === 0) {
			oldStart = oldIndex;
			newStart = newIndex;
		}
		if (run.removed) {
			removed += run.count;
			oldIndex += run.count;
		} else {
			added += run.count;
			newIndex += run.count;
		}
	}
	flush();
	return edits;
}

function withEdits(text: string, edits: string[]): string {
	const block = `${EDITS_INTRO}\n\n${edits.join("\n")}`;
	return withoutOpenFindings(text) !== text ? `${text.trimEnd()}\n\n${block}\n` : withOpenFindings(text, block);
}

function textResult(text: string, details: Record<string, unknown>) {
	return { content: [{ type: "text" as const, text }], details };
}

/** Elapsed time as m:ss for the progress line. */
function formatElapsed(ms: number): string {
	const seconds = Math.floor(ms / 1000);
	return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/** The plugin version from its package.json, for the review records; null when it cannot be read. */
function pluginVersion(): string | null {
	try {
		const { version } = JSON.parse(readFileSync(join(import.meta.dir, "..", "package.json"), "utf8")) as { version?: unknown };
		return typeof version === "string" ? version : null;
	} catch {
		return null;
	}
}

type RecordsSetting = { records: boolean; warning: string | null };

/**
 * `[plan-review] records` from the av configuration (docs/configuration.md), read by `records_setting.py`. Records stay
 * off without python3, since nothing else can read the setting; a setting that cannot be read otherwise warns.
 */
async function recordsSetting(pi: ExtensionAPI, cwd: string): Promise<RecordsSetting> {
	let result: ExecResult;
	try {
		result = await pi.exec("python3", ["-B", RECORDS_SETTING, "--cwd", cwd], { cwd, timeout: RECORDS_SETTING_TIMEOUT_MS });
	} catch {
		return { records: false, warning: null };
	}
	try {
		const setting = JSON.parse(result.stdout) as { records?: unknown; warning?: unknown };
		if (result.code === 0 && typeof setting.records === "boolean") {
			return { records: setting.records, warning: typeof setting.warning === "string" ? setting.warning : null };
		}
	} catch {
		// Reported below with the script's stderr.
	}
	return {
		records: false,
		warning: `Plan review could not read its [plan-review] records setting, so it saves no record of this call.\n${result.stderr.trim().slice(-500)}`,
	};
}

/** Save the record of one plan_review call in the session's artifacts directory, never the repository; a failure only warns. */
async function saveRecord(record: ReviewRecord, ctx: ExtensionContext): Promise<void> {
	try {
		const artifactsDir = ctx.sessionManager.getArtifactsDir();
		if (!artifactsDir) throw new Error("this session has no artifacts directory");
		const dir = join(artifactsDir, RECORDS_DIR);
		await mkdir(dir, { recursive: true });
		const name = `${record.startedAt.replaceAll(":", "-")}-${randomUUID().slice(0, 8)}.json`;
		await Bun.write(join(dir, name), `${JSON.stringify(record, null, 2)}\n`);
	} catch (error) {
		ctx.ui.notify(`Plan review could not save its record of this call; the review result stands.\n${String(error)}`, "warning");
	}
}

/** Restore valid review entries from this session's artifacts; a failed read gives plans fresh rounds and only warns. */
async function loadReviews(ctx: ExtensionContext): Promise<Map<string, Review>> {
	const reviews = new Map<string, Review>();
	const dir = ctx.sessionManager.getArtifactsDir();
	if (!dir) return reviews;
	const path = join(dir, STATE_FILE);
	let detail: string | undefined;
	try {
		const file = Bun.file(path);
		if (!(await file.exists())) return reviews;
		const saved: unknown = await file.json();
		if (typeof saved !== "object" || saved === null || Array.isArray(saved)) throw new Error("saved review state must be an object");
		const invalid: string[] = [];
		for (const [url, entry] of Object.entries(saved)) {
			if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
				invalid.push(`invalid entry for ${url}`);
				continue;
			}
			const { hash, outcome, rounds, text } = entry as Record<string, unknown>;
			if (
				typeof hash !== "string" ||
				typeof text !== "string" ||
				typeof outcome !== "string" ||
				!Object.hasOwn(REVIEW_OUTCOMES, outcome) ||
				typeof rounds !== "number" ||
				!Number.isInteger(rounds) ||
				rounds < 0
			) {
				invalid.push(`invalid entry for ${url}`);
				continue;
			}
			reviews.set(url, { hash, outcome: outcome as Review["outcome"], rounds, text });
		}
		if (invalid.length > 0) detail = invalid.join(", ");
	} catch (error) {
		detail = String(error);
	}
	if (detail) {
		ctx.ui.notify(
			`Plan review could not read its saved review state (${path}), so this session's plans get fresh review rounds.\n${detail}`,
			"warning",
		);
	}
	return reviews;
}

/** Atomically replace the session's saved state; a failed write leaves the in-process review and gate intact. */
async function saveReviews(reviews: Map<string, Review>, ctx: ExtensionContext): Promise<void> {
	const dir = ctx.sessionManager.getArtifactsDir();
	if (!dir) return;
	const path = join(dir, STATE_FILE);
	const temp = `${path}.${randomUUID().slice(0, 8)}.tmp`;
	try {
		await mkdir(dir, { recursive: true });
		await Bun.write(temp, `${JSON.stringify(Object.fromEntries(reviews))}\n`);
		await rename(temp, path);
	} catch (error) {
		await rm(temp, { force: true }).catch(() => {});
		ctx.ui.notify(
			`Plan review could not save its review state, so an OMP restart may not restore this session's current review rounds.\n${String(error)}`,
			"warning",
		);
	}
}

/** The slug as one shell word: bare when it needs no quoting, else single-quoted. */
function commandWord(slug: string): string {
	return /^[A-Za-z0-9._/-]+$/.test(slug) ? slug : `'${slug.replaceAll("'", "'\\''")}'`;
}

/**
 * `command` split into simple commands at unquoted `;`, `&`, `|`, `(`, `)` and newlines, each a list of its words
 * without quotes. `null` stands for a word the shell would expand: one with `$` or a backquote outside single quotes.
 */
function shellCommands(command: string): (string | null)[][] {
	const commands: (string | null)[][] = [];
	let words: (string | null)[] = [];
	let word: string | undefined;
	let expands = false;
	let quote: "'" | '"' | undefined;
	const endWord = () => {
		if (word !== undefined) words.push(expands ? null : word);
		word = undefined;
		expands = false;
	};
	for (let i = 0; i < command.length; i++) {
		const char = command[i];
		if (quote === "'") {
			if (char === "'") quote = undefined;
			else word = (word ?? "") + char;
		} else if (char === "\\") {
			const next = command[i + 1];
			// Inside double quotes a backslash escapes only these characters; before any other it stays.
			if (quote === '"' && (next === undefined || !'$`"\\\n'.includes(next))) word = (word ?? "") + char;
			else {
				i++;
				if (next !== undefined && next !== "\n") word = (word ?? "") + next;
			}
		} else if (char === "$" || char === "`") {
			expands = true;
			word = (word ?? "") + char;
		} else if (quote === '"') {
			if (char === '"') quote = undefined;
			else word = (word ?? "") + char;
		} else if (char === "'" || char === '"') {
			quote = char;
			word ??= "";
		} else if (" \t;&|()\n".includes(char)) {
			endWord();
			if (char !== " " && char !== "\t" && words.length > 0) {
				commands.push(words);
				words = [];
			}
		} else {
			word = (word ?? "") + char;
		}
	}
	endWord();
	if (words.length > 0) commands.push(words);
	return commands;
}

/**
 * The title a tool call submits to OMP's `propose`: a direct `propose` call's `title`, or the argument of
 * `propose <slug>` (or `--title <slug>`) in a `bash` command. `null` when the call runs `propose` in a form this
 * cannot read (twice, other arguments, a slug the shell would expand, input from JSON, a file or stdin);
 * `undefined` when it does not run `propose`.
 */
function proposedTitle(toolName: string, input: unknown): string | null | undefined {
	const fields = (typeof input === "object" && input !== null ? input : {}) as Record<string, unknown>;
	if (toolName === "propose") return typeof fields.title === "string" ? fields.title : null;
	if (toolName !== "bash" || typeof fields.command !== "string") return undefined;
	const proposals = shellCommands(fields.command).filter(words => words[0] === "propose");
	if (proposals.length === 0) return undefined;
	const args = proposals.length === 1 ? proposals[0].slice(1) : [];
	const [first, second] = args;
	if (args.length === 1 && first != null && !/^[-@]/.test(first)) return first;
	if (args.length === 1 && first?.startsWith("--title=")) return first.slice("--title=".length);
	if (args.length === 2 && first === "--title" && second != null) return second;
	return null;
}

export default function planReviewExtension(pi: ExtensionAPI): void {
	const version = pluginVersion();
	/** Latest review per plan file path, for the current session: `local://` plan files belong to one session. */
	let reviews: Promise<Map<string, Review>> | undefined;
	// `/new`, `/resume`, a fork and a session reload keep this extension loaded; each session has its own state file,
	// so the next call loads the new session's file.
	pi.on("session_switch", () => {
		reviews = undefined;
	});
	const sessionReviews = (ctx: ExtensionContext) => (reviews ??= loadReviews(ctx));
	const storeReview = async (url: string, review: Review, ctx: ExtensionContext) => {
		const state = await sessionReviews(ctx);
		state.set(url, review);
		await saveReviews(state, ctx);
	};
	const forgetReview = async (url: string, ctx: ExtensionContext) => {
		const state = await sessionReviews(ctx);
		if (state.delete(url)) await saveReviews(state, ctx);
	};

	pi.on("before_agent_start", async (event, ctx) => {
		// Approval can keep this session alive; the next plan in the same file is a new planning cycle.
		const approved = approvedPlanUrl(event.prompt);
		if (approved) await forgetReview(approved, ctx);
		if (!planModeChange(ctx)) return undefined;
		return { systemPrompt: [...event.systemPrompt, PLAN_REVIEW_PROMPT] };
	});

	pi.registerTool({
		name: "plan_review",
		label: "Plan Review",
		description:
			"Review a plan-mode plan with the advisor-role model before proposing it. Pass the slug you will submit to `propose`; returns blockers, concerns and nits. Only blockers keep `propose` blocked; the concerns of a review without blockers go to approval in the plan file. After the last round it runs no reviewer: it restores the text that round reviewed and lists any later edit in the plan file.",
		parameters: pi.zod.object({
			title: pi.zod.string().describe("Plan slug, the same text you submit to `propose`"),
		}),
		approval: "read",
		async execute(_toolCallId, params, signal, onUpdate, ctx) {
			const started = Date.now();
			const record: ReviewRecord = {
				version,
				title: params.title,
				url: null,
				round: null,
				startedAt: new Date(started).toISOString(),
				endedAt: "",
				outcome: "error",
				reason: null,
				reviewer: null,
				plan: null,
				answer: null,
				findings: null,
				usage: null,
				proposeSha256: null,
			};
			try {
				const plan = await resolvePlan(params.title, ctx);
				if (!plan) throw new Error(`No plan file found for "${params.title}". Write the plan to local://<slug>-plan.md first.`);
				const slugWord = commandWord(plan.slug);
				record.url = plan.url;
				const review = (await sessionReviews(ctx)).get(plan.url);
				const rounds = review?.rounds ?? 0;
				if (review && rounds >= MAX_ROUNDS) {
					record.outcome = "exhausted";
					if (sha256(plan.content) === review.hash)
						return textResult(
							`All ${MAX_ROUNDS} plan review rounds for ${plan.url} are used. Run \`propose ${slugWord}\`; the user decides on the plan without another review.`,
							{ url: plan.url, rounds, edits: 0, restored: false },
						);
					const view = plan.writable
						? (text: string): string => `${withoutOpenFindings(text).trimEnd()}\n`
						: (text: string): string => text;
					const edits = planEdits(view(review.text), view(plan.content));
					let reason = "The plugin writes only plan files stored in this session's local:// directory";
					if (plan.writable) {
						if (await stillHolds(plan.writable, plan.content)) {
							const restored = edits.length > 0 ? withEdits(review.text, edits) : review.text;
							let written = false;
							try {
								await Bun.write(plan.writable, restored);
								written = true;
							} catch (error) {
								reason = `The plugin could not restore it (${String(error)})`;
							}
							if (written) {
								const hash = sha256(restored);
								await storeReview(plan.url, { ...review, hash, text: restored }, ctx);
								record.proposeSha256 = hash;
								return textResult(
									edits.length > 0
										? `All ${MAX_ROUNDS} plan review rounds for ${plan.url} are used, and the plan changed after the last one. The plugin restored the text that round reviewed and listed your ${edits.length} later ${edits.length === 1 ? "edit" : "edits"} in the "${OPEN_FINDINGS_HEADING}" section at the end of ${plan.url}, unreviewed; the user decides on them in the approval dialog. Do not edit the plan again. Run \`propose ${slugWord}\`.`
										: `All ${MAX_ROUNDS} plan review rounds for ${plan.url} are used, and only its "${OPEN_FINDINGS_HEADING}" section changed after the last one; only the plugin writes that section. The plugin restored the text that round reviewed. Do not edit the plan again. Run \`propose ${slugWord}\`.`,
									{ url: plan.url, rounds, edits: edits.length, restored: true },
								);
							}
						} else {
							reason = "The plan file changed while the plugin read it";
						}
					}
					return textResult(
						`All ${MAX_ROUNDS} plan review rounds for ${plan.url} are used, and the plan changed after the last one. ${reason}, so undo these edits in ${plan.url} until it holds the text that round reviewed, then run \`propose ${slugWord}\`; tell the user about any edit you still want:\n\n${edits.join("\n")}`,
						{ url: plan.url, rounds, edits: edits.length, restored: false },
					);
				}
				const round = rounds + 1;
				// Every review attempt replaces the section an earlier review wrote: the reviewer must not read stale findings,
				// and the plan must not go to approval with them. Only a plan file inside the session's local:// directory is written.
				const { writable } = plan;
				const body = writable ? withoutOpenFindings(plan.content) : plan.content;
				if (writable && body !== plan.content) await Bun.write(writable, body);
				const hash = sha256(body);
				record.plan = { text: body, sha256: hash };
				const request: PlanCheckRequest = { url: plan.url, file: plan.file, cwd: ctx.cwd, problems: [] };
				// Replay evals and older extension fakes have no event bus; no installed checker means no structural veto.
				pi.events?.emit(PLAN_CHECK_CHANNEL, request);
				const checks = await Promise.allSettled(request.problems);
				const problems = checks.flatMap(check =>
					check.status === "fulfilled" && Array.isArray(check.value)
						? check.value.filter(problem => typeof problem === "string" && problem.trim().length > 0)
						: [],
				);
				if (problems.length > 0) {
					record.outcome = "rejected";
					record.reason = problems.join("\n");
					return textResult(
						`Plan review did not run: another plugin's plan check rejects ${plan.url}, and the review rounds go only to a plan it accepts. This call used no round.\n${problems.map(problem => `- ${problem}`).join("\n")}\nFix these in the plan file, then run \`plan_review ${slugWord}\` again.`,
						{ url: plan.url, outcome: "rejected", problems },
					);
				}
				record.round = round;
				const unavailable = async (reason: string) => {
					await storeReview(plan.url, { hash, outcome: "unavailable", rounds: round, text: body }, ctx);
					record.outcome = "unavailable";
					record.reason = reason;
					record.proposeSha256 = hash;
					ctx.ui.notify(`Plan review could not run (round ${round} of ${MAX_ROUNDS}); the plan goes to approval unreviewed.\n${reason}`, "warning");
					return textResult(
						`Plan review could not run (round ${round} of ${MAX_ROUNDS}); run \`propose ${slugWord}\`, the plan goes to approval unreviewed.\n${reason}`,
						{ url: plan.url, round, outcome: "unavailable" },
					);
				};
				const skipped = async () => {
					await storeReview(plan.url, { hash, outcome: "skipped", rounds, text: body }, ctx);
					record.outcome = "skipped";
					record.proposeSha256 = hash;
					ctx.ui.notify(
						`Plan review skipped: the current text of ${plan.url} goes to approval unreviewed. Any edit to the plan needs a new review.`,
						"warning",
					);
					return new Error(
						`Plan review cancelled by the user. If the user asks to propose the plan, run \`propose ${slugWord}\`; its current text goes to approval unreviewed.`,
					);
				};
				const cancelled = async () => {
					if (signal?.reason === USER_INTERRUPT_LABEL) return await skipped();
					record.outcome = "cancelled";
					return new Error("Plan review cancelled.");
				};

				const progress = () =>
					onUpdate?.(
						textResult(
							`Plan review round ${round}/${MAX_ROUNDS}: ${formatElapsed(Date.now() - started)} elapsed (stops at ${REVIEW_TIMEOUT_MS / 60_000}:00). Esc skips it.\nReviewing ${plan.url} with the advisor model role.`,
							{ url: plan.url, round },
						),
					);
				progress();
				const heartbeat = setInterval(progress, PROGRESS_INTERVAL_MS);

				let result: ExecResult;
				try {
					result = await pi.exec("omp", [...REVIEWER_ARGS, `${REVIEWER_PROMPT}\n\nPlan (${plan.url}):\n\n<plan>\n${body}\n</plan>`], {
						cwd: ctx.cwd,
						signal,
						timeout: REVIEW_TIMEOUT_MS,
					});
				} catch (error) {
					if (signal?.aborted) throw await cancelled();
					return await unavailable(`${String(error)}.`);
				} finally {
					clearInterval(heartbeat);
				}
				const { answer, usage } = reviewerOutput(result.stdout);
				record.usage = usage;
				if (answer) {
					record.reviewer = answer.reviewer;
					record.answer = answer.text;
				}
				if (signal?.aborted) throw await cancelled();
				if (result.killed) return await unavailable(`the reviewer did not finish within ${REVIEW_TIMEOUT_MS / 60_000} minutes.`);
				if (result.code !== 0) return await unavailable(`the reviewer exited with code ${result.code}: ${result.stderr.trim().slice(-500)}`);
				if (!answer) return await unavailable(`the reviewer's output has no final answer: ${result.stdout.trim().slice(-500)}`);
				const { reviewer, text } = answer;
				const findings = parseFindings(text);
				if (!findings) return await unavailable(`${reviewer}'s answer is not the expected JSON: ${text.trim().slice(0, 500)}`);
				record.findings = findings;

				const blocked = findings.some(finding => finding.severity === "blocker");
				const concerns = findings.filter(finding => finding.severity === "concern");
				const outcome = blocked ? "revise" : concerns.length > 0 ? "approve-with-concerns" : "approve";
				const last = round >= MAX_ROUNDS;
				const lastRound = blocked && last;
				const open = lastRound ? findings.filter(finding => finding.severity !== "nit") : outcome === "approve-with-concerns" ? concerns : [];
				let reviewed = body;
				let listed = false;
				let changed = false;
				let annotated: string | undefined;
				let unlisted = "the plugin writes open findings only into plan files stored in this session's local:// directory";
				if (open.length > 0 && writable) {
					const by = `Plan Review (${reviewer}, round ${round} of ${MAX_ROUNDS})`;
					const intro = lastRound
						? `${by} still found blockers in its last round. These findings remain open: decide on them when you approve the plan. The plan-review plugin wrote this section; any later review of the plan replaces it.`
						: `${by} found no blockers. These concerns remain open and do not block approval: decide on them when you approve the plan. The plan-review plugin wrote this section; any later review of the plan replaces it.`;
					annotated = withOpenFindings(body, `${intro}\n\n${open.map(formatFinding).join("\n")}`);
					// The review can run for minutes: never overwrite an edit made meanwhile, nor write through a swapped-in symlink.
					if (await stillHolds(writable, body)) {
						try {
							await Bun.write(writable, annotated);
							reviewed = annotated;
							listed = true;
						} catch (error) {
							unlisted = `the plugin could not write them (${String(error)})`;
						}
					} else {
						changed = true;
					}
				}
				// Keep the canonical snapshot's hash usable for byte-exact restoration after a concurrent edit.
				const snapshot = changed ? (annotated ?? body) : reviewed;
				const gateHash = sha256(snapshot);
				await storeReview(plan.url, { hash: gateHash, outcome, rounds: round, text: snapshot }, ctx);
				record.outcome = outcome;
				record.proposeSha256 = changed ? null : gateHash;
				const status = { approve: "approved", "approve-with-concerns": "approved with open concerns", revise: "changes requested" }[outcome];
				const lines = [
					`Plan review of ${plan.url} by ${reviewer}, round ${round} of ${MAX_ROUNDS}: ${status}.`,
					...(findings.length > 0 ? ["", ...findings.map(formatFinding)] : []),
					"",
				];
				if (changed && last)
					lines.push(
						`${plan.url} changed while the review ran, so the plugin did not list the open findings in it, and all ${MAX_ROUNDS} review rounds are used. Run \`plan_review ${slugWord}\`: no reviewer runs; the plugin restores the text this review covered with the findings above and lists your edit in the "${OPEN_FINDINGS_HEADING}" section, unreviewed. Then run \`propose ${slugWord}\`.`,
					);
				else if (changed)
					lines.push(
						`${plan.url} changed while the review ran, so the plugin did not list the open findings in it and this review does not cover its current text. Run \`plan_review ${slugWord}\` again.`,
					);
				else if (outcome === "approve") lines.push(`Run \`propose ${slugWord}\` to request approval.`);
				else if (outcome === "approve-with-concerns" && listed)
					lines.push(
						`The concerns are listed in the "${OPEN_FINDINGS_HEADING}" section at the end of ${plan.url}, where the user decides on them in the approval dialog. Do not revise the plan for them and do not edit that section${last ? "" : ": any edit to the plan needs a new review"}. Run \`propose ${slugWord}\` to request approval.`,
					);
				else if (outcome === "approve-with-concerns")
					lines.push(
						`${plan.url} does not list these concerns: ${unlisted}. Tell the user about them, then run \`propose ${slugWord}\` to request approval.`,
					);
				else if (lastRound && listed)
					lines.push(
						`All ${MAX_ROUNDS} review rounds are used. The blockers and concerns are listed in the "${OPEN_FINDINGS_HEADING}" section at the end of ${plan.url}. Run \`propose ${slugWord}\`; the user decides on these findings.`,
					);
				else if (lastRound)
					lines.push(
						`All ${MAX_ROUNDS} review rounds are used. ${plan.url} does not list these findings: ${unlisted}. Tell the user about the blockers and concerns above, then run \`propose ${slugWord}\`; the user decides on them.`,
					);
				else
					lines.push(
						`Resolve every blocker and concern in the plan file, or state in the plan why one does not apply. Then run \`plan_review ${slugWord}\` again; \`propose\` stays blocked until a review finds no blockers in the current plan.`,
					);
				if (last && !changed)
					lines.push(
						`This was the last review round: \`propose\` now accepts only the text it reviewed. Do not edit ${plan.url} any more; after an edit, run \`plan_review ${slugWord}\`, which runs no reviewer, restores that text and lists your edit in the "${OPEN_FINDINGS_HEADING}" section, unreviewed.`,
					);
				return textResult(lines.join("\n"), { url: plan.url, reviewer, round, outcome, findings });
			} catch (error) {
				if (record.outcome === "error") record.reason = error instanceof Error ? error.message : String(error);
				throw error;
			} finally {
				record.endedAt = new Date().toISOString();
				const setting = await recordsSetting(pi, ctx.cwd);
				if (setting.warning) ctx.ui.notify(setting.warning, "warning");
				if (setting.records) await saveRecord(record, ctx);
			}
		},
	});

	pi.on("tool_call", async (event, ctx) => {
		const title = proposedTitle(event.toolName, event.input);
		if (title === undefined) return undefined;
		if (title === null)
			return {
				block: true,
				reason: "Plan review checks the plan before approval: run `propose <slug>` as a command of its own, with the slug as its only argument.",
			};
		const plan = await resolvePlan(title, ctx);
		// OMP's own proposal handler reports a missing plan file.
		if (!plan) return undefined;
		const hash = sha256(plan.content);
		const review = (await sessionReviews(ctx)).get(plan.url);
		const used = review !== undefined && review.rounds >= MAX_ROUNDS;
		if (review?.hash === hash && review.outcome !== "revise") return undefined;
		if (review?.hash === hash && used) {
			ctx.ui.notify(`Plan review: the last of the ${MAX_ROUNDS} review rounds still found blockers; the plan goes to approval with them open.`, "warning");
			return undefined;
		}
		const slugWord = commandWord(plan.slug);
		if (used)
			return {
				block: true,
				reason: `All ${MAX_ROUNDS} review rounds of ${plan.url} are used and the plan changed after the last one, so its current text has no review. Run \`plan_review ${slugWord}\`: no reviewer runs; the plugin restores the text that round reviewed and lists your later edits in the "${OPEN_FINDINGS_HEADING}" section. Then run \`propose ${slugWord}\` again.`,
			};
		const next = `run \`plan_review ${slugWord}\``;
		return {
			block: true,
			reason:
				review?.hash === hash
					? `Plan review requested changes (round ${review.rounds} of ${MAX_ROUNDS}) and the plan has not changed since. Resolve every blocker and concern in the plan file, or state in the plan why one does not apply, then ${next} again.`
					: `${review ? "The plan changed since its last review." : "This plan has not been reviewed."} Before proposing it, ${next} and follow its result, then run \`propose ${slugWord}\` again.`,
		};
	});
}
