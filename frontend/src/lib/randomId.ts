/**
 * Shared random-identifier helper (FAR-2070).
 *
 * Internal, non-security-critical IDs across the SPA all need the same
 * precedence chain: `crypto.randomUUID()` when available, then CSPRNG bytes
 * (`crypto.getRandomValues`), and only as a last resort a timestamp/Math.random
 * pair for environments with no CSPRNG at all. Keeping that chain in one place
 * stops the per-module copies from drifting apart.
 *
 * @param prefix - optional namespace prefix, e.g. `tab`
 */
export function randomId(prefix?: string): string {
  const cryptoApi = globalThis.crypto
  let id: string
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    id = cryptoApi.randomUUID()
  } else if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16))
    id = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  } else {
    id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}` // NOSONAR: only when no CSPRNG exists
  }
  return prefix ? `${prefix}-${id}` : id
}

/**
 * A canonical version-4 UUID, for ids the API stores as `uuid.UUID`.
 *
 * `randomId()` is not enough there: its last fallback is not a UUID. And
 * `crypto.randomUUID` only exists in a secure context, so a self-hosted
 * instance served over plain HTTP takes the byte-based path.
 */
export function randomUuid(): string {
  const cryptoApi = globalThis.crypto
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') return cryptoApi.randomUUID()
  const bytes =
    cryptoApi && typeof cryptoApi.getRandomValues === 'function'
      ? cryptoApi.getRandomValues(new Uint8Array(16))
      : Uint8Array.from({ length: 16 }, () => Math.floor(Math.random() * 256)) // NOSONAR: only when no CSPRNG exists
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
