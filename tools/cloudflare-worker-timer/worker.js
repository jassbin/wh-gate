/**
 * wh-gate 外部定时器 (Cloudflare Worker)
 *
 * 作用: 按点调用 GitHub Actions 的 workflow_dispatch API 触发检测流水线。
 *   为什么不用 GitHub 自带的 schedule: 它「尽力而为」, 高负载时延迟甚至整个丢弃
 *   (本仓库实测高频 cron〔每 5 分钟〕在 event=schedule 上恒为 0 次)。外部定时器更准时,
 *   且不依赖 GitHub 的调度器。
 *
 * 需要配置 (变量/密钥):
 *   普通变量 [vars] (wrangler.toml 或面板):
 *     REPO_OWNER     你的 GitHub 用户名 (如 whua898)
 *     REPO           仓库名 (如 wh-gate)
 *     WORKFLOW_FILE  workflow 文件名 (默认 check.yml)
 *     REF            触发的分支 (默认 main)
 *     MANUAL_TOKEN   (可选) 手动 GET /run 时的口令, 留空则不校验
 *   密钥 (SENSITIVE, 不要写进 wrangler.toml):
 *     GH_PAT         GitHub Personal Access Token
 *                    - classic: 勾选 repo + workflow
 *                    - fine-grained: 仓库权限 Actions = Read and write
 *                    命令行写入:  wrangler secret put GH_PAT
 *
 * 触发方式:
 *   - cron 到点自动调 dispatch() (见 wrangler.toml 的 [triggers] crons)
 *   - 手动测试: 浏览器/curl 访问 https://<worker域名>/run (?key=<MANUAL_TOKEN>)
 */

export default {
  // cron 到点触发 (见 wrangler.toml [triggers] crons)
  async scheduled(event, env, ctx) {
    // waitUntil: 让 fetch 在后台完成, 不被 worker 提前回收
    ctx.waitUntil(dispatch(env));
  },

  // HTTP 入口: 提供手动触发/健康检查
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/run" || url.pathname === "/dispatch") {
      // 可选口令保护, 避免任何人访问该地址都能触发你的 workflow
      if (env.MANUAL_TOKEN && url.searchParams.get("key") !== env.MANUAL_TOKEN) {
        return new Response("forbidden", { status: 403 });
      }
      const out = await dispatch(env);
      return new Response(JSON.stringify(out, null, 2), {
        headers: { "content-type": "application/json; charset=utf-8" },
      });
    }
    return new Response(
      "wh-gate 外部定时器已就绪。cron 会自动触发；手动触发: GET /run（如设了 MANUAL_TOKEN 需带 ?key=...）\n",
      { headers: { "content-type": "text/plain; charset=utf-8" } }
    );
  },
};

// 调 GitHub API 触发 workflow_dispatch。返回结果对象便于日志/手动查看。
async function dispatch(env) {
  const owner = env.REPO_OWNER;
  const repo = env.REPO;
  const workflow = env.WORKFLOW_FILE || "check.yml";
  const ref = env.REF || "main";
  const token = env.GH_PAT;

  if (!owner || !repo || !token) {
    return { ok: false, error: "缺少变量: 需要 REPO_OWNER / REPO / GH_PAT" };
  }

  const url =
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;

  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "wh-gate-timer",
      "content-type": "application/json",
    },
    body: JSON.stringify({ ref }),
  });

  // 成功触发时 GitHub 返回 204 No Content (没有响应体)
  const body = res.status === 204 ? "" : await res.text();
  return {
    ok: res.ok,
    status: res.status,
    repo: `${owner}/${repo}`,
    workflow,
    ref,
    body,
    at: new Date().toISOString(),
  };
}