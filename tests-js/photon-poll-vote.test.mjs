import { expect, test } from 'vitest'
import { normalizePollVote } from '../plugins/platforms/photon/sidecar/poll-vote.mjs'

test('native selection preserves its option and poll metadata', () => {
  expect(normalizePollVote({
    option: { title: 'Allow Once' }, poll: { title: 'Approval' }, selected: true,
  })).toEqual({ type: 'poll_option', title: 'Allow Once', pollTitle: 'Approval', selected: true })
})

test('deselection and ambiguous selection state cannot become consent', () => {
  for (const selected of [false, undefined, null, 'true', 1]) {
    expect(normalizePollVote({ title: 'Always Allow', selected }).selected).toBe(false)
  }
})
