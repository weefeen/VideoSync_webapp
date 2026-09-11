# docs/

Seven documents. They serve different purposes, and two of them are
deliberately records of things that went wrong.

| | What it is | Read it when |
|---|---|---|
| `history.md` | **Why the system is shaped this way**, with the measurement or the argument that settled each question — including the decisions that reverse a design document. Ends with the mistakes worth keeping. | Before re-opening anything that looks arbitrary. Most of it looks arbitrary and is not. |
| `status.md` | What is live, what is built and switched off, what is not built, what is waiting on a decision, and what the checks do not cover. | You are picking the work back up and need to know where it stands. |
| `infrastructure.md` | The machines, what listens on what, which credential lives where and where each must never reach, mail, backups, and what is deliberately absent. | Touching a server, or building the next one. |
| `deployment-log.md` | **A record, appended to, never tidied.** Every change made to a machine, in order, with what it was for and what it proved. A step that turned out to be wrong is worth more here than a clean account that hides it. | Something about a server looks odd and you want the written cause rather than a story somebody half remembers. |
| `broker-slice.md` | The design for splitting the worker out of the web process, with RabbitMQ between them. Says which parts are done and marks what is still open. | Before touching anything under `app/queue/`. |
| `queue-design.md` | The larger design this was cut from: stage-per-queue, two hosts, scale-to-zero, storage tiers, observability. Written first, and **since overtaken in several places.** | Planning what comes after the current slice. |
| `security-review.md` | What an attacker can do to a public, sign-in-free upload endpoint, and what stops them. | Before anything faces the public. Its top finding — a bot check — is still absent. |

## Which document wins

`queue-design.md` came first and is the wider plan. `broker-slice.md` is one
increment of it and departs from it deliberately in two places, listed in its
§12. `history.md` overrides both where it names the disagreement, because a
reversal there carries the evidence that forced it — decision D, the
scale-to-zero timing, and the render cap are all reversals of the design,
and the design still reads as originally written.

Do not resolve a disagreement by editing whichever one you happen to have
open. All of them are records of decisions, and a decision quietly reversed
in one file is how two documents come to describe two different systems.
Where a design document is now wrong, the reversal belongs in `history.md`
with its reason — not as a silent edit to the design.

One conflict is still unresolved rather than reversed: `queue-design.md` §10
deletes uploads after 48 hours, and uploads are being kept for training.
`status.md` lists it as a decision waiting.
