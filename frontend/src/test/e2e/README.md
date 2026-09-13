# Playwright E2E

当前 90 个 spec 按运行依赖显式分组：

- `mocked`：67 个，所有业务请求/实时通道本地替代，后端地址故意不可达；
- `stack`：22 个，需要隔离的 OpenOntology 后端；
- `external`：1 个，需要显式开关和真实付费/供应商服务。

```bash
npm run test:e2e:classification
npm run test:e2e:mocked
npm run test:e2e:stack
npm run test:e2e:external
```

不要用文件名 grep 或默认 skip 代替分类。新增 spec 后先更新唯一一个 suite
allowlist，再运行 classification。证据统一写到仓库根目录
`.artifacts/playwright/`。

真实栈用例通过 `PLAYWRIGHT_ADMIN_USER` 和
`PLAYWRIGHT_ADMIN_PASSWORD` 接收隔离环境管理员凭据；未设置时仅为本地开发
兼容回退到 `admin / admin123`。CI、staging 和严格生产烟测不得依赖这个开发
默认值。全部 21 个 `stack` spec 和 1 个 `external` spec 统一从
`support/stack-credentials.ts` 读取这两个值，不得在单个真实栈用例中再次定义
默认账号。`mocked` 用例可以在完全拦截登录请求时使用明确的假凭据，但不能把
它们复制到真实栈分组。
