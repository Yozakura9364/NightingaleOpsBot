import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  answerSourceMatchesExpectedIssue,
  applyManualQqAnswers,
  classifyCollectionWindow,
  expectedGlobalIssue,
  formatFashionCheckAnswer,
  getScorePlanStatus,
  manualScorePlansComplete,
  scorePlansComplete,
  shouldLockAnswerPush
} from '../runner/fashionCheckCollector.mjs'
import { calculateScorePlan } from '../runner/fashionCheckScoring.mjs'
import {
  decodeQqSheetJsonp,
  extractQqCurrentWeek,
  parseAllGameStaffEntry,
  parseCsv
} from '../runner/fashionCheckSources.mjs'

test('persists subscriber updates without duplicating the same signature', async () => {
  const storageRoot = await mkdtemp(join(tmpdir(), 'fashion-check-subscriber-'))
  const previousRoot = process.env.NS_FASHION_CHECK_ROOT
  process.env.NS_FASHION_CHECK_ROOT = storageRoot
  try {
    const collector = await import(`../runner/fashionCheckCollector.mjs?subscriber-test=${Date.now()}`)
    assert.equal(
      await collector.recordFashionCheckSubscriberUpdate('theme-updated', 'theme:test', '测试主题'),
      true
    )
    assert.equal(
      await collector.recordFashionCheckSubscriberUpdate('theme-updated', 'theme:test', '测试主题'),
      false
    )
    const updates = await collector.getFashionCheckSubscriberUpdates()
    assert.equal(updates.items.length, 1)
    assert.equal(updates.items[0].text, '测试主题')
    assert.equal(updates.latestId, updates.items[0].id)
  } finally {
    if (previousRoot === undefined) delete process.env.NS_FASHION_CHECK_ROOT
    else process.env.NS_FASHION_CHECK_ROOT = previousRoot
    await rm(storageRoot, { recursive: true, force: true })
  }
})

function shanghaiDate(isoLocal) {
  return new Date(`${isoLocal}+08:00`)
}

test('classifies the Tuesday and Friday collection windows in Asia/Shanghai', () => {
  assert.equal(classifyCollectionWindow(shanghaiDate('2026-07-14T16:04:00')), null)
  assert.equal(classifyCollectionWindow(shanghaiDate('2026-07-14T16:05:00')).kind, 'theme')
  assert.equal(classifyCollectionWindow(shanghaiDate('2026-07-15T16:05:00')).finalTick, true)
  assert.equal(classifyCollectionWindow(shanghaiDate('2026-07-15T16:06:00')), null)
  assert.equal(classifyCollectionWindow(shanghaiDate('2026-07-17T16:05:00')).kind, 'answers')
  assert.equal(classifyCollectionWindow(shanghaiDate('2026-07-18T16:05:00')).finalTick, true)
})

test('calculates the current issue at the Tuesday 16:00 boundary', () => {
  assert.equal(expectedGlobalIssue(shanghaiDate('2026-07-14T15:59:59')), 441)
  assert.equal(expectedGlobalIssue(shanghaiDate('2026-07-14T16:00:00')), 442)
})


test('does not treat stale answer sources as the current issue', () => {
  const qq = { current: { globalIssue: 442 } }
  assert.equal(
    answerSourceMatchesExpectedIssue({ globalIssue: 441 }, qq, shanghaiDate('2026-07-17T16:05:00')),
    false
  )
  assert.equal(
    answerSourceMatchesExpectedIssue({ globalIssue: 442 }, qq, shanghaiDate('2026-07-17T16:05:00')),
    true
  )
})
test('does not expose a stale week as the current answer', () => {
  const current = {
    sources: {
      'qq-cn-history': {
        current: {
          globalIssue: 441,
          theme: '上一期主题',
          slots: [{ slotLabel: '头部', tag: '旧标签', answerText: '旧答案' }]
        }
      }
    }
  }
  const output = formatFashionCheckAnswer(current, shanghaiDate('2026-07-14T18:00:00'))
  assert.match(output, /国际服第 442 期/)
  assert.match(output, /本周主题与答案尚未更新/)
  assert.doesNotMatch(output, /上一期主题|旧答案/)
})

test('formats current gold answers and 80/100 point plans', () => {
  const current = {
    sources: {
      'qq-cn-history': {
        current: {
          globalIssue: 442,
          theme: '测试主题',
          slots: [
            { slotLabel: '头部', tag: '知性', answerText: '黄铜眼镜' },
            { slotLabel: '身体', tag: '简约', answerText: '春意衬衫' }
          ]
        }
      },
      'allgamestaff-en': {
        globalIssue: 442,
        tables: [
          {
            heading: 'How to score 80 Points',
            rows: [['Slot', 'Item', 'Dye'], ['Head', 'Brass Spectacles', 'Rust Red Dye']]
          },
          {
            heading: 'How to achieve 100 Points',
            rows: [['Slot', 'Item', 'Dye'], ['Body', 'Spring Shirt', 'Soot Black Dye']]
          }
        ]
      }
    }
  }
  const output = formatFashionCheckAnswer(current, shanghaiDate('2026-07-14T18:00:00'))
  assert.match(output, /主题：测试主题/)
  assert.match(output, /头部｜知性\n待核验档位：黄铜眼镜/)
  assert.match(output, /80 分方案[\s\S]*头部：Brass Spectacles \/ Rust Red Dye/)
  assert.match(output, /100 分方案[\s\S]*身体：Spring Shirt \/ Soot Black Dye/)
})

test('publishes known gold tags while omitting unknown equipment', () => {
  const current = {
    sources: {
      'qq-cn-history': {
        current: {
          globalIssue: 442,
          theme: '测试主题',
          slots: [
            { slotLabel: '头部', tag: '知性', answerText: '黄铜眼镜' },
            { slotLabel: '身体', tag: '简约', answerText: '' },
            { slotLabel: '手部', tag: '军装', answerText: '  ' }
          ]
        }
      }
    }
  }
  const output = formatFashionCheckAnswer(current, shanghaiDate('2026-07-14T18:00:00'))
  assert.match(output, /已知部位与装备[\s\S]*头部｜知性\n待核验档位：黄铜眼镜/)
  assert.match(output, /身体｜简约/)
  assert.match(output, /手部｜军装/)
  assert.match(output, /作业未更新/)
  assert.doesNotMatch(output, /尚未确认|80\/100 分方案尚未更新/)
})

test('formats owner-confirmed dye guide before partial gold answers', () => {
  const current = {
    manualAnswers: {
      '442': {
        dyeGuide: {
          provider: '@勍天',
          entries: [
            { slotLabel: '武器', family: '红', dye: '果酒红', source: 'NPC霞，216g' },
            { slotLabel: '头部', family: '绿', dye: '柔彩绿', source: '雇员的宝箱/商城/活动' }
          ]
        }
      }
    },
    sources: {
      'qq-cn-history': {
        current: {
          globalIssue: 442,
          theme: '测试主题',
          slots: [{ slotLabel: '身体', tag: '亚拉戈高位', answerText: '亚拉戈高位系列身体防具' }]
        }
      }
    }
  }
  const output = formatFashionCheckAnswer(current, shanghaiDate('2026-07-14T18:00:00'))
  assert.match(output, /各部位染色攻略（提供者：@勍天）/)
  assert.match(output, /武器：红-果酒红（NPC霞，216g）/)
  assert.match(output, /头部：绿-柔彩绿（雇员的宝箱\/商城\/活动）/)
  assert.match(output, /80\/100 方案未更新/)
  assert.doesNotMatch(output, /作业未更新/)
})

test('formats owner-confirmed 80 point plan and keeps 100 point pending', () => {
  const current = {
    manualAnswers: {
      '442': {
        dyeGuide: {
          entries: [{ slotLabel: '武器', family: '红', dye: '果酒红', source: 'NPC霞，216g' }]
        },
        scorePlans: [
          {
            title: '80 分作业',
            entries: [
              '1. 六个精准染色',
              '2. 胸/手/腿/脚任意一件亚拉戈高位 + 两个精准染色'
            ]
          }
        ]
      }
    },
    sources: {
      'qq-cn-history': {
        current: {
          globalIssue: 442,
          theme: '测试主题',
          slots: [{ slotLabel: '身体', tag: '亚拉戈高位', answerText: '亚拉戈高位系列身体防具' }]
        }
      }
    }
  }
  const output = formatFashionCheckAnswer(current, shanghaiDate('2026-07-14T18:00:00'))
  assert.match(output, /80 分作业[\s\S]*1\. 六个精准染色/)
  assert.match(output, /2\. 胸\/手\/腿\/脚任意一件亚拉戈高位 \+ 两个精准染色/)
  assert.match(output, /100 分方案未更新/)
  assert.doesNotMatch(output, /80\/100 方案未更新|作业未更新/)
})

test('locks completed answer pushes per issue after both score plans exist', () => {
  const current = {
    manualAnswers: {
      '442': {
        scorePlans: [
          { title: '80 分作业', entries: ['六个精准染色'] },
          { title: '100 分方案', entries: ['身体：任意可染色身体装备 / 盗龙蓝染剂'] }
        ]
      }
    },
    sources: {
      'qq-cn-history': {
        current: { globalIssue: 442, answerCount: 4, tagCount: 4 }
      },
      'allgamestaff-en': { globalIssue: 442, tableCount: 3 }
    }
  }
  assert.deepEqual(getScorePlanStatus(current, 442), { has80: true, has100: true })
  assert.equal(scorePlansComplete(current, 442), true)
  assert.equal(manualScorePlansComplete(current, 442), true)
  assert.equal(shouldLockAnswerPush({}, current, shanghaiDate('2026-07-18T18:00:00')), true)
  assert.equal(
    shouldLockAnswerPush(
      { answerPushLocks: { '442': { lockedAt: '2026-07-18T18:00:00+08:00' } } },
      current,
      shanghaiDate('2026-07-18T18:00:00')
    ),
    false
  )
})

test('does not lock a source-complete issue before manual confirmation', () => {
  const current = {
    sources: {
      'qq-cn-history': { current: { globalIssue: 442 } },
      'allgamestaff-en': {
        globalIssue: 442,
        tables: [
          { heading: 'How to score 80 Points', rows: [['Slot', 'Item'], ['Body', 'Source 80']] },
          { heading: 'How to achieve 100 Points', rows: [['Slot', 'Item'], ['Body', 'Source 100']] }
        ]
      }
    }
  }
  assert.equal(scorePlansComplete(current, 442), true)
  assert.equal(manualScorePlansComplete(current, 442), false)
  assert.equal(shouldLockAnswerPush({}, current, shanghaiDate('2026-07-18T18:00:00')), false)
})

test('manual score plans take precedence over duplicate source plans', () => {
  const current = {
    manualAnswers: {
      '442': {
        scorePlans: [
          { title: '80 分作业', entries: ['手工 80 分方案'] },
          { title: '100 分方案', entries: ['手工 100 分方案'] }
        ]
      }
    },
    sources: {
      'qq-cn-history': {
        current: {
          globalIssue: 442,
          theme: '测试主题',
          slots: []
        }
      },
      'allgamestaff-en': {
        globalIssue: 442,
        tables: [
          {
            heading: 'How to score 80 Points',
            rows: [['Slot', 'Item'], ['Body', 'Source 80']]
          },
          {
            heading: 'How to achieve 100 Points',
            rows: [['Slot', 'Item'], ['Body', 'Source 100']]
          }
        ]
      }
    }
  }
  const output = formatFashionCheckAnswer(current, shanghaiDate('2026-07-18T18:00:00'))
  assert.match(output, /手工 80 分方案/)
  assert.match(output, /手工 100 分方案/)
  assert.doesNotMatch(output, /Source 80|Source 100/)
})

test('uses owner-confirmed answers ahead of unverified public source text', () => {
  const source = {
    current: {
      globalIssue: 442,
      slots: [
        { slotId: 'body', answerText: null },
        { slotId: 'hands', answerText: '公开答案' },
        { slotId: 'legs', answerText: '' }
      ]
    }
  }
  const merged = applyManualQqAnswers(source, {
    '442': {
      answers: {
        body: '亚拉戈高位系列身体防具',
        hands: '亚拉戈高位系列手部防具',
        legs: '亚拉戈高位系列腿部防具'
      }
    }
  })
  assert.equal(merged.current.slots[0].answerText, '亚拉戈高位系列身体防具')
  assert.equal(merged.current.slots[0].matches[0].grade, 'gold')
  assert.equal(merged.current.slots[0].matches[0].points, 8)
  assert.equal(merged.current.slots[1].answerText, '亚拉戈高位系列手部防具')
  assert.equal(merged.current.slots[1].matches[0].verification, 'owner-confirmed')
  assert.equal(merged.current.slots[2].answerText, '亚拉戈高位系列腿部防具')
  assert.equal(merged.current.answerCount, 3)
})

test('uses a manual Tuesday preview when the public source is still on the previous week', () => {
  const source = {
    current: {
      globalIssue: 443,
      cnIssue: 428,
      startDate: '2026-07-21',
      theme: '旧主题',
      slots: [{ slotId: 'head', slotLabel: '头部', tag: '旧标签', answerText: '旧答案' }],
      dyes: [{ slotId: 'head' }],
      answerCount: 1,
      tagCount: 1
    }
  }
  const merged = applyManualQqAnswers(
    source,
    {
      '444': {
        preview: {
          cnIssue: 429,
          startDate: '2026-07-28',
          theme: '风信子冒险者',
          themeId: 453,
          slots: [
            { slotId: 'head', slotLabel: '头部', tag: '风信子' },
            { slotId: 'body', slotLabel: '身体', tag: '西格玛' },
            { slotId: 'hands', slotLabel: '手部', tag: '冒险的开始' },
            { slotId: 'legs', slotLabel: '腿部', tag: '装饰钉扣' }
          ]
        }
      }
    },
    444
  )

  assert.equal(merged.current.globalIssue, 444)
  assert.equal(merged.current.cnIssue, 429)
  assert.equal(merged.current.theme, '风信子冒险者')
  assert.equal(merged.current.themeId, 453)
  assert.equal(merged.current.slots.length, 4)
  assert.equal(merged.current.slots[2].tag, '冒险的开始')
  assert.equal(merged.current.answerCount, 0)
  assert.deepEqual(merged.current.dyes, [])
  assert.deepEqual(merged.current.slots[0].matches, [])
})

test('calculates a score only when every contribution has evidence', () => {
  const verified = calculateScorePlan({
    baseScore: 68,
    equipment: [{ slot: 'Body', grade: 'gold', points: 8 }],
    dyes: [{ slot: 'Head', kind: 'exact' }, { slot: 'Body', kind: 'exact' }],
    targetScore: 80
  })
  assert.equal(verified.calculatedScore, 80)
  assert.equal(verified.verified, true)
  assert.equal(verified.meetsTarget, true)

  const unresolved = calculateScorePlan({
    baseScore: 68,
    equipment: [{ slot: 'Body', grade: 'silver', points: null }],
    targetScore: 80
  })
  assert.equal(unresolved.verified, false)
  assert.equal(unresolved.meetsTarget, null)
  assert.equal(unresolved.unresolved[0].reason, 'match-score-missing')
})

test('decodes a QQ t=3 cell operation and extracts the current week', () => {
  const width = 23
  const height = 21
  const cells = {}
  const put = (row, column, value) => {
    cells[String(row * width + column)] = { 2: [typeof value === 'number' ? 2 : 1, value] }
  }
  put(8, 13, '军装')
  put(9, 0, 442)
  put(11, 2, '测试主题')
  put(11, 3, '知性')
  put(11, 4, '简约')
  put(11, 5, '无赖')
  put(11, 6, '蛮族工匠')
  put(12, 3, '黄铜眼镜')
  put(12, 4, '春意衬衫')
  const payload = {
    clientVars: {
      collab_client_vars: {
        initialAttributedText: {
          text: [[[{ t: 3, v: 5, c: [['BB08J2', 0, height - 1, 0, width - 1], cells] }]]]
        }
      }
    }
  }
  const rows = decodeQqSheetJsonp(`clientVarsCallback(${JSON.stringify(payload)});`)
  const current = extractQqCurrentWeek(rows)
  assert.equal(rows.length, height)
  assert.equal(current.globalIssue, 442)
  assert.equal(current.cnIssue, 427)
  assert.equal(current.theme, '测试主题')
  assert.equal(current.tagCount, 4)
  assert.equal(current.answerCount, 2)
  assert.deepEqual(current.dyes, [])
})

test('parses quoted CSV fields', () => {
  assert.deepEqual(parseCsv('a,b\r\n1,"two,three"\r\n'), [
    ['a', 'b'],
    ['1', 'two,three']
  ])
})

test('parses WordPress tables without using string-based HTML extraction', () => {
  const entry = {
    id: 1,
    modified: '2026-07-17T10:00:00',
    link: 'https://example.test/fashion',
    title: { rendered: 'FFXIV Fashion Report Guide - Week 442' },
    content: {
      rendered: `
        <h2>How to achieve 100 Points</h2>
        <figure><table><tr><th>Slot</th><th>Item</th></tr><tr><td>Head</td><td>Brass Spectacles</td></tr></table></figure>
        <h2>How to score 80 Points</h2>
        <table><tr><th>Slot</th><th>Item</th></tr><tr><td>Body</td><td>Spring Shirt</td></tr></table>
        <h2>Extra: all accepted items per category</h2>
        <table>
          <tr><th>Slot</th><th>Undyed Score</th><th>Item</th></tr>
          <tr><td>Head</td><td>Gold (8+ pts)</td><td>Brass Spectacles</td></tr>
          <tr><td>Silver Spectacles</td></tr>
          <tr><td>Body</td><td>Silver (3+ pts)</td><td>Spring Shirt</td></tr>
        </table>
      `
    }
  }
  const parsed = parseAllGameStaffEntry(entry, 'allgamestaff-en')
  assert.equal(parsed.globalIssue, 442)
  assert.equal(parsed.tableCount, 3)
  assert.deepEqual(parsed.tables[0].rows[1], ['Head', 'Brass Spectacles'])
  assert.deepEqual(parsed.scoreEvidence[0], {
    slot: 'Head',
    equipmentText: 'Brass Spectacles',
    grade: 'gold',
    points: 8,
    verification: 'source-explicit-score',
    sourceId: 'allgamestaff-en',
    evidence: { tableHeading: 'Extra: all accepted items per category', scoreText: 'Gold (8+ pts)' }
  })
  assert.equal(parsed.scoreEvidence[1].equipmentText, 'Silver Spectacles')
  assert.equal(parsed.scoreEvidence[1].grade, 'gold')
  assert.equal(parsed.scoreEvidence[2].points, 3)
  assert.equal(parsed.scorePlans.length, 2)
  assert.equal(parsed.scorePlans[0].targetScore, 100)
  assert.equal(parsed.scorePlans[0].audit.verified, false)
  assert.equal(parsed.scorePlans[0].audit.unresolved[0].reason, 'base-score-missing')
})
