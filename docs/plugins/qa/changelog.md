# QA changelog

This page lists user-visible changes per release.

## 3.1.0: delivery plans as the planner's contract source

For a `last N commits` source, the planner reads every distinct delivery plan named by Git-parsed `Delivery-Plan:` trailers and associates each commit with its own plan, only after checking repository containment and rejecting traversal, symlinks and non-files; ignored trailers are disclosed in `## Changes Summary`. Prose mentions do not count as trailers. Plan text is specification data, not instructions. Plans written by 3.0.0 stay valid.

## 3.0.0: flagless `/qa:run`, config and account provisioning

**Breaking cutover:** `/qa:run` now owns the complete loop; `/qa:loop` is removed with no alias. `/qa:create-plan` remains optional plan authoring/review. Run `/qa:run` interactively once for config bootstrap, then commit `.av/config.toml`; prepare/pin it explicitly for headless runners. There are no invocation flags or per-fix `step` mode.

3.0.0 moved every flag into `.av/config.toml`; [Configuration](../../configuration.md) lists the current keys.

The old parsed Setup grammar (`Base URL`, `Required environment variables`, `Required databases`, `Required services`) is gone. Move those settings to `[env]`/`[qa]`; optional `## Setup` is human notes only. Plans written before 3.0.0 lack `Branch:`/`Head:`, so **pass their path once or regenerate** rather than expecting automatic branch reuse. Update credential tokens/targets and DB checks to the current config contract.

`auth-unverified` now applies only when the main flow lacks an engine-authenticated persona credential. Some former auth SKIPs become **FAIL flagged `auth`**, excluded from automatic fixing and visible for approval review. Fixers may not weaken auth. Headless default `approve` now tests/reports without fixing instead of requiring a TTY. Services may be brought up from trusted config, and accounts are freshly logged in before every tester dispatch. Config edits during a run stop it; run `/qa:run` again.

## Earlier releases (historical compatibility)

**`qa` 2.9.0:** In OMP, `qa:test-planner` runs with OMP's Advisor: a second model on the `advisor` role watches the planner while it writes the plan and can steer it, before `qa:test-plan-reviewer` reviews the result. Expect more model cost per plan. Switch it off with `task.agentAdvisor` (see [Oh My Pi](../qa.md#oh-my-pi)). The Claude Code edition is unchanged.

**`qa` 2.6.0 pairs with `code-review` ≥ 2.0.0 wherever a shared report carries a decision-stage rejection.** That is the precondition, and it is worth stating plainly: `**Fix-policy:** needs-decision` is emitted by `code-review`'s own producers alone — today, reports written by `/review` — while `/qa:run` and `/qa:loop` never write the field, and an absent field is `auto` by both fix commands' fail-safe. A report this plugin produces therefore cannot presently reach the decision gate, and cannot acquire a `🚫 Rejected` status or any of the loop-written decision fields. `qa` 2.6.0's handling of them is **forward compatibility** for a schema the QA producers do not yet emit.

Where the state does arise — a `/review` report fed through the decision stage and then re-rendered by this plugin — the pairing binds: `code-review` 2.0.0 adds a `🚫 Rejected` status to reports it shares with this plugin, and `/qa:loop` on `qa` ≥ 2.6.0 knows to read it as terminal and preserve the line. An older `/qa:loop` (< 2.6.0) does not: its Step 4.1 in-place Status update overwrites a `🚫 Rejected` line and its reason whenever a sibling issue passes on the same scenario in a later iteration, silently discarding the rejection. So keep both plugins on paired minimums (`code-review` ≥ 2.0.0, `qa` ≥ 2.6.0) for any report that can carry a rejection. This is milder than `code-review`'s own intra-plugin skew — an older `code-review` reader can silently re-offer and dispatch a rejected finding, which is worse, and which is unconditional rather than waiting on a producer that does not exist yet. See [code-review.md's Upgrade Notes](../code-review.md#upgrade-notes) for the fuller detail.
