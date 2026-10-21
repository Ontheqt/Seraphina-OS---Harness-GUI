ESCALATE

## Why

The change deliberately adds `left-pad` as a new runtime npm dependency (updated `package.json`, new `package-lock.json`, installed `node_modules`). Adding a dependency is a hard escalation trigger regardless of how intentional or well-reviewed the change is. All four round-2 reviews pass and the implementation is technically sound, but a human must sign off on the new dependency before it merges automatically.

## Acceptance criteria

1. `node --test test/todo.test.js` exits 0 with all 8 tests passing: met — confirmed by running directly in the worktree (# fail 0).
2. `node --check src/todo.js src/app.js` exits 0: met — stated in build.md and confirmed by review-code.md round 2.
3. `node -e "import('left-pad').then(m => console.log(m.default(7, 2, '0')))"` prints `07`: met — confirmed by running directly in the worktree.
4. `package.json` contains `"left-pad": "^1.3.0"` in `dependencies`: met — visible in the diff.
5. Items display `01`, `02`, `03` etc. in the browser: met — src/app.js calls `leftPad(i + 1, 2, "0")` and the importmap routes the browser to `./src/left-pad-esm.js`.
6. Item 10 is labelled `10` not `010`: met — `leftPad(10, 2, "0")` returns `"10"` (no extra padding when string is already at target length).

## For the human

The only reason for escalation is the new npm dependency. Please confirm:

- Is `left-pad` an acceptable runtime dependency for this project? (The spec explicitly requires it and it is deprecated upstream in favour of `String.prototype.padStart()`; the security reviewer noted this is low risk but flagged it.)
- Is `src/left-pad-esm.js` (a hand-inlined copy of the CJS package logic used as a browser ESM shim) acceptable, rather than a direct re-export of the installed package?

Files to look at: `package.json`, `package-lock.json`, `src/left-pad-esm.js`, `src/app.js`, `index.html`.
