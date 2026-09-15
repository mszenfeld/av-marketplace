# Simple Language Plugin

Make the agent write text that people can scan: the answer first, one idea per sentence, no undefined jargon.

**Version:** 1.0.0

## Why

Many readers scan text rather than read it line by line: people with dyslexia, non-native speakers, anyone reading on a phone or under time pressure. A correct answer buried in a dense paragraph costs them the most.

This plugin does not shorten or dumb down the message. It changes its **shape**: order of information, sentence length, allowed words.

## How It Works

| Component | Purpose |
|-----------|---------|
| `simple-language` skill | The rules: reply shape, sentence rules, words, lists, emphasis, documents |
| `SessionStart` hook | Injects the rules into the session context at startup |

The hook runs every time a session starts, resumes, is cleared, or is compacted. From the first reply, the agent follows the rules without being asked. The rules survive context compaction, because the hook re-injects them.

## What Changes

**Replies**

- First line is the answer: a verdict, a status, or a number.
- Details as a list or a table.
- Caveats and next steps last, in their own block.
- Sentences up to ~15 words, one idea each.
- No definitions in parentheses. A definition is its own sentence.
- Technical terms are defined at first use.
- One name per thing, repeated every time. No synonyms for variety.

**Documents written by the agent**

Plans, specs, reports, and READMEs follow the same shape:

- The paragraph after the title states the outcome.
- When another skill or template fixes the section order, that order wins. The rules apply inside each section.
- Decision first, reasons as a list below it.
- One topic per section.
- Terms defined once, in a table when there are more than 3.
- Tests, risks, and open questions in their own section at the end.

**Unchanged**

- Code, commands, and file contents.
- Quoted text, error output, and log lines.
- Replies where the user asks for continuous prose: an essay, an article, a letter.

## Example

Before:

> I implemented the `DISCRETIONARY_SELL_CODES` constant and the `_freeze_discretionary_sells` method in `DecisionEngine`, so that defensive mode now also covers discretionary sells, and the tests pass.

After:

> **Done.** Defensive mode now also freezes discretionary sells.
>
> - New constant `DISCRETIONARY_SELL_CODES` in `DecisionEngine`
> - `stop_loss`, `score_below_threshold`, `rank_exit` now map to `HOLD_DEFENSIVE`
> - Forced sells still go through
>
> Tests: **4864** green, coverage 92.18%.
