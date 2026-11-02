---
name: escalate
description: You think you cannot continue without a person
---
# Before you ask
A person's attention is the scarcest resource in this loop. Ask only when one of these holds:
- missing_access: a credential, permission or account you do not have.
- missing_info: information that exists nowhere you can reach. Search the workspace, docs, git
  history and configuration first.
- decision: a choice with real consequences for users or the product, where the task gives no
  direction and no existing convention decides it.
- external: something outside your control is broken, such as a service that is down.

Not reasons to ask: a failing test you have not investigated, a choice an existing convention
settles, a reversible default nobody would object to, or permission for work the task already
asks for.

# How to ask
Call `finish` with status=need_human and:
- question: ONE concrete question whose answer unblocks you, answerable without reading your
  work. Good: "Should the deploy script use the staging or the production API key?"
  Bad: "How should I proceed?"
- category: one of the four above.
- tried: what you already checked, in one or two lines.
- summary: one sentence on where the task stands.
Keep useful partial work in the workspace so the task resumes where it stopped.
