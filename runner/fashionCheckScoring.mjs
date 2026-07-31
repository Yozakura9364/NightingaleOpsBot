const ACCESSORY_SLOT_IDS = new Set(['ears', 'neck', 'wrists', 'rightRing', 'leftRing'])

function cleanText(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim()
}

function normalizedItem(value) {
  return cleanText(value).toLocaleLowerCase('en-US')
}

function hasNumericValue(value) {
  return value !== null && value !== '' && value !== undefined && Number.isFinite(Number(value))
}

export function goldPointsForSlot(slotId) {
  return ACCESSORY_SLOT_IDS.has(String(slotId || '').trim()) ? 6 : 8
}

export function createUnverifiedMatch(slotId, equipmentText, sourceId, evidence = {}) {
  const text = cleanText(equipmentText)
  if (!text) return null
  return {
    equipmentText: text,
    grade: 'unverified',
    points: null,
    verification: 'source-text-only',
    sourceId,
    evidence
  }
}

export function createManualGoldMatch(slotId, value) {
  const input = typeof value === 'object' && value ? value : { equipmentText: value }
  const equipmentText = cleanText(input.equipmentText ?? input.text ?? input.answerText)
  if (!equipmentText) return null
  return {
    equipmentText,
    grade: 'gold',
    points: hasNumericValue(input.points) ? Number(input.points) : goldPointsForSlot(slotId),
    verification: 'owner-confirmed',
    sourceId: 'owner-manual',
    evidence: { kind: 'manual-confirmation', updatedAt: input.updatedAt ?? null }
  }
}

export function parseScoreBand(value) {
  const text = cleanText(value)
  if (!text) return null
  const grade = /\bgold\b/i.test(text) ? 'gold' : /\bsilver\b/i.test(text) ? 'silver' : null
  if (!grade) return null
  const points = text.match(/(\d+)\s*\+?\s*(?:pt|pts|point|points)\b/i)
  return { grade, points: points ? Number(points[1]) : null, text }
}

export function extractScoreEvidence(tables, sourceId) {
  const evidence = []
  for (const table of tables || []) {
    const rows = Array.isArray(table?.rows) ? table.rows : []
    const header = (rows[0] || []).map(cleanText)
    const slotIndex = header.findIndex((cell) => /slot|equip/i.test(cell))
    const scoreIndex = header.findIndex((cell) => /score|point/i.test(cell))
    const itemIndex = header.findIndex((cell) => /item|equipment/i.test(cell))
    if (slotIndex < 0 || scoreIndex < 0 || itemIndex < 0) continue

    let activeSlot = ''
    let activeBand = null
    for (const rawRow of rows.slice(1)) {
      const row = rawRow.map(cleanText)
      if (row.length === 1) {
        if (!activeSlot || !activeBand || !row[0]) continue
        evidence.push({
          slot: activeSlot,
          equipmentText: row[0],
          grade: activeBand.grade,
          points: activeBand.points,
          verification: 'source-explicit-score',
          sourceId,
          evidence: { tableHeading: table.heading || '', scoreText: activeBand.text }
        })
        continue
      }

      const slot = cleanText(row[slotIndex])
      const band = parseScoreBand(row[scoreIndex])
      const item = cleanText(row[itemIndex])
      if (slot) activeSlot = slot
      if (band) activeBand = band
      if (!item || !activeSlot || !activeBand) continue
      evidence.push({
        slot: activeSlot,
        equipmentText: item,
        grade: activeBand.grade,
        points: activeBand.points,
        verification: 'source-explicit-score',
        sourceId,
        evidence: { tableHeading: table.heading || '', scoreText: activeBand.text }
      })
    }
  }
  return evidence
}

function dyePoints(dye) {
  if (hasNumericValue(dye?.points)) return Number(dye.points)
  const kind = cleanText(dye?.kind).toLocaleLowerCase('en-US')
  if (kind === 'exact') return 2
  if (kind === 'same-family') return 1
  return null
}

export function calculateScorePlan({ baseScore, equipment = [], dyes = [], targetScore = null } = {}) {
  const contributions = []
  const unresolved = []
  let total = 0

  if (hasNumericValue(baseScore)) {
    const points = Number(baseScore)
    contributions.push({ kind: 'base', points })
    total += points
  } else {
    unresolved.push({ kind: 'base', reason: 'base-score-missing' })
  }

  for (const match of equipment) {
    if (hasNumericValue(match?.points)) {
      const points = Number(match.points)
      contributions.push({ kind: 'equipment', slot: match.slot ?? null, grade: match.grade ?? null, points })
      total += points
    } else {
      unresolved.push({ kind: 'equipment', slot: match?.slot ?? null, reason: 'match-score-missing' })
    }
  }

  for (const dye of dyes) {
    const points = dyePoints(dye)
    if (points === null) {
      unresolved.push({ kind: 'dye', slot: dye?.slot ?? null, reason: 'dye-score-missing' })
      continue
    }
    contributions.push({ kind: 'dye', slot: dye?.slot ?? null, kindDetail: dye.kind ?? null, points })
    total += points
  }

  return {
    calculatedScore: total,
    targetScore: hasNumericValue(targetScore) ? Number(targetScore) : null,
    verified: unresolved.length === 0,
    meetsTarget: hasNumericValue(targetScore) && unresolved.length === 0 ? total >= Number(targetScore) : null,
    contributions,
    unresolved
  }
}

export function findEvidenceForItem(evidence, slot, equipmentText) {
  const expectedItem = normalizedItem(equipmentText)
  const expectedSlot = normalizedItem(slot)
  return (evidence || []).find(
    (entry) =>
      normalizedItem(entry.slot) === expectedSlot && normalizedItem(entry.equipmentText) === expectedItem
  ) ?? null
}

export function extractScorePlans(tables, scoreEvidence) {
  const plans = []
  for (const table of tables || []) {
    const target = cleanText(table?.heading).match(/\b(80|100)\s*(?:points?|pts?)\b/i)
    if (!target) continue
    const rows = Array.isArray(table?.rows) ? table.rows : []
    const header = (rows[0] || []).map(cleanText)
    const slotIndex = header.findIndex((cell) => /slot|equip/i.test(cell))
    const itemIndex = header.findIndex((cell) => /item|equipment/i.test(cell))
    const dyeIndex = header.findIndex((cell) => /dye|colour|color/i.test(cell))
    if (slotIndex < 0 || itemIndex < 0) continue

    const rowsWithEvidence = []
    for (const row of rows.slice(1)) {
      const slot = cleanText(row[slotIndex])
      const equipmentText = cleanText(row[itemIndex])
      if (!slot || !equipmentText) continue
      rowsWithEvidence.push({
        slot,
        equipmentText,
        dyeText: dyeIndex >= 0 ? cleanText(row[dyeIndex]) || null : null,
        match: findEvidenceForItem(scoreEvidence, slot, equipmentText)
      })
    }
    const audit = calculateScorePlan({
      targetScore: Number(target[1]),
      equipment: rowsWithEvidence.map((row) => ({
        slot: row.slot,
        grade: row.match?.grade ?? 'unverified',
        points: row.match?.points ?? null
      })),
      dyes: rowsWithEvidence
        .filter((row) => row.dyeText && row.dyeText !== '-' && row.dyeText !== '–')
        .map((row) => ({ slot: row.slot, kind: 'unverified', dyeText: row.dyeText }))
    })
    plans.push({ targetScore: Number(target[1]), heading: table.heading || '', rows: rowsWithEvidence, audit })
  }
  return plans
}
