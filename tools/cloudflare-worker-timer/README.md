# wh-gate 外部定时器（Cloudflare Worker）

用 Cloudflare Worker 的 Cron Trigger 按点调用 GitHub 的
`workflow_dispatch` API，从而触发本仓库的 `check.yml`。

**为什么要它**：GitHub 自带的 `schedule` 是「尽力而为」的——高负载时会被延迟、
甚至整个丢弃（本仓库实测 `cron: "*/5 * * * *"` 在 `event=schedule` 上恒为 0 次）。
外部定时器更准时，且不受 GitHub 调度器影响。

> 用外部定时器时，`check.yml` 里的 `schedule:` 保持**注释掉**，避免两边重复触发。

## 一、准备 PAT（GitHub 令牌）

在 GitHub → Settings → Developer settings → Personal access tokens 里创建：

- **classic**：勾选 `repo` + `workflow`
- **fine-grained**：Repository access 选本仓库，权限 **Actions = Read and write**

> 只触发 `workflow_dispatch` 需要以上权限；令牌只存在 Cloudflare 的密钥里，不会进仓库。

## 二、部署（二选一）

### 方式 A：wrangler 命令行（推荐）

```bash
# 在本目录 (tools/cloudflare-worker-timer)
npm i -g wrangler        # 或 npx wrangler
wrangler login
wrangler deploy          # 依据 wrangler.toml 创建 Worker + Cron Trigger
wrangler secret put GH_PAT   # 按提示粘贴第一步的 PAT
```

改频率/仓库：编辑 `wrangler.toml` 的 `[triggers] crons` 与 `[vars]`，再 `wrangler deploy`。

### 方式 B：Cloudflare 控制台（不用命令行）

1. Cloudflare 控制台 → **Workers 和 Pages → 创建 → 创建 Worker**，命名如 `wh-gate-timer`。
2. 把 `worker.js` 的全部内容粘贴进编辑器 → **部署**。
3. Worker → **设置 → 变量**：
   - 普通变量：`REPO_OWNER`、`REPO`、`WORKFLOW_FILE`（可选，默认 `check.yml`）、`REF`（可选，默认 `main`）、`MANUAL_TOKEN`（可选，手动触发口令）
   - **密钥**：`GH_PAT` = 第一步的 PAT
4. Worker → **设置 → 触发器 → Cron 触发器** → 添加 `*/30 * * * *`。
   > ⚠️ 控制台的 cron 格式是 5 段（分 时 日 月 周），不是 6 段。

## 三、验证

- **手动触发**：浏览器打开 `https://<worker域名>/run`（设了 `MANUAL_TOKEN` 就带 `?key=<口令>`）。
  返回 `{"ok":true,"status":204,...}` 即成功；随后到 Actions 页应能看到一次
  `workflow_dispatch` 运行。
- **定时触发**：等到 cron 到点，Actions 列表会出现 `workflow_dispatch` 运行，频次即 cron 间隔。

## 四、注意

- 触发成功 = GitHub 返回 **204**；若返回 401/403 是 PAT 权限/前缀不对，
  404 是仓库名或 workflow 文件名写错。
- 本方案产生的是 `workflow_dispatch` 事件，**不写** `.github/last-run.txt`
  （该文件只在 GitHub 自带 `schedule` 事件下回写，用于防「60 天无活动」停用；改用外部
  定时器后已无此需求）。
- 产物头部注释里的「每 X 分钟」来自 `check.yml` 的 `REFRESH_MINUTES`，
  请与本文件的 `crons` 频率保持一致。