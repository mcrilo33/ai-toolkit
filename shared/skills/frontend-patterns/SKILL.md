# Frontend Patterns

Production-ready patterns for React / Next.js applications — component design,
state management, performance, forms, and accessibility.

## When to Use

- Building or reviewing React / Next.js components or pages
- Designing state management, data fetching, or form handling
- User says "frontend pattern for X", "how should I structure this component"

**Do NOT use when:**

- Backend-only work (use `backend-patterns`)
- Styling-only task with no logic (just write the CSS)
- Mobile-native work (React Native has different patterns)

## Patterns

### 1. Component Composition

Prefer composition over prop drilling and inheritance.

**Compound components** — share state via context, compose via children:

```tsx
<Select value={v} onChange={setV}>
  <Select.Trigger />
  <Select.Options>
    <Select.Option value="a">Alpha</Select.Option>
    <Select.Option value="b">Beta</Select.Option>
  </Select.Options>
</Select>
```

**Rules:**

- Split UI from logic: container (data/state) → presentational (rendering)
- Keep components under ~150 lines — extract when they grow
- Co-locate component, styles, tests, and types in the same directory
- Props interface at the top of the file, exported if reused

### 2. Custom Hooks

Extract reusable logic into hooks. One hook = one concern.

**Common hooks to extract:**

| Hook | Purpose |
|------|---------|
| `useDebounce(value, ms)` | Delay updates for search/filter inputs |
| `useFetch(url)` | Data fetching with loading/error states |
| `useLocalStorage(key)` | Persistent state synced to localStorage |
| `useMediaQuery(query)` | Responsive breakpoint detection |
| `useClickOutside(ref, cb)` | Close dropdowns/modals on outside click |

**Rules:**

- Prefix with `use` — always
- Return `[value, setter]` or `{ data, error, loading }` — be consistent
- Handle cleanup in `useEffect` return — prevent memory leaks
- Don't call hooks conditionally — follow Rules of Hooks

### 3. State Management

**Decision tree:**

```
Local to one component?         → useState / useReducer
Shared by a subtree (< 5)?      → Context + useReducer
Global, frequent updates?       → Zustand / Jotai
Server state (API data)?        → TanStack Query / SWR
URL state (filters, pagination)? → URL search params
```

**Rules:**

- Start with the simplest option — escalate only when needed
- Never store derived state — compute it (`useMemo` if expensive)
- Avoid prop drilling past 2 levels — use context or state library
- Colocate state as close to where it's used as possible

### 4. Data Fetching (Server Components + TanStack Query)

**Server Components (Next.js App Router):**

- Fetch data in `page.tsx` or `layout.tsx` — no `useEffect`
- Use `async` server components for initial page data
- Stream with `<Suspense>` + `loading.tsx` for progressive rendering

**Client-side (mutations, real-time, user-driven):**

- Use TanStack Query for cache, dedup, retry, background refetch
- Optimistic updates for mutations: update cache → mutate → rollback on error
- Invalidate related queries after mutations

**Rules:**

- Server Components for read-heavy pages — zero client JS for data fetching
- Client Components for interactivity (forms, clicks, real-time)
- Never `fetch` in `useEffect` for initial data — use server components or TanStack Query

### 5. Form Handling

Use a form library (React Hook Form, Formik) for anything beyond a single input.

**Pattern:**

```
Schema (Zod) → Form library → Field components → Submit handler → API call
```

**Rules:**

- Validate with Zod/Yup schema — share schema between client and server
- Show errors inline, next to the field — not in a toast
- Disable submit button while submitting — prevent double-submit
- Handle server-side validation errors by mapping to field errors
- Use `<form>` element + `onSubmit` — never `onClick` on a button for form submission

### 6. Performance

**Rendering:**

- `React.memo` — only for components that re-render often with same props
- `useMemo` / `useCallback` — only when profiling shows a bottleneck
- Never optimize prematurely — measure first with React DevTools Profiler

**Bundle size:**

- `dynamic(() => import(...))` (Next.js) or `React.lazy` for heavy components
- Split vendor code: large libraries (charts, editors) loaded on demand
- Analyze with `next build --analyze` or `source-map-explorer`

**Images:**

- Use `<Image>` (Next.js) or lazy-loaded `<img>` — never unoptimized raw `<img>`
- Provide `width` and `height` to prevent layout shift (CLS)
- Use WebP/AVIF formats with fallbacks

### 7. Error Boundaries

Wrap route segments and critical UI sections:

```tsx
<ErrorBoundary fallback={<ErrorFallback />}>
  <SuspenseWrapper>
    <Dashboard />
  </SuspenseWrapper>
</ErrorBoundary>
```

**Rules:**

- Place boundaries at route level + around critical widgets (charts, editors)
- Provide a "retry" button in fallback UI
- Log errors to monitoring (Sentry, etc.) in `componentDidCatch` or `onError`
- Never let a broken widget crash the entire page

### 8. Accessibility (a11y)

**Non-negotiable:**

- All interactive elements are keyboard-accessible (`Tab`, `Enter`, `Escape`)
- Images have meaningful `alt` text (or `alt=""` for decorative)
- Form inputs have associated `<label>` elements
- Color contrast ≥ 4.5:1 for normal text, 3:1 for large text
- Focus is visible and managed (modals trap focus, closing restores focus)
- Use semantic HTML (`<nav>`, `<main>`, `<article>`, `<button>`) — not `<div onClick>`
- ARIA only when semantic HTML isn't enough — don't over-ARIA
