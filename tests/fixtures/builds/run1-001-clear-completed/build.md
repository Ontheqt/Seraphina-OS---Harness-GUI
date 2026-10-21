STATUS: READY

# Build: Clear Completed Button

## Summary
Adds a `clearCompleted(items)` function to `src/todo.js`, imports it in `src/app.js`,
wires a "Clear completed" button click handler, updates `render()` to show/hide the
button based on whether any items are done, adds the button element to `index.html`,
and adds three new tests in `test/todo.test.js`.

Files changed:
- `src/todo.js` - added `clearCompleted` export
- `src/app.js` - imported `clearCompleted`, added `clearBtn` reference, visibility logic in `render()`, click handler
- `index.html` - added `<button id="clear-btn" type="button" hidden>Clear completed</button>`
- `test/todo.test.js` - imported `clearCompleted`, added three tests

## Acceptance criteria
1. [x] `node --check src/todo.js src/app.js` exits 0 with no output: ran, exit code 0, no output.
2. [x] `node --test test/todo.test.js` exits 0, all 7 tests pass (4 existing + 3 new for `clearCompleted`): all pass.
3. [x] `clearCompleted` is exported as a function: `node -e "import('./src/todo.js').then(m => { console.log(typeof m.clearCompleted); })"` printed `function`.
4. [x] `clearCompleted([{text:'a',done:true},{text:'b',done:false}])` returns `[{"text":"b","done":false}]`: verified with the node one-liner, output matched exactly.
5. [x] `index.html` contains `id="clear-btn"`: element present after `<p id="empty">` and before `</main>`.

## Validation
- `node --check src/todo.js src/app.js` — passed (exit 0, no output)
- `node --test test/todo.test.js` — passed (7/7 tests, exit 0)
- `node -e "import('./src/todo.js').then(m => { console.log(typeof m.clearCompleted); })"` — output: `function`
- `node -e "import('./src/todo.js').then(({clearCompleted})=>{ const r=clearCompleted([{text:'a',done:true},{text:'b',done:false}]); console.log(JSON.stringify(r)); })"` — output: `[{"text":"b","done":false}]`

## Log
### Round 1
Implemented the feature from scratch in one commit:
- Added `clearCompleted(items)` to `src/todo.js` using `Array.prototype.filter` to keep items where `done` is `false`, without mutating the input.
- Updated `src/app.js`: added `clearCompleted` to the import, grabbed `clear-btn` element, updated `render()` to toggle `hidden` on the button based on `items.some(item => item.done)`, and attached a click listener that calls `clearCompleted` then `render()`.
- Added `<button id="clear-btn" type="button" hidden>Clear completed</button>` to `index.html` after `<p id="empty">`.
- Added three `clearCompleted` tests to `test/todo.test.js` covering the all-incomplete, all-complete, and mixed cases.
