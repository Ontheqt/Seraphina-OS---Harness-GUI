# Left-pad item numbers

> Feature: Add a dependency on left-pad and use it to pad item numbers

## Context

The app is a plain HTML/CSS/JS todo with no runtime dependencies (`package.json`
is `{ "type": "module", "private": true }`). Two source files drive behaviour:

- `src/todo.js` — pure logic (`addItem`, `toggleItem`, `removeItem`), tested in
  `test/todo.test.js` using `node --test`.
- `src/app.js` — DOM wiring; the `render()` function builds `<li>` elements from
  the `items` array. Item numbers (1-based position `i + 1`) are not currently
  shown in the UI.

AGENTS.md currently states "no dependencies" as a convention. This feature
intentionally contradicts that convention; the convention line must be updated.

## Requirements

1. Add `left-pad` as a production dependency in `package.json` (install it with
   npm so `node_modules/left-pad` is present and the package is listed under
   `"dependencies"`).
2. Import `leftPad` from `left-pad` in `src/app.js`.
3. In `render()` in `src/app.js`, prepend each list item's visible label with
   its 1-based position number, left-padded with spaces to a total width of 3,
   followed by a period and a space (e.g. `"  1. Buy milk"`), using `leftPad`.
4. Update the "no dependencies" convention note in `AGENTS.md` to reflect that
   `left-pad` is now a dependency and `npm install` is required on a fresh
   checkout. Update the "Setup for a fresh checkout" section to say
   `npm install`.
5. Add at least one test in `test/todo.test.js` (or a new file under `test/`)
   that imports `leftPad` from `left-pad` and asserts the padding output used in
   `render()` is correct (e.g. `leftPad(1, 3)` equals `"  1"`).

## Acceptance criteria

1. After running `npm install` in the worktree, the package resolves:
   `node -e "import('left-pad').then(m => console.log(m.default(7, 3)))"` prints
   `"  7"`.
2. `package.json` contains a `"dependencies"` key with `"left-pad"` listed.
3. `node --test` passes with no failures.
4. `node --check src/todo.js src/app.js` exits 0.
5. `src/app.js` imports `leftPad` from `"left-pad"` (grep:
   `grep "left-pad" src/app.js` returns a match).
6. With two items added (e.g. "alpha" and "beta"), the rendered `<li>` label
   text for the first item starts with `"  1."` and for the second starts with
   `"  2."` — verifiable by reading the `render()` function and confirming
   `leftPad(i + 1, 3)` is concatenated into the label.
7. `AGENTS.md` "Setup for a fresh checkout" section contains `npm install`.

## Out of scope

- Zero-padding (using `"0"` as the pad character instead of spaces).
- Storing or exposing the padded number through `src/todo.js` or item data
  structures.
- Any changes to `styles.css` or `index.html`.
- Adding a monospace font or CSS alignment for the numbers.

## Assumptions

- Width 3 is chosen as a reasonable fixed width (fits up to 999 items without
  truncation). The request does not specify width; 3 is the smallest useful
  fixed width. **(product)**
- The pad character is a space (the default for `left-pad`). The request does
  not specify otherwise.
- `left-pad` is imported as an ES module default import. The published
  `left-pad` package exposes a CommonJS export; using a dynamic `import()` or
  a named default import both work under Node's CJS interop. The spec requires
  the same import style already used in `src/app.js` (ES module `import`).
- The number is shown in the label text node (inside the existing `<label>`),
  not as a separate element, to minimise DOM changes.

## Risk

- Introducing the first external dependency changes the supply-chain surface of
  the repo. `left-pad` is a tiny, widely-audited package, but reviewers should
  confirm the installed version is the one from the official npm registry.
- `left-pad` is a CommonJS package; its interop with `"type": "module"` in
  `package.json` should be verified at runtime (Node ESM/CJS interop for default
  exports can behave differently across Node versions).
