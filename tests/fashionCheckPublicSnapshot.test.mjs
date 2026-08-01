import test from 'node:test'
import assert from 'node:assert/strict'

import {
  formatPublicSnapshotAnswer,
  validatePublicSnapshot
} from '../runner/fashionCheckPublicSnapshot.mjs'

function makeSnapshot() {
  return {
    fetchedAt: '2026-08-01T04:00:00.000Z',
    current: {
      schemaVersion: 'fashion-check.public-current.v5',
      globalIssue: 444,
      cnIssue: 429,
      theme: '风信子冒险者',
      challengeWindow: {
        startsAt: '2026-07-31T16:00:00+08:00',
        endsAt: '2026-08-04T16:00:00+08:00'
      },
      slots: [
        {
          slotId: 'head',
          label: '头部',
          tag: '风信子',
          categoryId: 210,
          gold: {
            points: 8,
            items: [
              { itemId: 29984, name: '橙色风信子头花', iconId: 54324, rarity: 1 },
              { itemId: 29985, name: '紫色风信子头花', iconId: 54319, rarity: 1 }
            ]
          }
        },
        {
          slotId: 'hands',
          label: '手部',
          tag: '人族',
          categoryId: 42,
          gold: {
            points: 6,
            items: [{ itemId: 3520, name: '人族手套', iconId: 44114, rarity: 1 }]
          }
        }
      ],
      referenceShowcase: {
        dyes: [
          {
            slotId: 'head',
            family: { id: 'black', name: '黑', color: '#2B2923', points: 1 },
            exact: { dyeId: 6, name: '煤烟黑', color: '#2B2923', points: 2 }
          }
        ],
        solutions: [
          {
            id: '100',
            score: 100,
            entries: [
              { slotId: 'hands', iconId: 44114, label: '各种族初始装备', dye: { dyeId: 34, name: '骸骨白', points: 2 } },
              { slotId: 'head', item: { itemId: 29984, name: '橙色风信子头花', iconId: 54324, rarity: 1 } }
            ]
          }
        ]
      }
    },
    locales: {
      schemaVersion: 'fashion-check.current-locales.v3',
      items: {
        '29984': { 'zh-CN': '橙色风信子头花' },
        '29985': { 'zh-CN': '紫色风信子头花' },
        '3520': { 'zh-CN': '人族手套' }
      },
      dyes: {
        '6': { 'zh-CN': '煤烟黑' },
        '34': { 'zh-CN': '骸骨白' }
      },
      tags: {}
    }
  }
}

test('validatePublicSnapshot 接受合法快照', () => {
  const { current, locales } = makeSnapshot()
  assert.deepEqual(validatePublicSnapshot(current, locales), [])
})

test('validatePublicSnapshot 拒绝错误 schemaVersion 和非法期号', () => {
  const { current, locales } = makeSnapshot()
  const errors = validatePublicSnapshot(
    { ...current, schemaVersion: 'x', globalIssue: 0 },
    { ...locales, schemaVersion: 'y' }
  )
  assert.equal(errors.length >= 3, true)
})

test('formatPublicSnapshotAnswer 输出中文答案', () => {
  const snapshot = makeSnapshot()
  const text = formatPublicSnapshotAnswer(snapshot, new Date('2026-08-01T12:00:00+08:00'), 'live', snapshot.fetchedAt)
  assert.match(text, /国际服第 444 期 \/ 国服第 429 期/)
  assert.match(text, /主题：风信子冒险者/)
  assert.match(text, /头部｜风信子：橙色风信子头花、紫色风信子头花/)
  assert.match(text, /手部｜人族：人族手套/)
  assert.match(text, /头部：煤烟黑（黑）/)
  assert.match(text, /100 分方案/)
  assert.match(text, /手部：各种族初始装备（骸骨白）/)
  assert.match(text, /头部：橙色风信子头花/)
  assert.doesNotMatch(text, /本期挑战已结束/)
})

test('formatPublicSnapshotAnswer 过期与缓存标注', () => {
  const snapshot = makeSnapshot()
  const stale = formatPublicSnapshotAnswer(snapshot, new Date('2026-08-10T12:00:00+08:00'), 'cache', snapshot.fetchedAt)
  assert.match(stale, /本期挑战已结束/)
  assert.match(stale, /网络失败，展示 .* 的缓存数据/)
})
