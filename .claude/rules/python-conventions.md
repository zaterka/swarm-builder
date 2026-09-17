---
paths:
  - "**/*.py"
---

# Python Conventions

## Type Annotations

Annotate all function parameters and return types. Avoid `Any`.

```python
# Bad
def process(data, timeout):
    ...

# Good
def process(data: bytes, timeout: float) -> ProcessResult:
    ...
```

Use `X | None` for nullable types. Use `tuple[str, int]` over `Tuple[str, int]` (Python 3.10+).

## Docstrings

Google-style. One-line summary in imperative mood, then `Args`, `Returns`, `Raises` sections. Do not repeat what type annotations already convey.

```python
def split_document(document: bytes, pages_per_split: int) -> list[bytes]:
    """Split a PDF into chunks of the specified page count.

    Args:
        document: The PDF file as raw bytes.
        pages_per_split: Maximum pages per chunk.

    Returns:
        List of PDF byte chunks.

    Raises:
        ValueError: If the document is empty or corrupt.
    """
```

For trivial helpers, a one-liner is enough: `"""Extract x,y coordinates from a bounding box."""`

## Imports

All imports at module level. Never inside a function body.

```python
# Standard library
import asyncio
from pathlib import Path

# Third-party
import httpx

# Local
from app.services.extraction import extract_document
from app.utils.constants import MAX_RETRIES
```

Order: standard library, third-party, local — with a blank line between each group. Let Ruff/isort enforce sorting.

## Error Handling

Raise exceptions on failure. Do not return `bool` or `None` to signal errors — callers forget to check.

```python
# Bad — caller can silently ignore failure
def save(data: dict) -> bool:
    try:
        db.insert(data)
        return True
    except Exception:
        return False

# Good — failure is explicit
def save(data: dict) -> None:
    try:
        db.insert(data)
    except Exception as e:
        logger.error(f"Failed to save: {e}", exc_info=True)
        raise
```

Use bare `raise` to re-raise (preserves traceback). Never `raise e`. Catch specific exception types when different handling is needed.

## Naming

- Functions and variables: `snake_case`, verb phrases for functions (`get_user`, `validate_input`)
- Classes: `PascalCase`, noun phrases (`DocumentProcessor`, `RetryConfig`)
- Constants: `UPPER_SNAKE_CASE` in a dedicated constants module
- Private helpers: prefix with `_` (`_parse_header`)
- Avoid abbreviations unless universally understood (`url`, `id`, `config` are fine; `doc_proc_mgr` is not)

## Comments

Explain **why**, not **what**. The code should be readable enough on its own.

```python
# Bad
# Increment retry counter
retry_count += 1

# Good — explains a non-obvious business rule
# Textract occasionally returns empty pages for scanned docs; retry up to 3 times before failing
retry_count += 1
```

Remove commented-out code. Use version control to recover old code.

## Async Patterns

Use `asyncio.gather()` with `return_exceptions=True` for parallel I/O. Bound concurrency with a semaphore when calling external services to avoid overwhelming them.

```python
semaphore = asyncio.Semaphore(10)

async def fetch_one(page: int) -> bytes:
    async with semaphore:
        return await s3.get_object(page)

results = await asyncio.gather(*[fetch_one(p) for p in pages], return_exceptions=True)
```

## General Principles

- **Single responsibility** — each function does one thing. If it needs a paragraph to explain, split it.
- **No magic values** — use named constants (`MAX_RETRIES = 3`) instead of bare literals.
- **DRY** — extract repeated logic, but do not abstract prematurely. Three similar lines are better than a forced abstraction used once.
