import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const workflow = readFileSync(new URL('../../.github/workflows/deploy-nano-ontoprompt.yml', import.meta.url), 'utf8')

function job(text, id) {
  const blocks = text.split(/^  (?=[\w-]+:\s*$)/m)
  const block = blocks.find(value => value.startsWith(`${id}:\n`))
  assert.ok(block, `missing job: ${id}`)
  return block
}

function checkDeployLock(text) {
  assert.doesNotMatch(text, /^concurrency:/m, 'only the production job may hold the deployment lock')
  assert.match(job(text, 'deploy'), /^    concurrency:\n      group: deploy-nano-ontoprompt\n      cancel-in-progress: false\n      queue: max$/m)
  assert.equal([...text.matchAll(/^\s+group: deploy-nano-ontoprompt$/gm)].length, 1)
}

checkDeployLock(workflow)
for (const invalid of [
  `concurrency:\n  group: test\n${workflow}`,
  workflow.replace('cancel-in-progress: false', 'cancel-in-progress: true'),
  workflow.replace('      cancel-in-progress: false\n', ''),
  workflow.replace('group: deploy-nano-ontoprompt', 'group: deploy-${{ github.run_id }}'),
  workflow.replace('      queue: max\n', ''),
  workflow.replace('  deploy:\n', '  deploy:\n    intervening: true\n  other:\n'),
]) {
  assert.throws(() => checkDeployLock(invalid))
}

function step(block, name) {
  const result = block.split(/^      - /m).find(value => value.startsWith(`name: ${name}\n`))
  assert.ok(result, `missing step: ${name}`)
  return result
}

const deploy = job(workflow, 'deploy')
const checkTipName = 'Check production branch tip under the deployment lock'
const tip = step(deploy, checkTipName)
for (const name of [
  'Download frontend dist', 'Check deployment secrets', 'Trust deployment host',
  'Materialize production dependency manifest', 'Generate backend runtime source manifest',
  'Upload source to server', 'Deploy over SSH',
]) {
  const remoteStep = step(deploy, name)
  assert.ok(deploy.indexOf(tip) < deploy.indexOf(remoteStep))
  assert.match(remoteStep, /^        if: steps\.tip\.outputs\.current == 'true'$/m)
}
assert.match(deploy, /^      release_result: \$\{\{ steps\.release\.outcome \}\}$/m)
assert.match(deploy, /^      superseded: \$\{\{ steps\.tip\.outputs\.superseded \}\}$/m)

const docker = job(workflow, 'verify-docker-build')
assert.match(docker, /^    needs: \[changes, verify-frontend-build\]$/m)
assert.match(docker, /needs\.verify-frontend-build\.result == 'success'/)
const e2e = job(workflow, 'verify-frontend-e2e')
assert.match(e2e, /^      fail-fast: false\n      matrix:\n        shard: \[1, 2\]$/m)
assert.match(e2e, /npm run test:e2e:mocked -- --shard=\$\{\{ matrix\.shard \}\}\/2/)
assert.match(e2e, /name: frontend-playwright-.*-shard-\$\{\{ matrix\.shard \}\}/)
for (const [id, flag] of [
  ['verify-frontend-static', 'run_frontend_tests'],
  ['verify-frontend-build', 'run_frontend_build'],
  ['verify-frontend-e2e', 'run_frontend_tests'],
]) {
  assert.ok(job(workflow, id).includes(`if: needs.changes.outputs.${flag} == 'true'`))
  assert.ok(deploy.includes(`needs.${id}.result == 'success'`))
}

const report = job(workflow, 'report')
const needs = block => block.match(/^    needs: \[([^\]]+)\]$/m)[1].split(', ')
assert.deepEqual(needs(report).sort(), [...needs(deploy), 'deploy'].sort())

// Exercise the actual github-script bodies with stubbed Git and GitHub, so a
// skipped/stale release cannot accidentally become a trusted baseline.
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
function script(block, name) {
  const body = step(block, name).split('          script: |\n')[1]
  assert.ok(body, `missing script: ${name}`)
  return new AsyncFunction('context', 'github', 'core', 'require', 'process', body)
}

const sha = 'a'.repeat(40)
const next = 'b'.repeat(40)
const ref = 'refs/heads/nano-ontoprompt'
const context = { sha, ref, repo: { owner: 'fixture', repo: 'fixture' }, payload: { before: sha } }
const baseline = script(job(workflow, 'changes'), 'Check previous push was released or needed no deployment')
for (const [state, description, trusted] of [
  ['success', 'Production deployment succeeded', true],
  ['success', 'Verification succeeded (no deployment needed)', true],
  ['success', 'Verification succeeded (superseded; not deployed)', false],
  ['success', 'Verification and deployment succeeded', false],
  ['failure', 'Production deployment succeeded', false],
  ['pending', 'Verification and deployment are running', false],
  ['', '', false],
  ['unreachable', '', false],
]) {
  const getCombinedStatusForRef = async ({ ref: before }) => {
    assert.equal(before, sha)
    if (state === 'unreachable') throw new Error('unavailable')
    return { data: { statuses: state ? [{ state, description, context: 'openontology/deploy' }] : [] } }
  }
  const outputs = {}
  await baseline(context, { rest: { repos: { getCombinedStatusForRef } } }, {
    setOutput: (key, value) => { outputs[key] = value }, warning: () => {},
  })
  assert.equal(outputs.ready, trusted ? 'true' : undefined, description)
}

// Run the production filter with real Git ancestry and the existing classifier.
const filter = step(job(workflow, 'changes'), 'Classify deployment changes').split('        run: |\n')[1]
assert.ok(filter)
const fixture = mkdtempSync(join(tmpdir(), 'openontology-workflow-'))
try {
  const git = (...args) => execFileSync('git', args, { cwd: fixture, encoding: 'utf8' }).trim()
  git('init', '--quiet')
  git('config', 'user.name', 'Deployment fixture')
  git('config', 'user.email', 'fixture@example.invalid')
  git('config', 'commit.gpgSign', 'false')
  git('config', 'core.hooksPath', '/dev/null')
  mkdirSync(join(fixture, 'scripts', 'ci'), { recursive: true })
  symlinkSync(fileURLToPath(new URL('./classify-deploy-changes.sh', import.meta.url)),
    join(fixture, 'scripts', 'ci', 'classify-deploy-changes.sh'))
  git('commit', '--quiet', '--allow-empty', '-m', 'Initial state')
  const initial = git('rev-parse', 'HEAD')
  mkdirSync(join(fixture, 'backend'))
  writeFileSync(join(fixture, 'backend', 'product.py'), '# fixture\n')
  git('add', 'backend')
  git('commit', '--quiet', '-m', 'Unreleased product change')
  const before = git('rev-parse', 'HEAD')
  writeFileSync(join(fixture, 'README.md'), '# Documentation only\n')
  git('add', 'README.md')
  git('commit', '--quiet', '-m', 'Documentation push')
  const head = git('rev-parse', 'HEAD')
  git('checkout', '--quiet', '--detach', initial)
  git('commit', '--quiet', '--allow-empty', '-m', 'Different history')
  const nonAncestor = git('rev-parse', 'HEAD')

  function classify(baseline, ready) {
    const output = execFileSync('bash', ['-c', filter], {
      cwd: fixture, encoding: 'utf8',
      env: { ...process.env, BEFORE: baseline, BASELINE_READY: ready, GITHUB_SHA: head, GITHUB_OUTPUT: '' },
    })
    return Object.fromEntries(output.trim().split('\n').map(line => line.split('=')))
  }
  const full = {
    run_backend: 'true', run_frontend_tests: 'true', run_frontend_build: 'true',
    run_docker: 'true', run_deploy: 'true',
  }
  assert.deepEqual(classify(before, 'true'), Object.fromEntries(Object.keys(full).map(flag => [flag, 'false'])))
  assert.deepEqual(classify(before, ''), full, 'a docs push must still verify an unreleased predecessor')
  assert.deepEqual(classify(nonAncestor, 'true'), full, 'a divergent validated commit is not a baseline')
  assert.deepEqual(classify('', ''), full, 'manual dispatch verifies everything')
} finally {
  rmSync(fixture, { recursive: true, force: true })
}

const checkTip = script(deploy, checkTipName)
async function checkTipWith({ remote = sha, status, branchRef = ref, disconnected = false } = {}) {
  const execFileSync = (exe, args) => {
    assert.equal(exe, 'git')
    assert.deepEqual(args, ['ls-remote', '--exit-code', 'origin', ref])
    if (disconnected) throw new Error('Git unavailable')
    return `${remote}\t${ref}\n`
  }
  const listWorkflowRuns = async ({ head_sha, branch }) => {
    assert.equal(head_sha, next)
    assert.equal(branch, 'nano-ontoprompt')
    const runs = status ? [{ id: 1, head_sha: next, event: 'push', status }] : []
    return { data: { workflow_runs: runs } }
  }
  const outputs = {}
  await checkTip({ ...context, ref: branchRef }, { rest: { actions: { listWorkflowRuns } } }, {
    setOutput: (key, value) => { outputs[key] = value }, notice: () => {},
  }, () => ({ execFileSync }))
  return outputs
}
assert.deepEqual(await checkTipWith(), { current: 'true' })
assert.deepEqual(await checkTipWith({ remote: next, status: 'queued' }), { superseded: 'true' })
await assert.rejects(checkTipWith({ remote: next }), /no pending or successful deployment/)
await assert.rejects(checkTipWith({ remote: next, status: 'completed' }), /no pending or successful deployment/)
await assert.rejects(checkTipWith({ branchRef: 'refs/tags/old' }), /Production deployment requires/)
await assert.rejects(checkTipWith({ disconnected: true }), /Git unavailable/)

const publish = script(report, 'Publish deployment result')
async function checkReport(deployJob, { runDeploy = 'true', failedJob } = {}) {
  const jobs = Object.fromEntries(needs(report).map(id => [id, { result: 'success', outputs: {} }]))
  jobs.changes.outputs = {
    run_backend: 'true', run_frontend_tests: 'true', run_frontend_build: runDeploy,
    run_docker: runDeploy, run_deploy: runDeploy,
  }
  if (failedJob) jobs[failedJob].result = 'failure'
  jobs.deploy = deployJob
  let status
  let failed = false
  const createCommitStatus = async value => { status = value }
  await publish(context, { rest: { repos: { createCommitStatus } } }, {
    notice: () => {}, setFailed: () => { failed = true },
  }, undefined, { env: { JOBS: JSON.stringify(jobs) } })
  assert.equal(status.context, 'openontology/deploy')
  assert.equal(failed, status.state === 'failure')
  return { state: status.state, description: status.description }
}
const released = { result: 'success', outputs: { release_result: 'success' } }
assert.deepEqual(await checkReport(released), { state: 'success', description: 'Production deployment succeeded' })
const superseded = { result: 'success', outputs: { superseded: 'true', release_result: 'skipped' } }
assert.deepEqual(await checkReport(superseded), { state: 'success', description: 'Verification succeeded (superseded; not deployed)' })
assert.deepEqual(await checkReport({ result: 'skipped' }, { runDeploy: 'false' }), {
  state: 'success', description: 'Verification succeeded (no deployment needed)',
})
for (const failedJob of ['changes', 'verify-frontend-static', 'verify-frontend-build', 'verify-frontend-e2e', 'verify-docker-build']) {
  assert.equal((await checkReport(released, { failedJob })).state, 'failure')
}
assert.equal((await checkReport({ result: 'success', outputs: {} })).state, 'failure')
assert.equal((await checkReport({ result: 'cancelled' })).state, 'failure')
console.log('ok deploy workflow lock, gates, branch tip and release status')
