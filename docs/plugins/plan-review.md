# Plan Review Plugin (Oh My Pi only)

Plan Review makes a second model review every plan written in OMP plan mode before it reaches the approval dialog. The agent gets the findings and fixes the plan until a review finds no blockers; the concerns that review leaves open go to approval in the plan file.

Plan Review is optional. Without it, plan mode proposes a plan directly: no other plugin in this marketplace calls `xd://plan_review` or waits for a review.

## Setup

Install the plugin and map the `advisor` model role in `~/.omp/agent/config.yml`. Pick a model from another family than your `plan` role, so the reviewer does not share the planner's blind spots:

```yaml
modelRoles:
  plan: anthropic/claude-fable-5-1:max
  advisor: openai-codex/gpt-6-astra
```

Without `modelRoles.advisor`, OMP resolves the role through `slow`. The role only picks the reviewer's model; OMP's Advisor runtime stays off unless you enable it (`advisor.enabled`).

OMP loads the plugin's extension when the `omp` process starts. After installing, disabling or uninstalling Plan Review, quit OMP and run `omp` again: `/new`, `/reload-plugins` and the in-session `/plugins` and `/marketplace` commands do not reload extensions, so the running process keeps reviewing plans until it exits.

## How a plan is reviewed

1. In plan mode the agent writes the plan file, then writes `{"title": "<slug>"}` to `xd://plan_review`.
2. Before each reviewer round, the plugin asks other plugins to check the plan. If a check rejects it, the call returns the problems without running a reviewer, using a round or changing the saved review state. Fix the problems in the plan file and call `xd://plan_review` again. Once the checks pass, the plugin runs `omp -p` on the `advisor` role with the `read`, `grep` and `glob` tools, without extensions or skills. The reviewer checks the plan against the repository and returns findings marked `blocker`, `concern` or `nit`. While it runs, the review shows its round and the elapsed time, updated every 15 seconds.
3. A blocker requests changes: the agent resolves every blocker and concern of that review in the plan file, or states in the plan why one does not apply, and runs the review again. A review without blockers approves the plan; its result reads `approved with open concerns` when it reported concerns, and `approved` when it reported only nits or nothing.
4. The plugin lists the concerns of a review without blockers in a `## Open review findings` section at the end of the plan file, so the approval dialog shows them and you decide on them when you approve. When Delivery runs the approved plan, the implementer and the reviewer of every task receive them as well, and Delivery applies those about the plan's `## Verification` that hold to its checks, or prints them as `manual` checks when they need a project file or a step outside the delivery ([Delivery](delivery.md#plan-review-findings)). The agent proposes the plan without revising it for them. Writing the section needs no new review, and every review removes the previous section before the reviewer reads the plan, so the section always belongs to the latest review. The plugin writes only plan files stored in the session's `local://` directory, where plan mode keeps its plans, and leaves a plan alone if it changed while the reviewer ran; then the agent reviews the current text again, unless that was the last of the 2 rounds. For a plan file stored elsewhere, or when writing the section fails, the agent reports the findings to you instead. If the plan changed while the last round's reviewer ran, the plugin lists the findings when it restores the reviewed text (step 5).
5. Writing the slug to `xd://propose` is blocked until a review finds no blockers in the current plan text. Until the 2 rounds are used, any edit after a review, including an edit of the `## Open review findings` section, needs another review. After the last round, `xd://propose` accepts only the text that round reviewed, with the findings section the plugin wrote; it warns when that round still found blockers.

   If the agent edits the plan after the last round, `xd://propose` is blocked. Calling `xd://plan_review` again runs no reviewer: it restores the reviewed text and appends each contiguous change to the plan body as an `[edit]` list item at the end of `## Open review findings`. Each item names the plan section and includes an `@@` hunk header, removed lines (`-`) and added lines (`+`). The approval dialog shows these edits as unreviewed, and Delivery passes them to its agents as findings. Only the plugin writes the findings section; edits to that section are undone without an `[edit]` item. The plugin never writes a plan outside the session's `local://` directory: it returns the same edit items and tells the agent to undo the edits before proposing the plan. If restoring a local plan fails, the agent must undo its edits too.

A review round takes minutes: about 7 minutes (median) in our sessions with `openai-codex/gpt-6-astra`. The reviewer stops after 15 minutes.

Every reviewer run of a plan file is a round, including a run that cannot finish. After 2 rounds of one plan file, the reviewer no longer checks it (step 5). When the last round still found blockers, the plugin lists its blockers and concerns in the `## Open review findings` section, and you decide on them in the approval dialog. One plan waits at most 2 × 15 minutes for reviews. If a review cannot run (no `omp` on `PATH`, a reviewer error, an answer that is not the expected JSON, or 15 minutes without an answer), the plugin warns and lets that plan text through unreviewed.

To skip a running review, press Esc. These other interrupts of the running turn skip it the same way: Enter on an empty prompt while messages are queued, leaving plan mode with `/plan`, a collab guest's interrupt, an RPC `abort` or ACP cancel, and another extension's abort. The plugin warns that the plan goes to approval unreviewed; ask the agent to propose it. A skipped review does not use a round, and any edit to the plan after it needs a review again.

The plugin saves each plan file's review state in `plan-review-state.json` in the session's artifacts directory, which is the session file's path without `.jsonl`: used rounds, the hash of the text covered by the last review and accepted by the gate, its outcome and that text. Restarting OMP to resume the session, `/resume` and a fork (which copies the artifacts) keep the used rounds; a new session starts without them. A session without a session file keeps state only in the OMP process. If saved state cannot be read, the plugin warns and the session's plans get fresh rounds; invalid entries are skipped and get fresh rounds. If state cannot be saved, it warns and keeps the state until OMP exits. Review state never goes to the repository.

With Delivery installed, its task check runs before every reviewer round for `local://` plans in a git repository. It checks the same violations that Delivery's `xd://propose` gate blocks, including missing file lists. A rejected plan gets no review and uses no round until the agent fixes it, so even the last round reviews a plan that passes Delivery's check. If the router fails, Delivery adds no problem and the review proceeds. The `## Open review findings` section does not change the task check: it comes after the last task, and a task ends at the next `##` heading.

## Review records

Review records are for contributors who work on Plan Review: they feed its eval and the measurement of its cost. They are off by default. Turn them on in your user file `${XDG_CONFIG_HOME:-~/.config}/av/config.toml`, which applies to every repository:

```toml
version = 1

[plan-review]
records = true
```

`records` is the only key of the `[plan-review]` table: `true` or `false`, `false` by default. A repository's `.av/config.toml` can set it too, and `.av/local.toml` overrides it when one of those files has the table ([override resolution](../configuration.md#files)). The plugin reads the setting at every `xd://plan_review` call through the shared configuration loader, which needs `python3` 3.11 or newer; without it, records stay off. An invalid `[plan-review]` table turns records off with a warning. In every case the review result and the `xd://propose` gate stay as they are.

With records on, every `xd://plan_review` call saves one JSON record in the session's artifacts directory, which is the session file's path without `.jsonl`: `<artifacts directory>/plan-review/<start time>-<8 hex characters>.json`, for example `2026-11-13T09-41-07.123Z-1a2b3c4d.json`. The names sort in start order. The plugin saves the record when the call ends, whatever its result, so a session has one record per call: reviews, reviews that could not run, plans rejected by another plugin's check, skipped or cancelled runs, calls after the 2 rounds are used, and calls that failed. Records never go to the repository.

| Field | Content |
|-------|---------|
| `version` | The plugin version; `null` when its `package.json` cannot be read |
| `title` | The title the agent wrote to `xd://plan_review` |
| `url` | The plan file; `null` when no plan file was found |
| `round` | The round of the reviewer run; `null` when no reviewer ran, including a `rejected` call. A skipped run uses no round, so the next run has the same number |
| `startedAt`, `endedAt` | Start and end of the call, ISO 8601 in UTC |
| `outcome` | `approve`, `approve-with-concerns` or `revise` when the reviewer answered; `unavailable` when the review could not run; `rejected` when another plugin's plan check rejected the plan before a reviewer ran, using no round; `skipped` when you interrupted it; `cancelled` for any other abort; `exhausted` when the 2 rounds were used and no reviewer ran, including a call that restored the reviewed text; `error` when the call failed, for example without a plan file |
| `reason` | Why the review was `unavailable`, the problems that made a call `rejected`, or why the call ended with `error`; otherwise `null` |
| `reviewer` | The `<provider>/<model>` that answered; `null` without an answer |
| `plan` | `{"text", "sha256"}`: the plan text quoted to the reviewer and its SHA-256, or the text that would have been sent when the call was `rejected`. For a plan file in the session's `local://` directory, the text has no `## Open review findings` section. `null` when no reviewer ran for another reason. The reviewer received that plugin version's reviewer prompt, then `Plan (<url>):` and the text between `<plan>` tags |
| `answer` | The reviewer's final answer as written, before parsing; `null` without one |
| `findings` | The findings parsed from the answer; `null` when the answer was not the expected JSON |
| `usage` | The `usage` object of every assistant message the reviewer finished, from the `message_end` events of its JSON output, in order and as OMP reports it: one per model turn, turns before a recovery or continuation included. Add them up for the round's tokens and cost. `null` when the output has no finished assistant message |
| `proposeSha256` | The SHA-256 the call stored for the `xd://propose` gate: the hash of the plan text this call reviewed, plus the `## Open review findings` section the plugin wrote after the review. `null` when the outcome is `cancelled`, `rejected` or `error`, when an `exhausted` call found the plan unchanged or could not restore it, or when the plugin found the plan changed while the reviewer ran, which it checks only before writing open findings. For an `exhausted` call that restored the plan, this is the hash of the restored text with any `[edit]` items. A non-null hash need not match the plan file's current text. The gate lets a plan through when its current text has the most recently stored hash and the outcome is not `revise`; after the 2 rounds, it still requires that hash, regardless of outcome |

A record holds the whole plan text and the reviewer's answer, and both can quote the repository's code: share a session's artifacts only where you would share that code. A fork of the session copies its artifacts, records included.

If a record cannot be saved, for example in a session without a session file, the plugin warns; the review result and the `xd://propose` gate stay as they are. The plugin never reads records back; it keeps its review state in `plan-review-state.json` (see [How a plan is reviewed](#how-a-plan-is-reviewed)).
