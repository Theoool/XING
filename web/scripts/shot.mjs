/** 用本机 Chrome 截图指定路径，并回收控制台错误。
 *
 * 为什么需要它：typecheck 与 build 只能证明「能编译」，证明不了「能渲染」。
 * 取证页是纯数据展示，最容易死的地方是运行时（字段缺失、表格越界、
 * 空数组上取 [0]），这些在构建阶段全是绿的。
 *
 * 用法：node scripts/shot.mjs <url> <out.png> [X-User-ID]
 *
 * 第三个参数是开发期的租户替身（后端 deps.py 支持 X-User-ID 覆盖）。
 * 脚本跑链路时建的工作区属于别的用户，浏览器默认的 dev 身份看不见它 ——
 * 那是 RLS 正常工作，不是 bug，所以验证渲染时得显式换成那个身份。
 */
import { launch } from 'puppeteer-core'
import { mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

const [, , url, out, asUser] = process.argv
if (!url || !out) {
  console.error('usage: node scripts/shot.mjs <url> <out.png> [X-User-ID]')
  process.exit(2)
}

const browser = await launch({
  executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
})

const page = await browser.newPage()
await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 2 })
if (asUser) await page.setExtraHTTPHeaders({ 'X-User-ID': asUser })

const problems = []
page.on('console', (msg) => {
  if (msg.type() === 'error' || msg.type() === 'warning') {
    problems.push(`[${msg.type()}] ${msg.text()}`)
  }
})
page.on('pageerror', (err) => problems.push(`[pageerror] ${err.message}`))
page.on('requestfailed', (req) =>
  problems.push(`[requestfailed] ${req.url()} :: ${req.failure()?.errorText}`),
)

const resp = await page.goto(url, { waitUntil: 'networkidle2', timeout: 45000 })
await new Promise((r) => setTimeout(r, 1200))

const title = await page.title()
const text = await page.evaluate(() => document.body.innerText.slice(0, 600))

mkdirSync(dirname(out), { recursive: true })
await page.screenshot({ path: out, fullPage: true })

await browser.close()

console.log(JSON.stringify({ status: resp?.status(), title, text, problems }, null, 2))
