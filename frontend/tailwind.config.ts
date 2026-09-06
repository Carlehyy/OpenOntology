import type { Config } from 'tailwindcss'
import tailwindcssAnimate from 'tailwindcss-animate'

// palantir-graph 编辑器色板：色值经 CSS 变量（RGB 通道三元组）下发，
// 深色默认值与浅色覆盖见 src/palantir-graph/palantir-graph.css 的
// .palantir-graph-root / .palantir-graph-root.pg-light 作用域。
// 这三组色板仅 palantir-graph 与图谱编辑器页面使用。
const pg = (name: string) => `rgb(var(--pg-${name}) / <alpha-value>)`

const config: Config = {
  content: ['./index.html', './src/**/*.{ts,tsx,js,jsx}'],
  // shadcn 深色模式约定：通过 <html class="dark"> 切换，变量定义见 src/styles/tokens.css
  darkMode: ['class'],
  theme: {
    extend: {
      colors: {
        // shadcn 语义色：映射到 tokens.css 的 CSS 变量，随 .dark 自动翻转
        background: 'var(--background)',
        foreground: 'var(--foreground)',
        card: {
          DEFAULT: 'var(--card)',
          foreground: 'var(--card-foreground)',
        },
        popover: {
          DEFAULT: 'var(--popover)',
          foreground: 'var(--popover-foreground)',
        },
        primary: {
          DEFAULT: 'var(--primary)',
          foreground: 'var(--primary-foreground)',
        },
        secondary: {
          DEFAULT: 'var(--secondary)',
          foreground: 'var(--secondary-foreground)',
        },
        muted: {
          DEFAULT: 'var(--muted)',
          foreground: 'var(--muted-foreground)',
        },
        accent: {
          DEFAULT: 'var(--accent)',
          foreground: 'var(--accent-foreground)',
        },
        // 品牌强调色阶（emerald）：DEFAULT 与导航同值，其余为明暗自适应语义档，
        // 取值事实源 tokens.css 的 --color-accent-*（口径见 DESIGN.md §2.2）
        brand: {
          DEFAULT: 'var(--color-nav-bg)',
          deep: 'var(--color-accent-deep)',
          soft: 'var(--color-accent-soft)',
          mist: 'var(--color-accent-mist)',
          line: 'var(--color-accent-line)',
          ink: 'var(--color-accent-ink)',
        },
        // 类型编码色（分类数据可视化）：与 tokens.css --color-viz-* 成对，
        // 取值同 DESIGN.md §5 图表序列，供图例/类型徽章/结构图节点使用
        viz: {
          violet: 'var(--color-viz-violet)',
          'violet-soft': 'var(--color-viz-violet-soft)',
          fuchsia: 'var(--color-viz-fuchsia)',
          'fuchsia-soft': 'var(--color-viz-fuchsia-soft)',
          rose: 'var(--color-viz-rose)',
          'rose-soft': 'var(--color-viz-rose-soft)',
          orange: 'var(--color-viz-orange)',
          'orange-soft': 'var(--color-viz-orange-soft)',
          indigo: 'var(--color-viz-indigo)',
          'indigo-soft': 'var(--color-viz-indigo-soft)',
          cyan: 'var(--color-viz-cyan)',
          'cyan-soft': 'var(--color-viz-cyan-soft)',
        },
        destructive: {
          DEFAULT: 'var(--destructive)',
          foreground: 'var(--destructive-foreground)',
        },
        // 语义成功色（DESIGN.md §2.3）：DEFAULT 为深字/实心，bg 为浅底，
        // 取值事实源 tokens.css 的 --color-success*（明暗成对自适应）
        success: {
          DEFAULT: 'var(--color-success)',
          hover: 'var(--color-success-hover)',
          bg: 'var(--color-success-bg)',
        },
        border: 'var(--border)',
        input: 'var(--input)',
        ring: 'var(--ring)',
        onto: {
          50: pg('onto-50'),
          100: pg('onto-100'),
          200: pg('onto-200'),
          300: pg('onto-300'),
          400: pg('onto-400'),
          500: pg('onto-500'),
          600: pg('onto-600'),
          700: pg('onto-700'),
          800: pg('onto-800'),
          900: pg('onto-900'),
          950: pg('onto-950'),
        },
        surface: {
          50: pg('surface-50'),
          100: pg('surface-100'),
          200: pg('surface-200'),
          300: pg('surface-300'),
          400: pg('surface-400'),
          500: pg('surface-500'),
          600: pg('surface-600'),
          700: pg('surface-700'),
          800: pg('surface-800'),
          900: pg('surface-900'),
          950: pg('surface-950'),
        },
        fn: {
          50: pg('fn-50'),
          100: pg('fn-100'),
          200: pg('fn-200'),
          300: pg('fn-300'),
          400: pg('fn-400'),
          500: pg('fn-500'),
          600: pg('fn-600'),
          700: pg('fn-700'),
        },
      },
      fontFamily: {
        display: ["'Cabinet Grotesk'", 'system-ui', 'sans-serif'],
        body: ["'Satoshi'", 'system-ui', 'sans-serif'],
        mono: ["'JetBrains Mono'", 'monospace'],
      },
      animation: {
        'fade-in': 'fadeIn 0.3s ease-out',
        'fade-out': 'fadeOut 0.2s ease-in forwards',
        'slide-up': 'slideUp 0.3s ease-out',
        'slide-in-right': 'slideInRight 0.3s ease-out',
        'slide-out-right': 'slideOutRight 0.2s ease-in forwards',
        'slide-in-left': 'slideInLeft 0.3s ease-out',
        'pulse-soft': 'pulseSoft 2s ease-in-out infinite',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        fadeOut: {
          '0%': { opacity: '1' },
          '100%': { opacity: '0' },
        },
        slideOutRight: {
          '0%': { opacity: '1', transform: 'translateX(0)' },
          '100%': { opacity: '0', transform: 'translateX(20px)' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(10px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        slideInRight: {
          '0%': { opacity: '0', transform: 'translateX(20px)' },
          '100%': { opacity: '1', transform: 'translateX(0)' },
        },
        slideInLeft: {
          '0%': { opacity: '0', transform: 'translateX(-20px)' },
          '100%': { opacity: '1', transform: 'translateX(0)' },
        },
        pulseSoft: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.7' },
        },
      },
    },
  },
  plugins: [tailwindcssAnimate],
}
export default config
