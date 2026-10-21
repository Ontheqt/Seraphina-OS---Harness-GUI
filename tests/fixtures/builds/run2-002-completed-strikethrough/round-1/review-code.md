VERDICT: PASS

## Checks
node --check src/todo.js src/app.js: pass
node --test test/todo.test.js: pass (5/5)

## Findings
None.

## Notes
The change is minimal and clean. `--completed: #c0c0c0` is added to `:root`, the `.done` rule correctly applies `text-decoration: line-through` and `color: var(--completed)`, and `render()` conditionally adds the class when `item.done` is true. Since `<li>` elements are recreated on each render, removing the class on untick is handled implicitly. No unrelated files are touched.
