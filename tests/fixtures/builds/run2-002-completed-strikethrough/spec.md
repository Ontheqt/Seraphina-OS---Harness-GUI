# Completed items struck through in light grey

> Feature: Show completed items struck through in light grey (#c0c0c0)

## Context

The todo list renders items in `src/app.js` (`render()`, line 9-23). Each item
is a `<li>` containing a `<label>` with a checkbox and the item text. When a
checkbox is toggled, `toggleItem` from `src/todo.js` flips `item.done` and
`render()` is called again.

Currently, `render()` does not set any class or style on completed items; there
is no visual distinction between done and not-done items.

Styling lives in `styles.css`. Design tokens are the `:root` variables. The
colour `#c0c0c0` is not yet defined as a token.

## Requirements

1. Add a CSS variable `--completed` with value `#c0c0c0` to the `:root` block
   in `styles.css`.
2. Add a CSS rule in `styles.css` targeting a class (e.g. `.done`) that sets
   `text-decoration: line-through` and `color: var(--completed)`.
3. In `src/app.js`, inside `render()`, when building each `<li>`, add that
   class to the `<li>` element when `item.done` is `true`, and omit it when
   `item.done` is `false`.
4. No other files or behaviour may change.

## Acceptance criteria

1. Lint passes with no errors:
   `node --check src/todo.js src/app.js`
   Expected: exits 0 with no output.

2. Tests pass with no failures:
   `node --test test/todo.test.js`
   Expected: exits 0, all tests pass.

3. `styles.css` contains `--completed: #c0c0c0` inside the `:root` block.

4. `styles.css` contains a rule with both `text-decoration: line-through` and
   `color: var(--completed)`.

5. When a todo item is added and its checkbox is ticked, the `<li>` element in
   the rendered list has the class used in the CSS rule defined in requirement 2.

6. When the same item's checkbox is unticked, the `<li>` element no longer has
   that class.

## Out of scope

- Changing the checkbox appearance or any other visual property of completed
  items.
- Filtering or hiding completed items.
- Persisting state to localStorage or any other storage.
- Animating the transition.
- Adding new test cases (the feature is purely presentational; no logic in
  `src/todo.js` changes).

## Assumptions

- The class name applied in `app.js` and targeted in `styles.css` can be any
  reasonable name (e.g. `done`); the spec does not mandate a specific name as
  long as both files use the same one. **(product)**
- The strikethrough and grey colour apply to the full `<li>` row (including the
  label text), which is the simplest implementation using the existing markup.
  Scoping the style to only the text inside the label is not required. **(product)**

## Risk

None.
