APPROVE

## Why
The change is 3 lines across exactly the two files the spec targets (styles.css and src/app.js), doing precisely what was asked: a `--completed` CSS variable, a `.done` rule with strikethrough and grey colour, and a single conditional `classList.add("done")` in `render()`. All four round-1 reviews are VERDICT: PASS and the notes they raise (accessibility contrast, strikethrough on the Remove button) are both explicitly acknowledged as product decisions in the spec.

## Acceptance criteria
1. Lint passes (`node --check src/todo.js src/app.js`): met — exits 0 with no output, verified by running the command.
2. Tests pass (`node --test test/todo.test.js`): met — 5/5 pass, exit 0, verified by running the command.
3. `styles.css` contains `--completed: #c0c0c0` inside `:root`: met — confirmed in the diff.
4. `styles.css` contains a rule with `text-decoration: line-through` and `color: var(--completed)`: met — `.done { text-decoration: line-through; color: var(--completed); }` confirmed in the diff.
5. Checked `<li>` gets class when `item.done` is true: met — `if (item.done) li.classList.add("done")` is the only addition in app.js.
6. Class absent when unticked: met — `<li>` is recreated fresh on every `render()` call; the class is only added when `item.done` is true.
