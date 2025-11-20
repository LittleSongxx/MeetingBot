// ESLint 扁平配置（ESLint 9+ 的唯一形态）。
// 分工：ESLint 管**正确性**（未使用变量、可疑写法、Vue 的易错用法），
// Prettier 管**格式**；两者冲突的规则由 eslint-config-prettier 关掉，
// 因此 CI 里不会出现"格式化工具和 lint 工具互相要求不同写法"的拉锯。
import js from '@eslint/js'
import configPrettier from 'eslint-config-prettier'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    // 构建产物与自动生成的类型声明不参与检查
    ignores: ['dist/**', 'node_modules/**', 'auto-imports.d.ts', 'components.d.ts'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.{ts,vue}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.node },
      parserOptions: {
        // .vue 里的 <script setup lang="ts"> 交给 TS 解析器
        parser: tseslint.parser,
      },
    },
    rules: {
      // 页面文件名（Home.vue 之类）是既有约定，不改
      'vue/multi-word-component-names': 'off',
      // 下面几条是**版式偏好**（属性顺序、每文件一个组件、prop 默认值），
      // 与仓库既有模板写法冲突而正确性无关：开着它们只能得到"几百条与代码风格
      // 打架的告警"，而告警多了等于没有告警。版式交给 Prettier，
      // 这里只保留能抓到真实缺陷的规则。
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/first-attribute-linebreak': 'off',
      'vue/attributes-order': 'off',
      'vue/one-component-per-file': 'off',
      'vue/require-default-prop': 'off',
      // 空 catch 是刻意写法（例如"解析失败就按兜底文案"），已写注释说明
      'no-empty': ['error', { allowEmptyCatch: true }],
      // 未使用变量：允许下划线开头的占位参数
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // 本项目不用 any 绕过类型（tsconfig 里 noImplicitAny 已放宽历史代码，但显式 any 禁止）
      '@typescript-eslint/no-explicit-any': 'error',
    },
  },
  {
    files: ['**/*.spec.ts'],
    rules: {
      // 测试里 mock 的对象允许宽松
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
  configPrettier,
)
