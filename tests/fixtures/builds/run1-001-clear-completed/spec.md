# Clear Completed Button

> Feature: Add a "Clear completed" button that removes all checked items; put the logic in src/todo.js with a test

## Context

The app is plain HTML/CSS/JS using ES modules with no dependencies. Pure list
logic lives in `src/todo.js` and is imported by `src/app.js`, which wires
everything to the DOM in `index.html`. Tests run with `node --test` against
`test/todo.test.js`.

Current functions in `src/todo.js`:
- `addItem(items, text)` - returns new array with a trimmed item appended
- `toggleItem(items, index)` - flips the `done` flag at the given index
- `removeItem(items, index)` - removes the item at the given index

`src/app.js` imports those three functions, holds a module-level `items` array,
and calls `render()` after every mutation. The `render()` function rebuilds the
`<ul id="list">` and hides/shows `<p id="empty">`.

`index.html` currently has: an `<h1>`, a submit form (`id="add-form"`), `<ul
id="list">`, and `<p id="empty">`. Styling comes from the CSS design tokens
(`--accent`, `--space`, `--radius`, etc.) in `styles.css`.

## Requirements

1. Add an exported function `clearCompleted(items)` to `src/todo.js` that
   returns a new array containing only items whose `done` property is `false`.
   It must not mutate the input array.
2. Import `clearCompleted` in `src/app.js` alongside the existing imports.
3. Add a "Clear completed" `<button>` in `index.html`, placed after `<p
   id="empty">` and before the closing `</main>`. Give it `id="clear-btn"` and
   `type="button"`.
4. In `src/app.js`, wire the button: clicking it calls `clearCompleted`,
   replaces `items`, and calls `render()`.
5. The button must be hidden (`hidden` attribute) when there are no completed
   items, and visible when at least one item has `done: true`. Update this
   visibility inside `render()`.
6. Add tests for `clearCompleted` in `test/todo.test.js` following the existing
   `node:test` / `node:assert/strict` pattern. Cover at minimum:
   - all items incomplete: returns the full array unchanged in content
   - all items complete: returns an empty array
   - mixed: returns only the incomplete items

## Acceptance criteria

1. Running `node --check src/todo.js src/app.js` exits with code 0 and no
   output.
2. Running `node --test test/todo.test.js` exits with code 0 and all tests
   pass, including at least three new tests for `clearCompleted`.
3. `src/todo.js` exports a function named `clearCompleted`. Check with:
   `node -e "import('./src/todo.js').then(m => { console.log(typeof m.clearCompleted); })"` — output must be `function`.
4. Calling `clearCompleted([{text:'a',done:true},{text:'b',done:false}])` returns
   `[{text:'b',done:false}]`. Verify:
   `node -e "import('./src/todo.js').then(({clearCompleted})=>{ const r=clearCompleted([{text:'a',done:true},{text:'b',done:false}]); console.log(JSON.stringify(r)); })"` — output must be `[{"text":"b","done":false}]`.
5. `index.html` contains an element with `id="clear-btn"`.

## Out of scope

- Persisting the todo list to `localStorage` or any other storage.
- Filtering or showing a separate "completed" view.
- Animations or transitions on removal.
- Any change to existing `addItem`, `toggleItem`, or `removeItem` functions.
- Adding a count label or badge alongside the button.

## Assumptions

- The button is rendered unconditionally in the HTML but toggled via the
  `hidden` attribute in `render()`, matching the pattern already used for `<p
  id="empty">`. **(product)**
- "Removed" means permanently deleted from the `items` array, not archived.
- The button label is exactly "Clear completed" (sentence case, no icon).
- No new CSS class is needed; the button inherits the existing `button` rule
  from `styles.css` (accent-coloured, rounded).

## Risk

None.
