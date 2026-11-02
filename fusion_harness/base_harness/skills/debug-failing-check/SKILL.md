---
name: debug-failing-check
description: A check or test fails and the cause is not obvious
---
1. Run the check yourself and read the whole output. Find the FIRST failure, not the last.
2. Reproduce it with the narrowest command: one test, one input.
3. Form one hypothesis and test it with a print, an assertion or a minimal script before editing.
4. After the fix, run the narrow command, then the full check.
5. If two hypotheses in a row were wrong, re-read the task: the check may test something you
   misunderstood.
