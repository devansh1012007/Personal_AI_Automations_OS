# Note — three dispatches that carried their brief in the prompt, not in a file

The protocol says a brief is written to `briefs/<AGENT-ID>.md` before the
dispatch, so a retry or a recovery agent gets exactly the same context. Three
wave-1 dispatches did not follow it, and the record says so rather than
pretending otherwise:

| Agent | What happened | Consequence |
|---|---|---|
| `W1-IMPR-04` | The brief was **never written**; the agent was pointed at `briefs/W1-IMPR-04.md` and found it absent. It derived its scope from the delta review and **declared its write-set in COMMS** — which is why the work is still attributable. | None to the code. The brief was reconstructed from the delta review afterwards. |
| `W1-IMPR-05` | Same — brief in the prompt only. | None. |
| `W1-IMPR-06` | The brief was **never written**; the first dispatch pointed at a file that did not exist and **returned nothing at all** — no commit, no report, no COMMS line. The brief was then written and the task re-dispatched, which landed. | A wasted dispatch. The rule exists for exactly this reason. |

**The substantive record for all three is the report**, not the brief:
`reports/w1-improve/W1-IMPR-04-delta.md`, `W1-IMPR-05-lock.md`,
`W1-IMPR-06-repair.md`, and the findings they act on in
`reports/w1-review/W1-DREV-04-delta.md`.

**What the protocol's rule is for:** an agent that cannot find its brief
cannot know its write-set, its acceptance criteria or its stop conditions —
and an agent that guesses them is an agent that guesses. Two of the three
guessed well and declared what they assumed; the third produced nothing,
which is the honest outcome when the context is missing.

**The rule for the rest of this run: the brief file exists before the
dispatch, or the agent does not run.**
