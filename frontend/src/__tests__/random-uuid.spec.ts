// FAR-2855: graph ids are `uuid.UUID` on the API, and a plain-HTTP self-hosted
// instance has no `crypto.randomUUID`, so every fallback must still be a UUID.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { randomUuid } from '../lib/randomId'

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const realCrypto = globalThis.crypto

describe('randomUuid', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it.each([
    ['crypto.randomUUID', realCrypto],
    ['getRandomValues only (insecure context)', { getRandomValues: realCrypto.getRandomValues.bind(realCrypto) }],
    ['no crypto at all', undefined],
  ])('returns distinct version 4 UUIDs with %s', (_name, cryptoApi) => {
    vi.stubGlobal('crypto', cryptoApi)

    const ids = [randomUuid(), randomUuid(), randomUuid()]

    for (const id of ids) expect(id).toMatch(UUID_V4)
    expect(new Set(ids).size).toBe(ids.length)
  })
})
