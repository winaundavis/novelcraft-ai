/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#f0f4ff',
          100: '#dbe4ff',
          200: '#bac8ff',
          300: '#91a7ff',
          400: '#748ffc',
          500: '#5c7cfa',
          600: '#4c6ef5',
          700: '#4263eb',
          800: '#3b5bdb',
          900: '#364fc7',
        },
        surface: {
          50: '#f8f9fa',
          100: '#f1f3f5',
          200: '#e9ecef',
          300: '#dee2e6',
          400: '#ced4da',
          500: '#adb5bd',
          600: '#6c757d',
          700: '#495057',
          750: '#3a4147',
          800: '#343a40',
          850: '#2b3035',
          900: '#212529',
        },
        // 语义色：状态表达唯一指定色，禁止再使用 green/emerald/yellow/amber/red/rose/blue 硬编码
        success: {
          DEFAULT: '#16a34a', // green-600
          soft: '#f0fdf4', // green-50
          'soft-dark': '#14532d4d', // green-900/30
          dark: '#4ade80', // green-400
        },
        warning: {
          DEFAULT: '#f59f00', // amber-500
          soft: '#fffbeb', // amber-50
          'soft-dark': '#78350f33', // amber-900/20
          dark: '#fbbf24', // amber-400
        },
        error: {
          DEFAULT: '#dc2626', // red-600
          soft: '#fef2f2', // red-50
          'soft-dark': '#7f1d1d33', // red-900/20
          dark: '#f87171', // red-400
        },
        info: {
          DEFAULT: '#3b82f6', // blue-500
          soft: '#eff6ff', // blue-50
          'soft-dark': '#1e3a8a33', // blue-900/20
          dark: '#60a5fa', // blue-400
        },
      },
      fontFamily: {
        sans: ['"Microsoft YaHei"', '"PingFang SC"', '"Hiragino Sans GB"', '"Source Han Sans SC"', 'system-ui', '-apple-system', 'sans-serif'],
        serif: ['"STSong"', '"Songti SC"', '"Noto Serif CJK SC"', 'Georgia', 'serif'],
      },
    },
  },
  plugins: [],
};
