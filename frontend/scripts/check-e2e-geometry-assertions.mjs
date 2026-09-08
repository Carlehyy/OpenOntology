// E2E 像素尺寸/坐标断言规范：boundingBox 的 width/height/x/y 禁止
// toBe/toEqual 精确相等断言——dvh/rem 分数像素在两次布局间存在浮点舍入差
// （CI Linux 实测 ~2e-5px，先例 6596ffa7），精确相等会间歇性误报。尺寸
// 一致性统一走 src/test/e2e/support/geometry.ts 的 expectSameBoundingBox
// （亚像素容差）；坐标断言用 toBeCloseTo。
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const testDir = join(frontendRoot, 'src', 'test', 'e2e')
const specs = readdirSync(testDir)
  .filter((name) => name.endsWith('.spec.ts'))
  .sort()

// 覆盖两类高危形态：接收端或参数端是标识符链上的
// .height/.width/.x/.y，如 expect(after?.height).toBe(before?.height)。
const exactPixelAssertion =
  /expect\(\s*[A-Za-z_$][\w$?.]*\.(?:height|width|x|y)\s*\)\s*\.(?:toBe|toEqual)\(|\.(?:toBe|toEqual)\(\s*[A-Za-z_$][\w$?.]*\.(?:height|width|x|y)\s*\)/

const violations = []
for (const spec of specs) {
  const content = readFileSync(join(testDir, spec), 'utf8')
  const lines = content.split('\n')
  lines.forEach((line, index) => {
    if (exactPixelAssertion.test(line)) {
      violations.push(`${spec}:${index + 1}: ${line.trim()}`)
    }
  })
}

if (violations.length > 0) {
  console.error('✗ E2E 像素尺寸精确相等断言（改用 expectSameBoundingBox）：')
  for (const violation of violations) {
    console.error(`  ${violation}`)
  }
  process.exit(1)
}

console.log(`E2E 断言规范通过：${specs.length} 个 spec 无像素级精确相等尺寸断言`)
