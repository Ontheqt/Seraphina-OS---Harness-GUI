APPROVE

## Why
The change adds exactly what the feature asked for: a `clearCompleted` function in `src/todo.js`, wired to a "Clear completed" button in `index.html` via `src/app.js`, with three new tests covering all required cases. All four round-1 reviews return `VERDICT: PASS` with no noted problems, and my own run of `node --test test/todo.test.js` confirmed 7/7 tests pass.

## Acceptance criteria
1. `node --check src/todo.js src/app.js` exits 0 with no output: met - confirmed by build.md and consistent with the clean syntax in the diff.
2. `node --test test/todo.test.js` exits 0, all tests pass including 3 new ones: met - I ran this directly and got 7/7 pass.
3. `clearCompleted` exported as a function: met - implemented as a named export in `src/todo.js`.
4. `clearCompleted([{text:'a',done:true},{text:'b',done:false}])` returns `[{"text":"b","done":false}]`: met - the filter logic `!item.done` correctly produces this result, verified by the mixed-case test passing.
5. `index.html` contains `id="clear-btn"`: met - the diff shows `<button id="clear-btn" type="button" hidden>Clear completed</button>` added after `<p id="empty">`.
