# Web app design

Read this page when you change the web app of `awa ui`. It shows where the code lives, the design
tokens, the shared components and the tests that hold the page to its rules. The style is plain and
calm: white surfaces, rounded corners, soft shadows, one accent color and clear symbols.

## Where the code lives

| File | Holds |
| --- | --- |
| `src/awa/ui/page.html` | The markup of every page and the icon sprite |
| `src/awa/ui/page.css` | The tokens and the components |
| `src/awa/ui/js` | The behavior, one file per page, loaded in file-name order (`10_core.js` first, `99_shell.js` last) |
| `src/awa/ui/_app.py` | The application; `page_source()` inlines the stylesheet and the scripts under the page's nonce |

The page loads nothing from elsewhere: no web font, no CDN, no external image. Keep every element
`id` and every `aria` attribute: the browser tests find the page by them.

## Tokens

Every color, size and time is a custom property at the top of `page.css`. Light is the default; dark
applies with `prefers-color-scheme: dark` and with `data-theme="dark"` (the toggle in the header),
and both themes define the same names.

| Group | Tokens | Rule |
| --- | --- | --- |
| Surfaces | `--bg`, `--surface`, `--sunken` | page, cards, wells inside cards |
| Text | `--text`, `--text-2`, `--text-3` | every text color reaches 4.5:1 on every surface |
| Lines | `--line`, `--line-strong` | `--line-strong` (3:1) is the border of every control |
| Accent | `--accent`, `--accent-hover`, `--on-accent`, `--accent-soft`, `--accent-text` | the only hue; one primary button per view |
| States | `--ok`, `--ok-bg`, `--warn`, `--warn-bg`, `--err`, `--err-bg` | only ever a state, always with an icon and a word |
| Shape | `--r-sm` 8 px, `--r-md` 12 px, `--r-lg` 16 px, `--r-xl` 28 px, `--r-pill` | cards 16, controls 12, pills capsule |
| Space | `--s-1` to `--s-6` (4, 8, 16, 24, 32, 48 px) | an 8 px grid |
| Elevation | `--sh-1`, `--sh-2` | cards, toasts; in dark a hairline instead of a shadow |
| Motion | `--d-fast` 120 ms, `--d-base` 200 ms, `--d-slow` 320 ms, `--ease-out`, `--ease-in` | only opacity and transform move |
| Gradient | `--grad-hero`, `--grad-accent` | very light; a flat color stays under them, and `forced-colors` removes them |
| Targets and focus | `--hit` 44 px, `--hit-sm` 32 px, `--focus` | the focus ring is 2 px, offset 2 px, on every focusable element |
| Type | `--font`, `--mono` | the system fonts of each platform |

## Components

| Component | Use |
| --- | --- |
| `icon(name)` | One symbol of the inline sprite (24 px grid, round strokes); in markup `<svg class="ic"><use href="#i-name"/></svg>` |
| `decorate(root)` | Gives every state element its symbol from its class, so no state is shown by color alone |
| `tick(element)` | The success check: a green pill whose ring and check draw themselves once |
| `emptyState(icon, title, text, action)` | What is missing, why, and the one action that fixes it |
| `skeleton()` | The placeholder while a page loads |
| `toast(text, {undo})` | A short message, with an Undo button when given |
| `removeWithUndo(row, label, commit)` | Collapses a removed row at once and keeps an Undo toast for 8 s; the server is asked only when the time is up or the page changes |
| `fail(error)` | An error that says what failed and how to fix it |

| State | Look |
| --- | --- |
| done, passed, saved, valid | green pill with the drawn check |
| failed | red pill, x-circle |
| needs your OK, warning | amber pill, alert or shield-check |
| running | spinner in the accent color |
| stand-in, copy | gray pill, database |
| live | green pill, plug |

## Rules

- `prefers-reduced-motion: reduce` keeps the end state: the check is drawn at once, skeletons and
  spinners stand still, and only fades remain.
- Below 640 px the layout is one column, the step bar scrolls sideways and wide tables become rows;
  the page never scrolls horizontally.
- Labels use the terms of the rest of awa: agent, approval, run, connector, external system, eval case.

## Tests

`tests/unit/test_ui_page.py` checks that the stylesheet and the scripts load nothing from elsewhere,
that every `var(--x)` in use is defined and both themes define the same colors, that the named text
pairs reach 4.5:1 in both themes, and that every icon in use exists and every symbol is used.
`tests/unit/test_ui_browser.py` drives Chrome or Edge headless over the DevTools protocol against the
app with scripted models: every page in both themes at 1280 and 390 px, the click-through of each
flow, keyboard focus, delete with undo, empty and error states and reduced motion. A console error or
a request to anything but the app fails the test; without a browser the tests skip.

```console
python -m pytest tests/unit/test_ui_page.py tests/unit/test_ui_browser.py
```
