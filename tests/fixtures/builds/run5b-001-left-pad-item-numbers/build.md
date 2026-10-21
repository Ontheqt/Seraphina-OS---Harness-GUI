STATUS: READY

# Build: Left-pad item numbers

## Summary

Adds `left-pad` as the first runtime dependency, uses it in `src/app.js` to
prepend each list item's label with its 1-based position padded to width 3
followed by ". " (e.g. "  1. Buy milk"). The padded number lives in a
`<span style="white-space: pre">` so leading spaces are not collapsed by the
browser. Tests, lint, and AGENTS.md are all updated to match.

Files changed:
- `package.json` — added `"dependencies": { "left-pad": "^1.3.0" }`, trailing newline added
- `package-lock.json` — created by `npm install`
- `src/app.js` — import `leftPad`; `render()` builds a `<span style="white-space:pre">` for the padded number
- `test/todo.test.js` — added `leftPad` padding assertions
- `AGENTS.md` — Overview and Conventions updated; Setup section says `npm install`
- `.gitignore` — added `node_modules/`

## Acceptance criteria

1. [x] `node -e "import('left-pad').then(m => console.log(m.default(7, 3)))"` prints `  7`: confirmed
2. [x] `package.json` contains `"dependencies"` with `"left-pad"`: `{"type":"module","private":true,"dependencies":{"left-pad":"^1.3.0"}}`
3. [x] `node --test` passes with no failures: 5 tests, 0 failures
4. [x] `node --check src/todo.js src/app.js` exits 0: confirmed
5. [x] `grep "left-pad" src/app.js` returns a match: `import leftPad from "left-pad";`
6. [x] `render()` uses `leftPad(i + 1, 3)` concatenated into the label: confirmed in `src/app.js`; number placed in `<span style="white-space: pre">` so spaces render visibly
7. [x] `AGENTS.md` "Setup for a fresh checkout" contains `npm install`: confirmed

## Validation

| Command | Result |
|---|---|
| `node --test test/todo.test.js` | 5 pass, 0 fail |
| `node --check src/todo.js src/app.js` | exit 0 |
| `node -e "import('left-pad').then(m => console.log(m.default(7, 3)))"` | prints `  7` |
| `grep "left-pad" src/app.js` | `import leftPad from "left-pad";` |
| `git status --porcelain` | clean |

## Log

### Round 1

Implemented all five requirements from the spec:

1. Ran `npm install left-pad --save` — added `left-pad@^1.3.0` to
   `package.json` dependencies and created `package-lock.json`.
2. Added `import leftPad from "left-pad"` to `src/app.js` and changed the
   `label.append` call to prepend `leftPad(i + 1, 3) + ". "` to each item text.
3. Updated `AGENTS.md` "Setup for a fresh checkout" from `None` to `npm install`
   and updated the conventions note to mention `left-pad`.
4. Added a `leftPad pads item numbers for render()` test in `test/todo.test.js`
   asserting `leftPad(1,3)==="  1"`, `leftPad(10,3)===" 10"`, etc.
5. Added `node_modules/` to `.gitignore` (was missing; needed to keep the tree
   clean after install).

### Round 2

Addressed all three CHANGES verdicts:

- **review-ui & review-ux (whitespace collapse)**: The padded number string was
  passed as a plain text node inside `<label>`, causing browsers to collapse the
  leading spaces. Fixed by placing the number in a `<span style="white-space: pre">`
  element (`num.textContent = leftPad(i + 1, 3) + ". "`) so leading spaces are
  preserved without touching `styles.css`.

- **review-code (AGENTS.md Overview)**: The Overview sentence still read "Plain
  HTML/CSS/JS, no dependencies." Updated to "one runtime dependency (`left-pad`)".

- **review-ux & review-code notes (package.json trailing newline)**: Added the
  missing trailing newline to `package.json`.

All tests pass (5/5), lint is clean, tree is clean.
