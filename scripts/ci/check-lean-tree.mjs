#!/usr/bin/env node

import { createHash } from 'node:crypto'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { existsSync, lstatSync, readFileSync } from 'node:fs'

// Captured manual evidence and one-off diagnostic output. Formal fixtures use
// capability names under test_data/ instead.
const suspiciousProcessPath = (
  /(^|\/)(dist|build|coverage|screenshots?|reports?|results?|artifacts?|test-results?|playwright-report|\.cache)(\/|$)/i
)

// `operations/reports` is a production page domain, not a generated report
// directory. Keep the general `reports` artifact guard for every other path.
function isSuspiciousProcessPath(path) {
  const productionReports = 'frontend/src/pages/operations/reports/'
  if (path.startsWith(productionReports)) {
    return suspiciousProcessPath.test(path.slice(productionReports.length))
  }
  return suspiciousProcessPath.test(path)
}

const suspiciousProcessFile = (
  /(^|\/)(debug|tmp|temp|output|final)[^/]*\.(png|jpe?g|html|json|log|csv)$/i
)
const disposableExtension = /\.(pyc|pyo|log|bak|tmp|swp)$/i

function inspectRecords(records) {
  const errors = []
  const duplicateGroups = new Map()

  for (const { path, content } of records) {
    if (content.length === 0) {
      if (!path.endsWith('/__init__.py') && path !== '__init__.py') {
        errors.push(`zero-byte non-package file: ${path}`)
      }
      continue
    }

    const digest = createHash('sha256').update(content).digest('hex')
    const duplicateKey = `${content.length}:${digest}`
    const duplicates = duplicateGroups.get(duplicateKey) ?? []
    duplicates.push(path)
    duplicateGroups.set(duplicateKey, duplicates)

    if (
      isSuspiciousProcessPath(path)
      || suspiciousProcessFile.test(path)
      || disposableExtension.test(path)
    ) {
      errors.push(`process artifact or disposable output in source tree: ${path}`)
    }
  }

  const exactDuplicates = [...duplicateGroups.values()]
    .filter(paths => paths.length > 1)
  for (const paths of exactDuplicates) {
    errors.push(`exact non-empty duplicate files: ${paths.join(', ')}`)
  }
  return errors
}

if (process.argv.includes('--self-test')) {
  assert.deepEqual(
    inspectRecords([
      { path: 'src/package/__init__.py', content: Buffer.alloc(0) },
      { path: 'src/clean.ts', content: Buffer.from('export const clean = true\n') },
      {
        path: 'frontend/src/pages/operations/reports/OperationsReportBookPage.tsx',
        content: Buffer.from('export const productionPage = true\n'),
      },
    ]),
    [],
  )
  const fixtureErrors = inspectRecords([
    { path: 'src/empty.txt', content: Buffer.alloc(0) },
    { path: 'frontend/screenshots/result.png', content: Buffer.from('image') },
    { path: 'src/first.ts', content: Buffer.from('same') },
    { path: 'src/second.ts', content: Buffer.from('same') },
  ])
  assert(fixtureErrors.some(error => error.includes('zero-byte non-package')))
  assert(fixtureErrors.some(error => error.includes('process artifact')))
  assert(fixtureErrors.some(error => error.includes('exact non-empty duplicate')))
  process.stdout.write('Lean tree self-tests passed.\n')
  process.exit(0)
}

const candidateFiles = execFileSync(
  'git',
  ['ls-files', '--cached', '--others', '--exclude-standard', '-z'],
)
  .toString()
  .split('\0')
  .filter(path => path && existsSync(path) && lstatSync(path).isFile())
const errors = inspectRecords(candidateFiles.map(path => ({
  path,
  content: readFileSync(path),
})))

if (errors.length > 0) {
  for (const error of errors) {
    process.stderr.write(`ERROR [lean-tree] ${error}\n`)
  }
  process.exit(1)
}

process.stdout.write(
  `Lean tree passed: checked ${candidateFiles.length} files; `
  + 'no non-package zero-byte file, process artifact, or exact non-empty duplicate.\n',
)
