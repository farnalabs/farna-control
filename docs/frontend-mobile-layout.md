# Frontend mobile layout (FAR-2537)

Mobile is lower priority than desktop. The rule is simple: below 640px (`sm:`) everything is a plain vertical stack. At `sm:` and up the layout is exactly what it was before. Fix problems in the shared primitives, not page by page.

## Rules

1. Stack first, then restore the row at `sm:` (`flex-col sm:flex-row`).
2. Content children get `min-w-0` so long values can wrap or truncate instead of forcing the page wider.
3. A button never wraps its own label. Whole buttons may wrap to the next row (`flex-wrap`).
4. Never size a page with bare `h-screen` or `100vh` on mobile. Browser chrome hides the bottom. The app shell uses `h-screen h-screen-dvh` (100vh fallback, 100dvh where supported).

## Which primitive to use

| Situation | Use |
|---|---|
| Card title/description with actions | `CardHeader` (`components/ui`) with the `actions` slot |
| Page title with right-hand controls | `PageHeader` (`components/shared`) with the `right` slot |
| Any other "content left, buttons right" row | `ActionRow` (`components/shared`) with the default and `actions` slots |
| A custom actions container | add the `actions-nowrap` class (stops button labels wrapping) |

## 390px acceptance test

Every new or changed screen should pass this at a 390x844 viewport:

- `document.documentElement.scrollWidth <= window.innerWidth` (no horizontal overflow)
- every button label inside an `actions-nowrap` container (card header actions, page header right slot, ActionRow actions) sits on a single line
- at 1024px or wider, `CardHeader` is still a row

The reference test is the "Mobile foundation" block in `frontend/tests/e2e/mobile-layout.spec.ts` (`pnpm run test:e2e:mobile`).

## Known follow-up

FAR-2547 completed the `dvh` sweep: every `100vh`/`h-screen`/`min-h-screen` view now
keeps its `vh` class as the fallback and adds the matching `dvh` companion. A new
screen should reach for one of the shared utilities instead of a bare `vh` class:

- `h-screen-dvh` — full viewport height (`h-screen` stays as the fallback).
- `min-h-screen-dvh` — at least full viewport height (`min-h-screen` fallback).
- `h-chrome-dvh` — viewport minus the `3.5rem` app-chrome bar (`h-[calc(100vh-3.5rem)]` fallback).
- `max-h-viewport-dvh` — viewport minus a `1rem` gutter, for fixed popovers.
