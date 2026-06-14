# Plan: Unit tests for brains/_postprocess.py

## 1. Files to create / modify
- **Create**: `tests/test_postprocess_unit.py`
- **Modify**: none. `brains/_postprocess.py` and all other files stay untouched.

## 2. Implementation order
1. Read `brains/_postprocess.py` (done) and pin down exact runtime behavior of
   `strip_think_blocks()` and `strip_markdown()`, including edge cases.
2. Write `tests/test_postprocess_unit.py` importing the two public functions
   directly from `brains._postprocess`.
3. Run the suite with pytest; reconcile any expectation against actual regex
   output (no source changes — tests must match real behavior).

## 3. Tests to write
### strip_think_blocks()
- `None` input returns `None` (guard: `not text` short-circuits).
- empty string `""` returns `""`.
- text with no `<think>` block returned unchanged.
- single `<think>...</think>` block removed.
- multiline `<think>` block (DOTALL) removed.
- multiple `<think>` blocks all removed.
- surrounding non-think text preserved; leftover blank lines collapsed.

### strip_markdown()
- empty string `""` returns `""`.
- `None` raises `TypeError` (passes through guard, then regex on None) — document actual behavior.
- bold `**x**` -> `x`.
- italic `*x*` -> `x`.
- headers `#`..`######` prefix removed.
- bullet lists (`-`, `*`, `•`) markers removed.
- numbered lists (`1.`, `2)`) markers removed.
- fenced code blocks (``` ```lang ```) fences removed, inner kept.
- inline code `` `x` `` -> `x`.
- strip_markdown also strips `<think>` blocks (delegates to strip_think_blocks).
- collapses 3+ newlines to 2 and trims outer whitespace.

## 4. Risks / assumptions
- The regexes are simple/greedy in places (e.g. italic `\*(.+?)\*`); test inputs
  are chosen to be unambiguous so assertions are stable across environments.
- `strip_markdown(None)` behavior is asserted as raising rather than returning
  None, because the guard only protects `strip_think_blocks`, and the first
  `re.sub` then receives `None`. Will verify empirically before finalizing.
- Tests assert exact string equality where the transform is deterministic, and
  use membership/substring checks only where whitespace details are incidental.
- Import path assumed to be `brains._postprocess` (package `brains/`); will
  confirm `brains/__init__.py` import resolves when running pytest.
