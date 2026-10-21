ESCALATE

## Why
The change adds `left-pad` as a new production dependency, which is an automatic escalation trigger. All acceptance criteria are met and all round-2 reviews pass with no blocking findings, but a human must sign off on the introduction of any new external dependency.

## Acceptance criteria
1. `node -e "import('left-pad').then(m => console.log(m.default(7, 3)))"` prints `  7`: met — verified by running the command; output is `  7`
2. `package.json` contains `"dependencies"` with `"left-pad"`: met — `{"left-pad":"^1.3.0"}` present
3. `node --test` passes with no failures: met — 5/5 pass, verified by running tests
4. `node --check src/todo.js src/app.js` exits 0: met — confirmed
5. `grep "left-pad" src/app.js` returns a match: met — `import leftPad from "left-pad";` present
6. `render()` uses `leftPad(i + 1, 3)` in the label: met — number is placed in a `<span style="white-space: pre">` so leading spaces are preserved
7. `AGENTS.md` "Setup for a fresh checkout" contains `npm install`: met — confirmed in diff

## For the human
The sole reason for escalation is the addition of a new external dependency (`left-pad@1.3.0`). The implementation is otherwise complete and clean.

Please confirm:
- You accept `left-pad@1.3.0` from `registry.npmjs.org` (WTFPL licence, no transitive dependencies, sha512 integrity hash present in `package-lock.json`) as the project's first runtime dependency.
- The **(product)** assumption in `spec.md` — using a fixed pad width of 3 — is acceptable.

Files to review: `package.json`, `package-lock.json`, `spec.md` (Assumptions section).
