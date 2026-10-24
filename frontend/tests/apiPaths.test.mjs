import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * 「后端集合路由要尾斜杠，前端却少写一个」这类坑的绊线。
 *
 * 背景（作者实测）：伏笔面板一直显示「Failed to fetch」。原因不是网络，而是
 * 前端调 `/api/projects/{id}/foreshadows`（无尾斜杠），后端路由是
 * `/foreshadows/`（`APIRouter(prefix=...)` + `@router.get("/")`）→ 返回 **307**，
 * 且 Location 是**绝对地址**（走 Vite 代理时 Host 已变成后端自己）→ 浏览器直接跨源
 * 请求后端端口 → `net::ERR_FAILED`。而「章节」用的是 `/chapters/`，所以一直没事。
 *
 * 这类坑靠人眼很难守（两个文件、一个字符），所以这里做静态绊线：
 *   1. 扫后端 `app/api/*.py`：哪些 router 的根路由写成了 `"/"`（必须带尾斜杠）；
 *   2. 扫前端 `services/api.ts`：项目下的集合调用若写成裸路径（路径在集合名后就结束），
 *      且该集合属于第 1 步的名单 → 报错。
 *
 * 它不是完整的路由校验（不解析 FaskAPI 路由表），但能挡住这个已知形态，
 * 且第 3 条用例会在「扫描失效」时自己变红，避免绊线静默失效。
 */

const HERE = fileURLToPath(new URL('.', import.meta.url));
const FRONTEND = join(HERE, '..');
const BACKEND_API = join(FRONTEND, '..', 'backend', 'app', 'api');

/** 后端：prefix 末段 → 根路由写成了什么（"/" 需要尾斜杠，"" 反之）。 */
function backendCollectionRoots() {
  const roots = new Map();
  for (const file of readdirSync(BACKEND_API)) {
    if (!file.endsWith('.py')) continue;
    const source = readFileSync(join(BACKEND_API, file), 'utf8');
    const prefix = source.match(/APIRouter\(\s*prefix\s*=\s*["']([^"']+)["']/);
    if (!prefix) continue;
    const segments = prefix[1].replace(/\/+$/, '').split('/');
    const last = segments[segments.length - 1];
    if (!last || last.startsWith('{')) continue; // 单数资源（如 /projects/{id}）不在本规则内
    if (/@router\.(get|post|put|patch|delete)\(\s*"\/"\s*\)/.test(source)) roots.set(last, '/');
    else if (/@router\.(get|post|put|patch|delete)\(\s*""\s*\)/.test(source)) roots.set(last, '');
  }
  return roots;
}

/** 前端：api.ts 里所有「项目下的集合」调用，以及它们是否带尾斜杠。 */
function frontendProjectCollectionCalls() {
  const source = readFileSync(join(FRONTEND, 'src', 'services', 'api.ts'), 'utf8');
  const calls = [];
  const pattern = /\/projects\/\$\{[a-zA-Z]+\}\/([a-zA-Z][a-zA-Z-]*)(\/?)(?=[`'"])/g;
  for (const match of source.matchAll(pattern)) {
    calls.push({ collection: match[1], trailingSlash: match[2] === '/', raw: match[0] });
  }
  return calls;
}

test('扫描能认出后端两侧的集合路由写法', () => {
  const roots = backendCollectionRoots();

  // 至少要有已知的两条；扫描失效时这里先红，避免下面的规则变成空转
  assert.equal(roots.get('chapters'), '/', 'chapters 的根路由写成 "/" → 前端必须带尾斜杠');
  assert.equal(roots.get('foreshadows'), '/', 'foreshadows 的根路由写成 "/" → 前端必须带尾斜杠');
  assert.equal(roots.get('story-state'), '', 'story-state 的根路由写成 "" → 前端不能带尾斜杠');
});

test('前端调用的尾斜杠必须与后端根路由一致（否则会 307 → Failed to fetch）', () => {
  const roots = backendCollectionRoots();
  const problems = [];

  for (const call of frontendProjectCollectionCalls()) {
    const root = roots.get(call.collection);
    if (root === undefined) continue;
    const wantsSlash = root === '/';
    if (wantsSlash && !call.trailingSlash) {
      problems.push(`${call.raw}  ← 后端根路由是 "/"，必须写成 /${call.collection}/`);
    }
    if (!wantsSlash && call.trailingSlash) {
      problems.push(`${call.raw}  ← 后端根路由是 ""，不能带尾斜杠`);
    }
  }

  assert.deepEqual(problems, [], `尾斜杠与后端不一致：\n${problems.join('\n')}`);
});

test('伏笔列表与创建这两条已知路径确实带上了尾斜杠', () => {
  const calls = frontendProjectCollectionCalls().filter((call) => call.collection === 'foreshadows');

  assert.ok(calls.length >= 2, 'api.ts 里应至少有伏笔的列表与创建两个调用');
  assert.ok(
    calls.every((call) => call.trailingSlash),
    `伏笔的集合调用必须全部带尾斜杠，实际：${JSON.stringify(calls.map((c) => c.raw))}`,
  );
});
