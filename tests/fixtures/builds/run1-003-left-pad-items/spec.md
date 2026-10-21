# Left-pad item numbers

> Feature: Add a dependency on left-pad and use it to pad item numbers

## Context

The app is a plain HTML/CSS/JS todo app with no runtime dependencies (AGENTS.md: "no dependencies"). The only relevant files are:

- `src/todo.js` — pure logic (addItem, toggleItem, removeItem, clearCompleted); no rendering
- `src/app.js` — DOM wiring; the `render()` function builds each `<li>` via `document.createElement`. Item number is currently not displayed; items are rendered as `label > checkbox + item.text`.
- `index.html` — loads `src/app.js` as a module, no bundler involved
- `package.json` — `{ "type": "module", "private": true }`, no dependencies field
- `test/todo.test.js` — tests run with `node --test`

The `left-pad` npm package (`npm install left-pad`) exports a CommonJS function `leftPad(str, len, ch)` that left-pads a string to a given length. Because the app uses native ES modules in the browser without a bundler, any npm package used in `src/app.js` must be importable as an ES module (e.g. via a bare specifier resolved by an importmap, or by a Node-compatible path for the test environment).

## Requirements

1. Add `left-pad` as a runtime dependency in `package.json` (run `npm install left-pad`), updating both `dependencies` and `node_modules`.
2. Import `left-pad` in `src/app.js` and use it to format a 1-based item number for each list item before displaying it.
3. Each rendered `<li>` must display the padded item number followed by the item text. The number must be zero-padded to at least 2 digits (i.e. `leftPad(i + 1, 2, "0")`), so item 1 shows as `01`, item 9 as `09`, item 10 as `10`.
4. The padded number must be prepended to the label text in the existing `label` element inside each `<li>`, keeping the checkbox and the "Remove" button structure intact.
5. Update `AGENTS.md` to remove the "no dependencies" claim and note that `left-pad` is used for number formatting.

## Acceptance criteria

1. After `npm install`, `node --test test/todo.test.js` exits 0 with all existing tests passing.
2. `node --check src/todo.js src/app.js` exits 0 (no syntax errors).
3. `node -e "import('left-pad').then(m => console.log(m.default(7, 2, '0')))"` prints `07`, confirming the package is installed and importable from Node.
4. Reading `package.json` shows a `dependencies` field containing `"left-pad"` at a semver range.
5. Opening `index.html` in a browser and adding three items shows them labelled `01`, `02`, `03` prepended to each item's text in the list.
6. After adding 10 items the tenth is labelled `10` (no extra leading zero), confirming the width is exactly 2 digits for single-digit indices and grows naturally beyond that.

## Out of scope

- Changing the item numbering width beyond 2 digits or making it configurable.
- Adding a bundler or build step.
- Renumbering items after removal (this is a display-only label based on current array index).
- Any changes to `src/todo.js` or the pure-logic tests beyond what is needed to support the feature.
- CSS or visual styling of the padded number.

## Assumptions

- "Item numbers" means the 1-based position of each item in the current list array, formatted with `left-pad`. **(product)**: the request does not specify what width or character to pad with; 2 digits with `"0"` is the minimal useful choice.
- Because there is no bundler, `left-pad` must be reachable from a browser `<script type="module">`. The simplest approach that avoids a bundler is to add a `node_modules` import-map entry in `index.html` pointing at the package's UMD/ESM file, or to use the package only in the Node test/lint path and inline its one-line logic in `app.js`. Given the feature explicitly says "use left-pad", the import must reference the actual package. A minimal `<script type="importmap">` in `index.html` mapping `"left-pad"` to its path under `node_modules` is the expected approach. **(product)**
- The existing "no dependencies" statement in `AGENTS.md` is a description of the current state, not a hard constraint the feature must preserve; the feature request supersedes it.
- Tests in `test/todo.test.js` cover only `src/todo.js`; no new tests for the DOM rendering in `src/app.js` are required because the repo has no DOM test harness.

## Risk

- `left-pad` is a CommonJS module. Its default export may not be directly importable as an ES module in browsers without a shim or re-export wrapper. The implementer must verify the file exposed in the importmap is browser-compatible (the package ships `index.js` which is CJS; a wrapper or the `esm` variant may be needed). This is the main technical risk.
- Updating `AGENTS.md` to remove "no dependencies" is a documentation change that may need review to keep other jobs' specs consistent.
