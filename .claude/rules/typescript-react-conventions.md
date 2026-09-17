---
paths:
  - "**/*.ts"
  - "**/*.tsx"
---

# TypeScript & React Conventions

## Project Stack

React 19, TypeScript 5.x, Vite, Tailwind CSS (v4), shadcn/ui, wouter (routing), TanStack Query (data fetching), Lucide React (icons).

TypeScript must be configured with `strict: true` and `noUncheckedIndexedAccess: true`.

## Type Annotations

Annotate function parameters and return types. Use TypeScript's built-in utility types. Avoid `any` — use `unknown` when the type is genuinely unknown and narrow it before use.

```typescript
// Bad
function formatArticle(data: any) { ... }

// Good
function formatArticle(data: ArticleDetail): FormattedArticle { ... }
```

Use `interface` for props and object shapes (better error messages). Use `type` for unions, intersections, mapped types, and type aliases.

```typescript
// Object shapes — interface
interface ArticleCardProps {
  article: Article;
  variant?: "default" | "compact" | "featured";
}

// Unions / computed — type
type TopicSlug = "llm" | "computer-vision" | "robotics";
type ArticleResponse = ArticleListItem | ArticleDetail;
```

Use `string | null` over `string | undefined` for values that can be absent in API responses. Use optional properties (`prop?:`) for genuinely optional configuration.

Use `satisfies` to validate a value conforms to a type while preserving the narrower literal type:

```typescript
const ROUTES = {
  home: "/",
  article: "/article/:id",
  topic: "/topic/:slug",
} satisfies Record<string, string>;
```

Use `as const` for literal tuples and constant objects that should not be widened:

```typescript
const TOPIC_SLUGS = ["llm", "computer-vision", "robotics"] as const;
type TopicSlug = (typeof TOPIC_SLUGS)[number];
```

## Component Structure

Use named function exports. Default exports only for page-level route components. Do not use `React.FC` — it is verbose and adds no value.

```typescript
// Components — named export
export function ArticleCard({ article, variant = "default" }: ArticleCardProps) { ... }

// Pages — default export (wouter lazy routes)
export default function Home() { ... }
```

Define props as an `interface` directly above the component. Destructure props in the function signature. Type `children` as `React.ReactNode` when needed.

```typescript
interface ChatInputProps {
  onSend: (message: string) => void;
  disabled?: boolean;
}

export function ChatInput({ onSend, disabled = false }: ChatInputProps) { ... }

interface PanelProps {
  title: string;
  children: React.ReactNode;
}

export function Panel({ title, children }: PanelProps) { ... }
```

Prefer composition (accepting `children` or render slots) over deeply nested config-object props. This aligns with shadcn/ui's compound component patterns.

Order within a component:

1. Hooks (state, router, queries, mutations)
2. Derived state / computed values
3. Event handlers
4. Early returns (loading, error, empty states)
5. Main JSX return

## Hooks

Prefix custom hooks with `use`. One hook per file in `src/hooks/`.

```typescript
// hooks/useChat.ts
export function useChat(sessionId: string) { ... }
```

Use TanStack Query for all server state. Do not store server data in `useState`.

```typescript
// Bad — manually fetching into state
const [articles, setArticles] = useState<Article[]>([]);
useEffect(() => { fetch("/api/v1/articles").then(...) }, []);

// Good — TanStack Query manages cache, refetch, loading
const { data: articles, isLoading } = useGetArticles({ limit: 20 });
```

## Imports

Order: external packages first, then local (`@/`) imports. One blank line between groups. Let the bundler/linter enforce sorting. Always use `@/` path aliases — avoid relative imports (`../../`) for anything outside the current directory.

```typescript
// External packages
import { useState, useEffect } from "react";
import { Link, useRoute } from "wouter";
import { Clock, ExternalLink } from "lucide-react";

// Local (via @/ alias)
import { ArticleCard } from "@/components/article-card";
import { Button } from "@/components/ui/button";
import { formatTimeAgo } from "@/lib/format-date";
```

## File & Directory Naming

- Files: `kebab-case.tsx` for components, `kebab-case.ts` for utilities and hooks
- Directories: `kebab-case/`
- Component files export a PascalCase function matching the semantic name, not the filename

```
components/
  article-card.tsx      -> export function ArticleCard
  chat-widget.tsx       -> export function ChatWidget
  ui/                   -> shadcn/ui primitives (do not modify directly)
hooks/
  use-chat.ts           -> export function useChat
pages/
  home.tsx              -> export default function Home
  article-detail.tsx    -> export default function ArticleDetail
lib/
  format-date.ts        -> export function formatTimeAgo
types/
  article.ts            -> interface Article, interface ArticleDetail
```

## Styling

Use Tailwind utility classes. No CSS modules, no inline `style` objects (except dynamic values like computed widths).

Use the project's design tokens via CSS custom properties — never hardcode color values.

```typescript
// Bad — hardcoded color
<div className="bg-[#0A0A0A] text-[#FF9900]">

// Good — design token
<div className="bg-card text-foreground">
```

Use the project's custom utility classes for terminal-style panels:

```typescript
<div className="terminal-panel">
  <div className="terminal-header">SECTION_TITLE</div>
  <div className="p-3">{/* content */}</div>
</div>
```

For conditional or merged classes, use `cn()` from `@/lib/utils` (wraps `clsx` + `tailwind-merge`). It handles deduplication and Tailwind class conflicts correctly.

```typescript
<span className={cn("text-xs", metric.change >= 0 ? "text-positive" : "text-negative")}>

// In reusable components — merge caller-provided classes with defaults
<div className={cn("terminal-panel p-3", className)}>
```

## Terminal Aesthetic

The UI follows a Bloomberg/terminal aesthetic. Text in UI chrome (headers, labels, status indicators) should be:
- Monospace font (`font-mono`)
- Uppercase (`uppercase`)
- Small size (`text-xs` or `text-[10px]`)
- Underscore-separated labels (e.g., `LIVE_FEED`, `SYS_METRICS`, `DOC_INFO`)

Article content (headlines, summaries, body) uses sans-serif at normal case.

## Data Fetching

Use generated TanStack Query hooks from `@workspace/api-client-react` for all API calls. Pass explicit `queryKey` via the getter functions to ensure cache consistency.

```typescript
const { data, isLoading } = useGetArticles(
  { topic: "llm", limit: 20 },
  {
    query: {
      queryKey: getGetArticlesQueryKey({ topic: "llm", limit: 20 }),
      refetchInterval: 30_000,
    },
  }
);
```

For mutations, invalidate specific query keys on success rather than the entire cache.

```typescript
addBookmark.mutate(
  { data: { articleId: article.id } },
  {
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: getGetArticlesQueryKey() }),
  }
);
```

## SSE / Streaming

For the chat endpoint (`/api/v1/chat`), use `fetch` with `ReadableStream` to consume SSE deltas. Parse each `data:` line as JSON, validate the shape with a type guard or Zod schema, and accumulate text. Do not blindly trust parsed payloads.

Do not use `EventSource` — it does not support `POST` requests or custom headers.

## Error & Loading States

Handle loading, error, and empty states explicitly. Use `Skeleton` components from shadcn/ui for loading placeholders that match the layout shape.

```typescript
if (isLoading) {
  return (
    <div className="space-y-4">
      <Skeleton className="h-6 w-3/4" />
      <Skeleton className="h-20 w-full" />
    </div>
  );
}

if (isError) {
  return (
    <div className="terminal-panel p-8 text-center border-destructive">
      <div className="text-destructive font-bold">ERR_FETCH_FAILED</div>
      <div className="text-muted-foreground text-sm mt-1">Unable to load data. Try refreshing.</div>
    </div>
  );
}

if (!data || data.length === 0) {
  return (
    <div className="p-8 text-center text-muted-foreground text-sm">
      NO_DATA_AVAILABLE
    </div>
  );
}
```

Empty and error states use terminal-style uppercase labels.

Use `react-error-boundary` for catching render errors. Place at least one boundary at the app root and ideally per-route.

## Event Handlers

Prefix with `handle`. Define inside the component body, not as standalone functions.

```typescript
const handleBookmarkToggle = (e: React.MouseEvent) => {
  e.preventDefault();
  e.stopPropagation();
  // ...
};
```

## Constants

Named constants at module level, `UPPER_SNAKE_CASE`. Use for magic numbers, intervals, and limits.

```typescript
const LIVE_POLL_INTERVAL = 30_000;
const MAX_MESSAGE_LENGTH = 2_000;
```

## React 19 Patterns

**Refs as props**: `forwardRef` is no longer needed. Accept `ref` as a normal prop in the component's props interface.

```typescript
interface InputProps {
  value: string;
  ref?: React.Ref<HTMLInputElement>;
}

export function Input({ value, ref }: InputProps) {
  return <input ref={ref} value={value} />;
}
```

**`use()` hook**: Prefer `use(SomeContext)` over `useContext(SomeContext)`. It can be called conditionally.

**Form handling**: Prefer `<form action={fn}>` with `useActionState` over manual `onSubmit` + `useState` for forms that trigger server actions (e.g., the chat input).

**`useOptimistic`**: Use for optimistic UI updates (e.g., bookmark toggle, sending a chat message) where you want the UI to update before the server responds.

**Memoization**: Do not manually add `React.memo`, `useMemo`, or `useCallback` unless you've measured a performance problem. React 19's compiler handles memoization automatically when enabled. If the compiler is not in use, apply memoization only for expensive computations or stable callback references passed to memoized children.

**Code splitting**: Use `React.lazy` + `Suspense` for route-level code splitting with wouter:

```typescript
const ArticleDetail = React.lazy(() => import("@/pages/article-detail"));
```

## Accessibility

- Use semantic HTML: `<button>` for actions, `<a>` for navigation, `<nav>`, `<main>`, `<article>`, `<section>`. Never use `<div onClick>` as a button.
- All interactive elements must be keyboard-accessible.
- Images need `alt` text. Decorative Lucide icons should have `aria-hidden="true"`. Functional icons need `aria-label`.
- Use `aria-live="polite"` for the chat message stream so screen readers announce new messages.
- Use `aria-busy="true"` on containers in loading states.
- shadcn/ui components handle accessibility well — do not break it when customizing.

## Security

- **`dangerouslySetInnerHTML`**: Forbidden unless rendering HTML sanitized with DOMPurify from a trusted backend. If article content arrives as HTML, sanitize before rendering.
- **User-supplied URLs**: Validate that URLs from API data use `https:` protocol before rendering as `href` to prevent `javascript:` injection.
- **Environment variables**: Never expose secrets client-side. Only `VITE_`-prefixed env vars are bundled by Vite.

## Environment Variables

Type all Vite env vars via an `env.d.ts` file extending `ImportMetaEnv`:

```typescript
/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_API_URL: string;
}
```

All client-exposed env vars must be prefixed with `VITE_`.

## General Principles

- **Colocation**: Keep types, helpers, and sub-components close to where they're used. Only extract to `types/` or `lib/` when shared across multiple files.
- **No barrel exports**: Import directly from the file, not via `index.ts` re-exports.
- **Immutable patterns**: Use spread/map/filter to derive new state. Never mutate state directly.
- **Keys**: Use stable, unique IDs from the data (`article.id`) for list keys. Never use array index as key unless the list is static and never reordered.
