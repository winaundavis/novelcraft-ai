import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

/**
 * 静态守卫：**hook 依赖数组里不许放"每次渲染都换身份"的回调 prop**。
 *
 * 为什么需要它（两次真实事故）：
 *   1. `Modal` 的 effect 依赖里有 `onClose`，而 App 传的是内联箭头 `() => undefined`
 *      → 每次按键 effect 重跑、重新聚焦 → **输入框每打一个字就丢焦点**；
 *   2. `CharacterRelationGraph` 的建图 effect 依赖里有 `onSelectCharacter`，而面板传内联箭头
 *      → 父组件每次渲染都 `cy.destroy()` 重建整个 Cytoscape 图（点一下节点就重建）。
 *
 * 这两次**普通测试都抓不到**（第一次靠作者肉身发现，第二次靠审计脚本）。
 * 原因：不稳定性在**调用方**，被调方看起来完全无辜；
 * 而且 `eslint-plugin-react-hooks` 的 `exhaustive-deps` 认为 `[open, onClose]` 完全合法。
 * 所以只能静态检查，且必须检查两遍：被调方依赖里有没有 prop → 调用方是不是传了内联函数/对象/数组。
 *
 * 这个守卫是**静态**的，不是行为测试——但这一族缺陷恰恰无法用行为测试覆盖
 * （真实调用点没人传回调时，修复前后行为一模一样，测不出差别）。
 */

const HERE = fileURLToPath(new URL('.', import.meta.url));
const SRC_DIR = join(HERE, '..', 'src');
const TSX_EXTENSIONS = ['.ts', '.tsx'];
const HOOK_NAMES = new Set(['useEffect', 'useMemo', 'useCallback', 'useLayoutEffect']);

/** 递归收集 src 下的 .ts/.tsx */
export function collectSources(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...collectSources(full));
    else if (TSX_EXTENSIONS.some((ext) => full.endsWith(ext))) out.push(full);
  }
  return out;
}

function parse(filePath, text) {
  return ts.createSourceFile(filePath, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}

/** 组件第一个参数的对象解构 → prop 名集合 */
function propsOf(fnNode) {
  const props = new Set();
  const param = fnNode.parameters?.[0];
  if (!param || !ts.isObjectBindingPattern(param.name)) return props;
  for (const element of param.name.elements) {
    const name = element.propertyName && ts.isIdentifier(element.propertyName)
      ? element.propertyName.text
      : ts.isIdentifier(element.name) ? element.name.text : null;
    if (name) props.add(name);
  }
  return props;
}

/** 收集本文件里所有大写开头的函数/箭头函数组件：名字 → { props, node } */
function collectComponents(source) {
  const found = new Map();
  const visit = (node) => {
    let name = null;
    let fnNode = null;
    if (ts.isFunctionDeclaration(node) && node.name) {
      name = node.name.text;
      fnNode = node;
    } else if (
      ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) &&
      node.initializer && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))
    ) {
      name = node.name.text;
      fnNode = node.initializer;
    }
    if (name && /^[A-Z]/.test(name) && fnNode) {
      found.set(name, { props: propsOf(fnNode), node: fnNode });
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

/**
 * Pass1：找出"某个 hook 的依赖数组里出现了本组件 prop"的地方。
 * 返回 [{ file, line, component, hook, depsText, propNames }]
 */
export function findPropsInHookDeps(sources) {
  const hits = [];
  for (const [file, text] of sources) {
    const source = parse(file, text);
    for (const [componentName, info] of collectComponents(source)) {
      const visit = (node) => {
        if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && HOOK_NAMES.has(node.expression.text)) {
          const deps = node.arguments[1];
          if (deps && ts.isArrayLiteralExpression(deps)) {
            const propNames = deps.elements
              .filter((element) => ts.isIdentifier(element) && info.props.has(element.text))
              .map((element) => element.text);
            if (propNames.length) {
              hits.push({
                file,
                line: source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1,
                component: componentName,
                hook: node.expression.text,
                depsText: deps.getText(source).replace(/\s+/g, ' '),
                propNames,
              });
            }
          }
        }
        ts.forEachChild(node, visit);
      };
      visit(info.node);
    }
  }
  return hits;
}

/**
 * 调用点传的是不是"每次渲染都一定新建"的东西。
 *
 * 只判定四种**确定**会新建身份的写法（内联箭头/函数表达式/对象字面量/数组字面量）。
 * 刻意**不**把任意函数调用算进内联：`Boolean(x)` / `Number(x)` / `String(x)` 这类返回的是原始值，
 * 身份按值稳定（第一版把它们判成违规，是假阳性）。
 */
function isUnstableInitializer(initializer) {
  if (!initializer || !ts.isJsxExpression(initializer) || !initializer.expression) return false;
  const expr = initializer.expression;
  return ts.isArrowFunction(expr) || ts.isFunctionExpression(expr)
    || ts.isObjectLiteralExpression(expr) || ts.isArrayLiteralExpression(expr);
}

/**
 * Pass2：把这些 prop 的**真实调用点**找出来，看有没有传不稳定值。
 * 精确匹配 `<ComponentName ... propName={...} />`，避免同名 prop 的误报。
 */
export function findUnstableCallSites(sources, hits) {
  const violations = [];
  const wanted = new Map();
  for (const hit of hits) {
    const key = `${hit.component}\u0000${hit.propNames.join(',')}`;
    wanted.set(key, hit);
  }

  for (const [file, text] of sources) {
    const source = parse(file, text);
    const visit = (node) => {
      const tagName = ts.isJsxSelfClosingElement(node) ? node.tagName
        : ts.isJsxOpeningElement(node) ? node.tagName : null;
      if (tagName && ts.isIdentifier(tagName)) {
        const owner = wanted.get(`${tagName.text}\u0000${''}`); // 占位，见下
        void owner;
        for (const [key, hit] of wanted) {
          const [componentName, propsJoined] = key.split('\u0000');
          if (componentName !== tagName.text) continue;
          for (const propName of propsJoined.split(',')) {
            const attribute = node.attributes.properties.find(
              (item) => ts.isJsxAttribute(item) && item.name.getText(source) === propName,
            );
            if (attribute && isUnstableInitializer(attribute.initializer)) {
              violations.push({
                component: componentName,
                propName,
                file,
                line: source.getLineAndCharacterOfPosition(attribute.getStart(source)).line + 1,
                declaredAt: hit.file === file ? hit.line : hit.file,
              });
            }
          }
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
  return violations;
}

function toDisplayPath(file) {
  return relative(join(HERE, '..'), file).split(sep).join('/');
}

function loadSources() {
  const map = new Map();
  for (const file of collectSources(SRC_DIR)) map.set(file, readFileSync(file, 'utf8'));
  return map;
}

// ── 自检：检测器必须真的会报警 ──────────────────────────

test('守卫自检：检测器能抓出「依赖里放内联回调 prop」这个形态', () => {
  const child = [
    'export function Child({ onPick }: { onPick: (id: string) => void }) {',
    '  useEffect(() => { onPick("x"); }, [onPick]);',
    '  return null;',
    '}',
  ].join('\n');
  const parent = [
    'export function Parent() {',
    '  const [n, setN] = useState(0);',
    '  return <Child onPick={(id) => console.log(id, n)} />;',
    '}',
  ].join('\n');

  const sources = new Map([['child.tsx', child], ['parent.tsx', parent]]);
  const hits = findPropsInHookDeps(sources);
  assert.equal(hits.length, 1, '应当发现 1 处「依赖含本组件 prop」');
  assert.deepEqual(hits[0].propNames, ['onPick']);

  const violations = findUnstableCallSites(sources, hits);
  assert.equal(violations.length, 1, '应当把内联箭头回调判为违规');
  assert.equal(violations[0].propName, 'onPick');
});

test('守卫自检：回调来自稳定引用（useCallback / 模块级函数）时不算违规', () => {
  const child = [
    'export function Child({ onPick }: { onPick: (id: string) => void }) {',
    '  useEffect(() => { onPick("x"); }, [onPick]);',
    '  return null;',
    '}',
  ].join('\n');
  const parent = [
    'export function Parent() {',
    '  const handlePick = useCallback((id: string) => console.log(id), []);',
    '  return <Child onPick={handlePick} />;',
    '}',
  ].join('\n');

  const sources = new Map([['child.tsx', child], ['parent.tsx', parent]]);
  const violations = findUnstableCallSites(sources, findPropsInHookDeps(sources));
  assert.equal(violations.length, 0, '稳定引用不该被误报');
});

// ── 正式守卫 ───────────────────────────────────────────

test('全仓：没有 hook 依赖「调用方用内联函数/对象传进来的 prop」', () => {
  const sources = loadSources();
  const hits = findPropsInHookDeps(sources);

  // 扫描有效性下限：解析器坏掉时这条会先红，避免守卫静默失效。
  assert.ok(hits.length >= 30, `应当扫到足够多的「依赖含 prop」样本，实际 ${hits.length} 个（解析器可能失效）`);

  const violations = findUnstableCallSites(sources, hits);
  const lines = violations.map(
    (item) => `  ${toDisplayPath(item.file)}:${item.line}  <${item.component} ${item.propName}={...} />`,
  );
  assert.equal(
    violations.length,
    0,
    `发现 ${violations.length} 处「hook 依赖里放了会被内联重建的 prop」，` +
      `会让 hook 每次渲染都重跑：\n${lines.join('\n')}\n` +
      `改法：把回调放进 ref 供事件处理器读取，别放进依赖数组。`,
  );
});

test('回归钉子：两处历史事故的回调不再出现在依赖数组里', () => {
  const sources = loadSources();
  const hits = findPropsInHookDeps(sources);
  const text = (file) => {
    const hit = [...sources.entries()].find(([path]) => path.endsWith(file));
    assert.ok(hit, `找不到 ${file}`);
    return hit[1];
  };

  // Modal：建/拆弹层与焦点管理的 effect 只能依赖 open
  const modalHits = hits.filter((hit) => hit.file.endsWith(`${sep}Modal.tsx`.slice(1)));
  assert.deepEqual(
    modalHits.map((hit) => hit.depsText),
    ['[open]'],
    'Modal 的 effect 依赖必须只有 open —— 放进 onClose 会让每次按键重跑并抢走焦点',
  );

  // CharacterRelationGraph：建图 effect 不许依赖回调 prop
  const graphHits = hits.filter((hit) => hit.file.endsWith('CharacterRelationGraph.tsx'));
  for (const hit of graphHits) {
    assert.ok(
      !hit.propNames.includes('onSelectCharacter'),
      '建图 effect 不许依赖 onSelectCharacter —— 否则父组件每次渲染都会重建整张图',
    );
  }

  // ChapterContextBar：取数 effect 不能依赖 onStateChange（它一旦被内联传入就是无限请求循环）
  assert.ok(
    !hits.some(
      (hit) => hit.file.endsWith('ChapterContextBar.tsx') && hit.propNames.includes('onStateChange'),
    ),
    'ChapterContextBar 的取数 effect 不许依赖 onStateChange —— 那会构成无限请求循环',
  );
  assert.ok(text('ChapterContextBar.tsx').includes('onStateChangeRef'), '回调必须通过 ref 读取');
});
