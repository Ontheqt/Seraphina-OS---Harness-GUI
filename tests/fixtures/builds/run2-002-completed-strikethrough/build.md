STATUS: READY

# Build: Completed items struck through in light grey

## Summary
Added visual distinction for completed todo items via strikethrough and grey colour. Files changed:
- `styles.css`: added `--completed: #c0c0c0` to `:root`, added `.done` rule with `text-decoration: line-through` and `color: var(--completed)`
- `src/app.js`: inside `render()`, adds class `done` to `<li>` when `item.done` is `true`

## Acceptance criteria
1. [x] Lint passes: `node --check src/todo.js src/app.js` exits 0 with no output.
2. [x] Tests pass: `node --test test/todo.test.js` exits 0, all 5 tests pass.
3. [x] `styles.css` contains `--completed: #c0c0c0` inside `:root`: confirmed by grep.
4. [x] `styles.css` contains a rule with both `text-decoration: line-through` and `color: var(--completed)`: `.done { text-decoration: line-through; color: var(--completed); }` confirmed by grep.
5. [x] When a todo item's checkbox is ticked, the `<li>` gets class `done`: `if (item.done) li.classList.add("done")` in `render()`.
6. [x] When the checkbox is unticked, the `<li>` loses the class: `li` is recreated fresh on each `render()` call; `classList.add("done")` is only called when `item.done` is `true`.

## Validation
- `node --check src/todo.js src/app.js` — passed (no output, exit 0)
- `node --test test/todo.test.js` — passed (5/5 tests, exit 0)
- `curl -s http://127.0.0.1:5120/styles.css | grep -E 'completed|line-through'` — returned both `--completed: #c0c0c0` and `.done { text-decoration: line-through; color: var(--completed); }`

## Log
### Round 1
Added `--completed: #c0c0c0` CSS variable to `:root` in `styles.css`. Added `.done` rule with strikethrough and grey colour. In `src/app.js` render(), added `if (item.done) li.classList.add("done")` before appending children to the `<li>`. All lint and tests pass.
