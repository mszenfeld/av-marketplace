---
name: simple-language
description: Use when writing any text a human will read: a reply, task summary, explanation, report, plan, spec, or README. Applies to short and technical answers too.
---

# Simple Language

## Overview

A correct answer the reader cannot scan is a bad answer.

This skill governs the **shape** of prose: order of information, sentence length, allowed words. It does not decide tone or how much to say. It does not lower the quality of the message.

Prose means sentences, lists, headings, and emphasis. Code blocks, commands, quoted text, error output, and log lines are not prose and stay verbatim.

Replies are in the user's language. Documents are in the language the user asks for.

## Reply Shape

Every reply has this order:

1. **First line = the answer.** A verdict, a status, a number, or the question you must ask. Not context. Not what you did to get there.
2. **Details** as a list or a table.
3. **Caveats and next steps** last, in their own block.

The reply ends after the last block. No closing offer of more help.

A reply longer than one screen gets `##` headings. One topic per heading.

## Sentence Rules

| Rule | Instead of | Write |
|---|---|---|
| One sentence = one idea, up to ~15 words. A condition and its result are one idea. | "Since the portfolio never sells, the valuation never recovers, so the mode stays active longer than intended." | "The portfolio never sells. The valuation never recovers. The mode stays active longer than intended." |
| Definition as its own sentence, not a parenthesis | "Win rate (the share of profitable trades) says nothing about profitability." | "Win rate says nothing about profitability. Win rate is the share of profitable trades." |
| Positive statement, not double negation | "It is not impossible that this fails." | "This may fail." |
| Active voice | "Selling was paused by the breaker." | "The breaker pauses selling." |
| The concrete name, not a pronoun | "That file does not handle it." | "`targets.py` does not handle it." |
| Quantities and results as digits | "four thousand eight hundred tests" | "4800 tests" |
| One word for one thing | "breaker", then "fuse", then "the mechanism" | always "breaker" |

## Words

- **One name per thing, every time.** Once you have named something, repeat that name. Do not swap in synonyms for variety. This is not literary prose. Varied wording reads as a second thing.
- **No team slang.** Do not coin words or mix languages: "the ancestor bump", "kicks the flow". Say what happens: "updates `updated_at` on the parent rows", "starts the workflow".
- **No undefined jargon.** If a technical term is necessary, define it in one sentence at first use. Then use only that one term.
- **The code name, not a shorthand.** A field or method name in backticks is unambiguous. "The ancestor bump" is not.
- **Check:** would someone outside this conversation understand the word without asking? If not, use a plain word or a name from the code.
- The same rules apply to logs, commit bodies, and test names.

## Lists

- A list item is **one short sentence** or a fragment. Not two clauses joined by an em dash or a semicolon.
- If an item needs a reason, split it: short item, then the reason on an indented line below it.
- One list type per section. Bulleted **or** numbered, not both mixed.
- Numbered only when order matters.
- Up to 7 items. More than that: split into sections.

## Emphasis

- **Bold on 1-2 words** that carry the meaning: the verdict, the number, the name. A run-in label at the start of a list item may also be bold.
- At most one bold per paragraph or list item.
- File names, functions, and values in `backticks`.
- A prose paragraph is at most 3 sentences. Longer: turn it into a list.

## Documents

Plans, specs, reports, and READMEs written by the agent follow the same shape. A document is a long reply.

If a skill or template fixes the section order, keep that order. These rules apply inside each section.

- **Outcome first.** The paragraph right after the title states what the document decides, or what the reader will have after following it.
- **Decision before justification.** State the choice in one sentence. Then the reasons, as a list.
- **One topic per section.** A second topic gets a second heading.
- **Terms.** Define each term at first use. More than 3 terms: a "Terms" table near the top instead.
- **Steps.** One action per step. Configuration and reasoning go on indented lines below the step.
- **Tests, risks, and open questions** get their own section at the end.

## Before / After

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

## Final Check

Before sending, scan for these. Any hit: rewrite.

- First line gives context instead of the answer
- A sentence with two or more commas separating clauses
- A parenthesis with an explanation inside a sentence
- An em dash or a semicolon joining two clauses in a list item
- A paragraph longer than 3 sentences
- A technical term not defined at first use
- The same thing called by two different words
- A closing offer of more help

## When Not To Apply

When the user asks for continuous prose: an essay, an article, a letter.
