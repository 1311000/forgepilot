# Review mode

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before changing the reading order, the coverage accounting, or claim validation**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## Review mode notes — read this before changing the ordering

The mode is aimed at a specific failure, not at summarisation. Research on code
review keeps finding that **position predicts how much gets read**: bugs in the
first file of a review are markedly more likely to be found than in the last, and
a change with many comments can still have uneven coverage — comment volume
*looks* like assurance. A narrative that only summarises makes this worse,
because it looks equally thorough whether or not it is.

So two things are the product:

- **The order is chosen, not alphabetical.** `schema → logic → api → config →
  tests → docs → noise`, so information arrives before it is needed; within a
  role, referenced-by-others first, then larger churn (spend the freshest
  attention on what matters). Each group carries `position_reason`, because
  ordering a reviewer cannot explain is ordering they will not trust.
  `test_the_reading_order_is_not_alphabetical` pins it — if that test ever passes
  trivially, the ordering has stopped doing anything.
- **`Coverage.unexamined_hunks` names the hunks *no claim cited*.** A tool that
  cannot say "I did not look at this" cannot be trusted when it implies it looked
  at everything. Noise is folded but **counted**, never deleted.

Other rules worth keeping:

- **Evidence is checked, not asserted.** `Claim.verified` starts `False` and only
  `validate.py` sets it, only for claims whose every `EvidenceRef` resolves — a
  session seq the log contains, a path in *this* change, a hunk in *this* diff.
  A failing claim moves to `ChangeNarrative.unverified` **with the reason**; it is
  never dropped. A long `unverified` list is a signal to the reviewer, and that
  is the intended reading.
- **The deterministic narrative is the floor, not a test fixture.** With no model
  configured the review still produces a real, checkable document — built from
  the diff, so evidenced by construction. A narrative that only worked with a
  model would be one nobody could check.
- **`tree-sitter` is an optional extra with a runtime probe.** See
  `pyproject.toml`: no cp314 Windows wheel, no abi3, no pure-Python fallback.
  `BlastRadius.backend` records which analysis actually ran, and the Markdown says
  so — a text search and an AST parse are not the same evidence and must not look
  alike. **The AST path here is written but unrun**; its test skips with a reason
  naming the platform issue.
- **`difflib` never emits `/dev/null`** — only git does. Creation and deletion are
  therefore also inferred from the hunk headers (`@@ -0,0 +1,N @@`). Keying solely
  on `/dev/null` meant rename detection silently never fired for diffs this
  project generates itself, which is every diff the task runner produces.

