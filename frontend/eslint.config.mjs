import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';

/**
 * ESLint 9 扁平配置（2026-10-22 引入）。
 *
 * 刻意**不**整套启用 recommended：
 * 本仓库已有大量存量代码，一次性打开 `no-unused-vars` / `no-empty` 之类会产生
 * 上百条噪声告警，最后大家只会把 lint 关掉。这里只开两类规则：
 *
 * 1. **真实缺陷**（写错就会出错，且现状基本为零）：重复键、重复 case、不可达代码、
 *    自赋值、条件里写赋值、case 穿透、异步 Promise executor 等；
 * 2. **React Hooks 铁律**：`rules-of-hooks` 直接置 error（违反它 = 运行时崩溃或状态错乱），
 *    `exhaustive-deps` 置 warn（提醒但不拦）。
 *
 * 后续想把 `no-unused-vars`、type-aware 规则纳入门禁，请**先清理存量**再逐步打开，
 * 不要一次全开。
 */
const correctnessRules = {
  'no-dupe-keys': 'error',
  'no-dupe-args': 'error',
  'no-duplicate-case': 'error',
  'no-unreachable': 'error',
  'no-unsafe-finally': 'error',
  'no-self-assign': 'error',
  'no-self-compare': 'error',
  'no-cond-assign': 'error',
  'no-fallthrough': 'error',
  'no-constant-condition': 'error',
  'no-sparse-arrays': 'error',
  'no-obj-calls': 'error',
  'no-async-promise-executor': 'error',
  'no-compare-neg-zero': 'error',
  'use-isnan': 'error',
  'valid-typeof': 'error',
};

export default tseslint.config(
  {
    ignores: [
      'dist/**',
      'node_modules/**',
      'test-results/**',
      'playwright-report/**',
      'public/**',
      'vite.config.ts',
    ],
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: {
        ecmaVersion: 'latest',
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      '@typescript-eslint': tseslint.plugin,
      'react-hooks': reactHooks,
    },
    rules: {
      ...correctnessRules,
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
    },
  },
);
