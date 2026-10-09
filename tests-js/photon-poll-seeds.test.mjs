import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, expect, test, vi } from 'vitest'
import { PollSeedStore } from '../plugins/platforms/photon/sidecar/poll-seeds.mjs'

const directories = []

afterEach(() => {
  vi.restoreAllMocks()
  for (const directory of directories.splice(0)) {
    fs.rmSync(directory, { recursive: true, force: true })
  }
})

function seedFile() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-poll-seeds-'))
  directories.push(directory)
  return path.join(directory, 'profile', 'polls.json')
}

function remember(store, id) {
  store.remember(id, id, [{ text: 'Yes', optionIdentifier: `${id}-yes` }])
}

test('a stale writer preserves the newest retained polls and sent-ID history', () => {
  const file = seedFile()
  const current = new PollSeedStore(file, 2)
  remember(current, 'p1')
  remember(current, 'p2')
  const stale = new PollSeedStore(file, 2).load()
  remember(current, 'p3')
  remember(current, 'p4')
  remember(stale, 'p5')

  const restarted = new PollSeedStore(file, 2).load()
  for (const store of [stale, restarted]) {
    expect([...store.polls.keys()]).toEqual(['p4', 'p5'])
    expect(store.get('p4').optionsByIdentifier.get('p4-yes')).toEqual({ title: 'Yes' })
    expect([...store.sentPollIds.keys()]).toEqual(['p1', 'p2', 'p3', 'p4', 'p5'])
  }
})

test('failed saves retain pending sends without losing another writer on retry', () => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const file = seedFile()
  const parent = path.dirname(file)
  fs.writeFileSync(parent, 'temporarily prevents creating the seed directory')
  const retrying = new PollSeedStore(file, 5)
  remember(retrying, 'unsaved')
  expect(retrying.has('unsaved')).toBe(true)
  fs.rmSync(parent)

  remember(new PollSeedStore(file, 5), 'other-writer')
  remember(retrying, 'current')
  const restarted = new PollSeedStore(file, 5).load()
  expect([...restarted.polls.keys()]).toEqual(['other-writer', 'unsaved', 'current'])
  expect([...restarted.sentPollIds.keys()]).toEqual(['other-writer', 'unsaved', 'current'])

  fs.writeFileSync(file, '{corrupt')
  remember(retrying, 'after-corruption')
  expect([...new PollSeedStore(file, 5).load().polls.keys()])
    .toEqual(['other-writer', 'unsaved', 'current', 'after-corruption'])
})
