/** Maximum length of an API key name accepted by the create-key dialog. */
export const API_KEY_NAME_MAX = 64;

/**
 * Display form of a stored key prefix. The backend already terminates some
 * prefixes with an ellipsis, so appending one unconditionally rendered
 * "5143……". Append only when the prefix does not already end with one.
 */
export function formatKeyPrefix(prefix: string): string {
  const trimmed = prefix.trimEnd();
  if (trimmed.endsWith("…") || trimmed.endsWith("...")) return trimmed;
  return `${trimmed}…`;
}

/**
 * The key name sent to the API: trimmed, non-empty, and within the length
 * cap. Returns null when the raw input has no usable name.
 */
export function normalizeKeyName(raw: string): string | null {
  const trimmed = raw.trim();
  if (trimmed.length === 0 || trimmed.length > API_KEY_NAME_MAX) return null;
  return trimmed;
}
