/**
 * "edited 5 minutes ago" for the start screen's graph rows.
 *
 * `GraphSummary.updatedAt` is server-stamped `datetime.now(UTC)`
 * (`routes/graphs.py`), so it always carries an offset and parses
 * unambiguously. Anything older than a week becomes an absolute date --
 * "edited 43 days ago" is harder to read than "edited Aug 5".
 */

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function formatRelativeTime(iso: string, now: Date = new Date()): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';

  const elapsed = now.getTime() - then;
  const magnitude = Math.abs(elapsed);

  if (magnitude < 45 * SECOND) return 'just now';

  const relative = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  if (magnitude < HOUR) {
    return relative.format(-Math.round(elapsed / MINUTE), 'minute');
  }
  if (magnitude < DAY) {
    return relative.format(-Math.round(elapsed / HOUR), 'hour');
  }
  if (magnitude < 7 * DAY) {
    return relative.format(-Math.round(elapsed / DAY), 'day');
  }

  const date = new Date(then);
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    year: date.getFullYear() === now.getFullYear() ? undefined : 'numeric',
  }).format(date);
}

export default formatRelativeTime;
