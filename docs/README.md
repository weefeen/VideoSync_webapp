# docs/

Four documents. They serve different purposes and one of them is deliberately
a record of things that went wrong.

| | What it is | Read it when |
|---|---|---|
| `deployment-log.md` | **A record, appended to, never tidied.** Every change made to a machine, in order, with what it was for and what it proved. A step that turned out to be wrong is worth more here than a clean account that hides it. | Something about a server looks odd and you want the written cause rather than a story somebody half remembers. Or you are building the next machine. |
| `broker-slice.md` | The design for splitting the worker out of the web process, with RabbitMQ between them. Says which parts are done and marks what is still open. | Before touching anything under `app/queue/`. |
| `queue-design.md` | The larger design this was cut from: stage-per-queue, two hosts, scale-to-zero, storage tiers, observability. Written first, and **`broker-slice.md` deviates from it in two places, listed there in §12.** | Planning what comes after the current slice. |
| `security-review.md` | What an attacker can do to a public, sign-in-free upload endpoint, and what stops them. | Before anything faces the public. Its top finding — a bot check — is still absent. |

## The rule about the two design documents

`queue-design.md` came first and is the wider plan. `broker-slice.md` is one
increment of it and departs from it deliberately in two places. Where they
disagree, `broker-slice.md` wins for the current slice and says why.

Do not resolve a disagreement by editing whichever one you happen to have
open. Both are records of decisions, and a decision quietly reversed in one
file is how two documents come to describe two different systems.
