import { describe, expect, it } from 'vitest'
import yaml from 'js-yaml'

describe('YAML merge work budget', () => {
  it('counts empty aliases toward the budget while accepting a small merge', () => {
    const small = 'empty: &empty {}\nresult:\n  <<: [*empty]\n'
    const repeated = 'empty: &empty {}\nresult:\n  <<: [*empty, *empty, *empty, *empty]\n'

    expect(yaml.load(small, { maxTotalMergeKeys: 2 })).toEqual({ empty: {}, result: {} })
    expect(() => yaml.load(repeated, { maxTotalMergeKeys: 2 })).toThrow(/maxTotalMergeKeys/)
  })
})
