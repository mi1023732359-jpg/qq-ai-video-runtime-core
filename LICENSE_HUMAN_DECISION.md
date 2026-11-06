# License — Human Decision Record

QQ AI Video Runtime Core — Experimental Source Preview — v0.1

**Status: RESOLVED on 2026-11-06.**
**Date opened**: 2026-11-06 · **Date decided**: 2026-11-06

## Decision

| Item | Decision |
|---|---|
| Copyright holder | `Melarizan` (GitHub handle; public attribution) |
| Real name published | **No** — the maintainer's real name is deliberately not published |
| License | **MIT** |
| Copyright line | `Copyright (c) 2026 Melarizan` |
| AI-assisted development | Disclosed as required: **DeepSeek** and **OpenAI Codex**, in `README.md`, `CONTRIBUTING.md`, and `THIRD_PARTY_NOTICES.md` §7 |
| Decided by | Melarizan (project owner) |

`LICENSE` now exists with the MIT text and that exact copyright line.
`THIRD_PARTY_NOTICES.md` keeps third-party rights separate from the project's own
MIT grant.

---

> The remainder of this file is the **original decision memo as written before the
> decision**, preserved unchanged so the reasoning and the options that were weighed
> stay on the record. Text describing the state as "BLOCKED" below refers to the
> situation *before* 2026-11-06 and is not current status.

---

# Original memo (historical, pre-decision)

**Status at time of writing: BLOCKED. Nothing had been filled in.**
**Date**: 2026-11-06

This is a decision memo, not a license. At the time of writing, no `LICENSE` file
had been created, no copyright holder had been named, and no placeholder text had
been inserted anywhere in the tree.

The release candidate was otherwise ready for a preview; that was the only
remaining blocker, and it could not be resolved by tooling.

---

## 1. Why this is blocked

The release tree carries **no license**, which under default copyright means the
work is "all rights reserved". Publishing it without a license means others cannot
legally use, copy, modify, or redistribute it — which defeats the purpose of an
"Experimental Source Preview".

Three things must be true before a `LICENSE` file can be written:

| # | Prerequisite | Status |
|---|---|---|
| 1 | A named copyright holder (a person, a company, or a defined group) | **UNRESOLVED** |
| 2 | A chosen license | **UNRESOLVED** |
| 3 | A decision on how AI-assisted portions are attributed | **UNRESOLVED** |

---

## 2. Question 1 — Who is the copyright holder?

You must choose exactly one, and it must be a name you are willing to have printed
in every copy of the software.

| Option | Implication |
|---|---|
| You personally (real name or a pseudonym you control) | Simplest. A pseudonym is acceptable in most jurisdictions if it identifies an author. |
| A company / studio you own | Requires that the company actually holds the rights — e.g. an assignment or work-for-hire arrangement. |
| "The QQ AI Video Runtime contributors" | **Not recommended** unless a contributor agreement exists. It asserts rights over contributions you may not have recorded. |
| No one / stay unlicensed | The work remains all-rights-reserved; nobody may legally reuse it. |

**What is needed from you:** one exact string, e.g. `Jane Doe` or `Example Studio
Ltd.`. Until you supply it, the line stays unwritten.

### The AI-assistance fact

`video_runtime/audio_out.py` is explicitly annotated as implemented/reviewed with
an AI assistant, and other modules were developed with AI assistance. Some
jurisdictions require human authorship for copyright to subsist. This is a legal
question, not a technical one. Options practitioners commonly take:

- assert copyright in the work as a whole, treating AI output as a tool output
  that you selected, arranged, and edited;
- disclose the AI assistance in the repository;
- seek local legal advice before a commercial release.

**None of these has been chosen for you.** The audit (`LICENSE_FINAL_AUDIT.md` §5)
records the fact; the decision is yours.

---

## 3. Question 2 — Which license?

The three defensible candidates for a preview of this shape:

| License | Grants | Requires | Fits this project because | Risk |
|---|---|---|---|---|
| **MIT** | Use, copy, modify, distribute, sublicense | Keep the copyright + permission notice | Shortest, most permissive, easiest for others to adopt | No patent grant; no explicit trademark stance |
| **Apache-2.0** | Same as MIT, **plus an express patent grant** | Keep notice, state changes, include NOTICE if present | This code calls into Windows/COM/audio APIs where patents are plausible; the patent grant is a real benefit | Longer; slightly more friction for some users |
| **GPL-3.0 / AGPL-3.0** | Copyleft | Derivatives must stay open | Would prevent closed-source reuse | Discourages the plugin ecosystem the roadmap hints at; AGPL is overkill for a local tool |

**Recommendation (yours to accept or reject): Apache-2.0.** This project touches
patent-encumbered territory (audio capture, COM interop, process loopback) more
than a typical utility, and the express patent grant is the main thing MIT lacks.
If maximum adoption matters more than patent comfort, MIT is the alternative.

**Not recommended:** an "all rights reserved" notice presented as an open-source
release, or a license invented for the occasion.

---

## 4. Question 3 — Copyright line format

The line that would go in `LICENSE` and/or a header:

```text
Copyright (c) <YEAR> <HOLDER>
```

- `<YEAR>` — the year of first publication, or a range.
- `<HOLDER>` — the exact string from Question 1.

**Both are blank on purpose.** Do not fill them with `contributors`, `the
authors`, `Example`, or a placeholder — a wrong holder is worse than no license,
because it is a false statement of ownership that downstream users may rely on.

---

## 5. What happens after you decide

1. Create `LICENSE` in the release tree root with the chosen text and the exact
   holder line.
2. Optionally add a short `SPDX-License-Identifier:` line to each source file.
3. Update `README.md` §License and `CONTRIBUTING.md` §Read-this-first to point at
   the real license and to state real contribution terms.
4. Recompute the tree hash — the tree changes, so the hash recorded in the release
   manifest no longer describes the candidate.
5. Re-run the test suite and one more read-only review before any publication.

**None of steps 1–5 has been done.** They must not be done automatically.

---

## 6. Secondary decisions that are also open

These are not blocking the license itself but will be asked immediately after:

| Decision | Why it matters |
|---|---|
| Trademark/branding policy | "QQ", "Live2D", "OBS" are third-party marks used descriptively. A public release may want an explicit statement that forks must not imply affiliation. |
| Model-weights licensing | `faster-whisper` downloads weights with their own terms. Whether you document that, or pin a specific model, is a product decision. |
| Contribution terms | `CONTRIBUTING.md` currently says contributions cannot be accepted. Once licensed, a DCO or CLA choice is needed. |
| Recording-consent notice | The software transcribes another person's speech. A prominent consent notice may be a distribution requirement in some jurisdictions. |

---

## 7. Bottom line

- **Nothing has been decided for you.**
- **No `LICENSE` file exists**, and none should exist until Question 1 has an exact
  answer.
- The rest of the release package is complete: dependencies declared, privacy
  traced from the code, documentation unified, tests green, and an independent
  read-only review on record.

Bring back one string — the holder — and a license choice, and the final file can
be written in minutes.
