> **OMP edition — generated file, do not edit.** Source of truth: `plugins/{plugin}/{relpath}`; regenerate with `python3 scripts/build_omp_edition.py`.
>
> The instructions below were written for Claude Code. In this harness, read their tool references as follows:
>
> - **Task tool** with `subagent_type: "<plugin>:<agent>"` → call `task` with `agent: "<plugin>:<agent>"` (the id is unchanged) and the prompt as the item's `task`. `run_in_background` has no equivalent: `task` runs asynchronously and results are delivered when agents finish. "Dispatch in parallel" means one `task` call with several items.
> - **TaskCreate / TaskUpdate / TaskList** → the `todo` tool: `init` with the listed subjects, `start` / `done` by subject text, `view` to list. `activeForm` has no equivalent. A subagent has no `todo` tool: when running as one, skip these progress-tracking steps and do the work they announce.
> - **AskUserQuestion** → the `ask` tool. `multiSelect: true` → `multi: true`.
> - **Skill tool**, `Skill(skill: "<name>")`, or a skill cited as `<plugin>:<name>` → `read skill://<name>` (skills are addressed by name, without the plugin prefix).
> - **WebSearch** → `web_search`. **WebFetch** → `read` on the URL.
> - **allowed-tools** and `Bash(<cmd>:*)` grants are Claude Code permission pre-approvals. They grant and restrict nothing here.
