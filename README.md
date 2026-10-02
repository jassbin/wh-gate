# VPN Gate SSTP 节点自动优选（edgetunnel 链式代理）

自动抓取 [VPN Gate](https://www.vpngate.net/) 的 SSTP 节点，调用检测 Worker 逐个验证可用性，按国家分组（仅收录住宅 IP，非住宅场景用 ed 其他节点），生成可直接粘贴进 edgetunnel 后台的链式代理清单。**每 30 分钟自动更新一次。**

> 核心价值：VPN Gate 的 SSTP 节点 30 分钟就换一批，手动测试筛选太痛苦。本仓库把它全自动了——GitHub Actions 每 30 分钟自动检测并推送进 edgetunnel 后台，你只需在客户端里更新订阅。

---

## 引用的开源项目（致谢）

本项目建立在以下开源项目之上：

| 项目 | 用途 | 链接 |
| :--- | :--- | :--- |
| **cmliu/edgetunnel** | VLESS 代理 + 链式代理（节点备注里的链式代理指令），节点最终通过它使用 | https://github.com/cmliu/edgetunnel |
| **lsh8848/cm-Workers-CheckSocks5** | 检测 Worker：验证 SSTP 节点可用性并读取出口 IP（住宅/机房判定） | https://github.com/lsh8848/cm-Workers-CheckSocks5 |
| **fdciabdul/Vpngate-Scraper-API** | VPN Gate 节点数据的 GitHub 镜像（官方源失效时回退） | https://github.com/fdciabdul/Vpngate-Scraper-API |
| **VPN Gate** | SSTP 节点数据源 | https://www.vpngate.net/ |

---

## 架构（数据流向）

```text
VPN Gate 官方源
      │  (每 30 分钟，GitHub Actions 定时抓取)
      ▼
筛选 SSTP 节点 → 去重
      │
      ▼
检测 Worker (CheckSocks5，部署在 Cloudflare)
      │  GET /<密钥>/check?sstp=vpn:vpn@host:port（密钥路径见第 1 步第 6 点）
      │  返回 success + 出口 IP(住宅/机房判定)
      ▼
保留成功节点 → 仅收录住宅（机房/未知丢弃） → 按国家分组 → 延迟排序
      │
      ▼
生成 hosts.txt + chains.txt + data.json + index.html (走 GitHub Pages 发布)
      │  workflow 自动同步进 edgetunnel 后台「自定义优选IP」
      │  每次运行回写 .github/last-run.txt（防 60 天停用定时任务）
      │  edgetunnel 自动把链式代理指令编码进节点 path

```

---

## 一、完整部署教程（从零开始，面向新用户）

### 前置条件

- 一个 Cloudflare 账号（免费即可）
- 一个 GitHub 账号
- 一个**已经部署好的 edgetunnel**（含自己的域名 + UUID，部署方法见 [edgetunnel 文档](https://github.com/cmliu/edgetunnel)）

> 下文所有「你的GitHub用户名 / 仓库名 / 域名 / UUID / Worker域名」都是占位符，替换成你自己的。

### 第 1 步：部署检测 Worker（CheckSocks5）

检测 Worker 负责验证「SSTP 节点能不能用」以及「出口是住宅还是机房」。建议按下面第 6 步加密钥路径保护（公开仓库里会直接出现检测地址，不加保护会被第三方拿去刷量）：

1. 打开 https://github.com/lsh8848/cm-Workers-CheckSocks5 ，点 **Fork**（或直接下载其中的 _worker.js）
2. 进 Cloudflare 控制台 → Workers 和 Pages → 创建 → 创建 Worker
3. 把 _worker.js 的全部内容粘贴进编辑器，点「部署」
4. 记下这个 Worker 的域名，形如 https://xxx.你的用户名.workers.dev （也可绑自定义域名）
5. 验证：浏览器打开 https://你的Worker域名/check?sstp=vpn:vpn@任意节点:端口 ，能返回 JSON 即成功
6. （可选但推荐）给 Worker 加密钥路径：在 Worker 的设置 → 变量里添加 `AUTH_PATH` = 一串随机字符（如 `a3f9k2xxxx`），再在你 fork 的仓库 `_worker.js` 入口处加一段校验——未携带 `/<密钥>` 前缀的请求一律返回 404。对应地，本仓库 workflow 里配同值的 Secret `CHECK_TOKEN` 即可（不配则只能用无保护的裸地址）

> 该 Worker 不需要其它环境变量（`AUTH_PATH` 不设时行为不变）；它原生支持 SSTP 检测，无需改检测逻辑。

### 第 2 步：Fork 本仓库

在 GitHub 上打开本仓库，点 **Fork**，复制到你账号下（变成 你的GitHub用户名/仓库名）。

> 注意：**fork 出来的仓库，GitHub 默认停用定时任务**（手动能跑、定时一次都不跑，且没有任何提示）。fork 后务必到 Actions 页点一次 **Enable workflow**；想彻底避开 fork 这套限制，按「七、脱离 fork」新建独立仓库。

### 第 3 步：修改配置（重点，Fork 后要改的全在这）

进你 fork 的仓库，改下面几处：

| 文件 | 位置 | 改成什么 | 为什么 |
| :--- | :--- | :--- | :--- |
| .github/workflows/check.yml | `CHECK_WORKER`（含密钥路径，形如 `https://你的Worker域名/<密钥>/check?sstp=vpn:vpn@`） | 你的检测 Worker 域名 | 检测统一走你自己的 Worker，需与 Worker 的 `AUTH_PATH` 变量同值 |
| vpngate.py | `EDT_DOMAIN`（约 550 行） | 你的 edgetunnel 域名 | 链式代理入口的 SNI/host；也可用仓库 Secret `EDT_BASE` 覆盖后台地址 |
| 仓库 Secret `EDT_UUID`（环境变量） | — | 你的 edgetunnel UUID | **源码不保存真值**；仅在需要生成公开 sub.txt 时注入 |
| vpngate.py | `EDGE_HOSTS`（约 480 行）或环境变量 `EDGE_HOSTS` | 你测出来的优选域名（逗号分隔，不用改源码也行） | 入口用谁，决定稳不稳 |
| vpngate.py | `CHAIN_URL` / `HOSTS_URL` / `SUB_URL`（约 430/490/552 行） | 把里面写死的固定地址换成 你的用户名/仓库名 | 清单注释头里的固定地址；也可用同名环境变量覆盖（sub.txt 默认不发布，见「六、安全与隐私」） |
| 仓库 Secret `CHECK_TOKEN` | — | 检测 Worker 的密钥路径，与 Worker 的 `AUTH_PATH` 变量同值 | 防止公开的检测地址被第三方白嫖 |
| .github/workflows/check.yml | 最后的 Show site URL | 把里面写死的站点地址换成你的 | 运行日志里显示的站点地址 |
| 仓库 Secret `EDT_ADMIN_PASSWORD` | — | edgetunnel 后台的 ADMIN 密码 | 必填，否则「同步进后台」那一步自动跳过 |
| 仓库 Secret `EDT_BASE`（可选） | — | 你的 edgetunnel 地址 | 覆盖默认后台地址 |

> `CHECK_WORKER` 通过 workflow 环境变量传给脚本、会覆盖 vpngate.py 里的默认值，所以检测 Worker 域名只需在 workflow 里改一处（记得带上 `/<密钥>` 前缀并配好 `CHECK_TOKEN`）。`EDT_DOMAIN` / `EDGE_HOSTS` / `CHAIN_URL` / `HOSTS_URL` 是 vpngate.py 里的默认值，可直接改源码，也可用同名环境变量覆盖；**`EDT_UUID` 不进源码**，用仓库 Secret 注入（见「六、安全与隐私」）。

### 第 4 步：开启 GitHub Pages 与 Actions

1. 进你 fork 的仓库 → Settings → Pages，Source 设为 **GitHub Actions**（首次运行 workflow 也会尝试自动开启）
2. 进 Actions 页，若提示启用 Actions 就点启用
3. 手动触发一次：Actions → VPN Gate Node Check → Run workflow → Run workflow
4. 等它跑完（约 2~10 分钟，取决于节点数量），看到绿色 ✓ 即成功

### 第 5 步：确认产物

跑完后，你的站点地址是：

```text
https://你的GitHub用户名.github.io/仓库名/hosts.txt
```

浏览器打开，能看到一堆「优选域名:443#国家-住宅-XX$sstp://…」的行，就说明全部打通了。同目录还有 `chains.txt`（粘贴进 edgetunnel 节点备注用）、`index.html`（节点总览页）、`data.json`（原始数据）。

### 第 6 步：客户端更新订阅（见下面「使用教程」）

---

## 二、使用教程

### 前置条件
- 已部署 edgetunnel（自己的域名 + UUID）
- 一个客户端：v2rayN / Clash Verge / v2rayNG 等

### 自动同步（默认，GitHub 全自动，本机无需常驻）

配好 Secrets 后什么都不用做：本仓库 `VPN Gate Node Check` workflow 每 30 分钟运行一次 ——
拉节点 → 检测 → 生成 `hosts.txt` → 发布到 Pages → 同步进 edgetunnel 后台「自定义优选IP」→ 回写 `.github/last-run.txt`（防 GitHub 因"公开仓库 60 天无活动"停用定时任务）。
你唯一要做的：在客户端里更新/刷新订阅（订阅地址是 edgetunnel 后台给你的那个），然后测延迟选节点用。

> ⚠️ 本仓库是 fork 出来的，GitHub **默认停用 fork 仓库的定时任务**：第一次用请先到 Actions → `VPN Gate Node Check` → **Enable workflow**，否则只会手动跑、定时永不触发（详见「五、常见问题 → 定时任务不触发」）。fork 里的定时任务还可能被 GitHub 再次自动停用，想一劳永逸见「七、脱离 fork」。

### 手工同步（备用：自动同步没配好时才用，约 1 分钟）

1. 打开 https://你的GitHub用户名.github.io/仓库名/hosts.txt
2. 浏览器里 Ctrl+A 全选 → Ctrl+C 复制
3. 进 edgetunnel 后台（你的域名/admin），找到「自定义优选IP」文本框
4. 光标移到现有内容末尾，Ctrl+V 粘贴
5. 点保存（右下角提示「自定义IP已保存」）

配置（都已配好，改动时才需要）：
- `Secrets`：`EDT_ADMIN_PASSWORD`（edgetunnel 后台密码，必填，否则同步后台那步自动跳过）、`CHECK_TOKEN`（检测 Worker 的路径密钥，与 Worker 的 `AUTH_PATH` 同值；不配则用无保护的裸地址）
- 仓库/Pages 保持公开，Actions 分钟免费无上限；无需本机计划任务或 `--watch`。
- `push_edt_hosts.py` 仅用于本地手动预览/调试：`python push_edt_hosts.py --dry-run`（只下载+合并，不连后台）；本地同步一次 `python push_edt_hosts.py`（密码交互式输入）；`--watch` 为常驻循环（本机方案，已被 GitHub Actions 取代，一般不用）。

说明：

- 后台地址默认读 `vpngate.py` 的 `EDT_DOMAIN`，可用 `--base https://你的域名` 或环境变量 `EDT_BASE` 覆盖
- 密码就是 edgetunnel Worker 上的 `ADMIN` 环境变量；脚本不会把它打印出来
- 重复运行**不会无限追加**：每次同步前先清掉上一轮的自动块、同名旧条目和早期「机房」遗留条目（名字固定、地址换新的语义），你自己填的其它内容原样保留在前面
- hosts.txt 主地址（本仓库 Pages）404 时自动回退到在线的备用地址，两边都挂才报错退出（退出码 1，不会假成功）

**接入 GitHub Actions 做到全自动**：仓库 Settings → Secrets and variables → Actions → New repository secret，添加 `EDT_ADMIN_PASSWORD`（可选再加 `CHECK_TOKEN`、`EDT_BASE`）。之后每 30 分钟 workflow 跑完会自动推送后台；未配置 `EDT_ADMIN_PASSWORD` 时这一步自动跳过，不影响原有流水线。

### 节点名含义

节点名格式：国家-住宅-编号，例如 日本-住宅-01。仅收录住宅 IP；非住宅需求用 ed 其他节点补。

### 每 30 分钟更新
节点每 30 分钟换一批，想换新节点时：在客户端里更新/刷新一下订阅就行（workflow 已自动把新 hosts.txt 推进后台，旧条目自动替换）。

---

## 三、如何更换优选域名

入口地址用的是「优选域名」，决定客户端连 Cloudflare 用哪个 IP、稳不稳。域名被墙或延迟高，可用节点就少。

### 在哪个文件改
- 文件：vpngate.py
- 位置：`EDGE_HOSTS = [ ... ]`（约 480 行）
- 不改源码也行：设环境变量 `EDGE_HOSTS`（逗号分隔，格式 `域名:443`），会整体覆盖默认值

### 改法
1. 用测速工具（如 bestcf）测一批 Cloudflare 优选域名，挑「延迟低 + 实际能连通」的
2. 打开 vpngate.py，把 EDGE_HOSTS 里的域名列表换成你测出来的（逗号分隔，格式 域名:443），或设环境变量 `EDGE_HOSTS`
3. 提交推送，等下一次自动运行（最多 30 分钟）或手动触发 Action

### 示例
```python
EDGE_HOSTS = [
    h.strip()
    for h in os.environ.get(
        "EDGE_HOSTS",
        "www.5199dy.com:443,hzytjy.cn:443,ali.nonull.pp.ua:443,auto.dolby.dpdns.org:443,"
        "cdn.cnno.de:443,saas.sin.fan:443,cf.1o.ee:443",
    ).split(",")
    if h.strip()
]
```
（与 vpngate.py 默认值一致；直接整段换你自己的即可）

### 技巧
- 只留实测能通的域名：bestcf 里延迟低 ≠ 一定能通，挑「延迟低 + 实际连接成功」的
- 数量建议 5～10 个：太少单域名负担重，太多容易混进被墙的域名拖累可用率

---

## 四、配置速查表（vpngate.py）

| 常量 | 约位置 | 说明 |
| :--- | :--- | :--- |
| EDGE_HOSTS | 约 480 行 / 环境变量 `EDGE_HOSTS` | 入口优选域名（改源码或设环境变量都行） |
| EDT_DOMAIN | 约 550 行 / 环境变量 `EDT_DOMAIN`（后台地址另有 Secret `EDT_BASE`） | 你的 edgetunnel 域名 |
| EDT_UUID | 环境变量 / Secret | edgetunnel UUID；**源码不保存真值**，未设置时不生成 sub.txt |
| EDT_FINGERPRINT | 约 551 行 / 环境变量 `EDT_FINGERPRINT` | TLS 指纹（默认 chrome） |
| WORKER_CHECK_URL | 约 75 行 / 环境变量 `CHECK_WORKER`（含 `/<密钥>` 前缀） | 检测 Worker（本地运行默认值，Action 里用 workflow 的 CHECK_WORKER 覆盖） |
| COUNTRY_ZH | 约 100 行 | 国家中文名映射 |

---

## 五、常见问题

### 只有几个节点能连
入口优选域名大部分被墙。用 bestcf 重新测速，把 EDGE_HOSTS 换成实测能通的域名（见「三」）。

### 全部 -1
检查：edgetunnel 是否部署好、域名是否解析到 Cloudflare、UUID 是否填对、传输协议是否对得上（默认按 ws/TLS 生成）。

### 30 分钟没更新
到 Actions 页看最近一次运行是否成功、`Event` 里有没有 `schedule`（cron 见 .github/workflows/check.yml，当前 `*/30 * * * *`）。
如果只有 `workflow_dispatch`、从来没有 `schedule` 运行 → 见下条「定时任务不触发」。

### 检测 Worker 报错
确认 Worker 部署成功、域名填对（workflow 里的 CHECK_WORKER）、`CHECK_TOKEN` 与 Worker 的 `AUTH_PATH` 一致。浏览器直接访问带密钥的完整地址 `https://你的Worker/<密钥>/check?sstp=vpn:vpn@任意节点:端口` 看是否返回 JSON；裸地址返回 404 说明密钥路径已生效、必须带密钥访问。

### 后台没同步上
先看 Actions 日志里「Push hosts.txt to edgetunnel admin」那一步：若显示「跳过：未配置 EDT_ADMIN_PASSWORD」就是 Secret 没配；若登录失败，检查密码是否为 Worker 上的 `ADMIN` 变量值、后台地址（`EDT_BASE` / `EDT_DOMAIN`）是否写对。

### 定时任务不触发
到 Actions → `VPN Gate Node Check` 页面点 **Enable workflow**（两种原因的表现都是「手动能跑、定时不跑」）：

1. **fork 仓库（本仓库就是从 `hezhanleiok/gate` fork 来的）**：GitHub 对「公开仓库被 fork」出来的仓库**默认停用 schedule**——`workflow_dispatch` 能跑、`schedule` 一次都不跑，而且没有任何报错、邮件或通知，`.github/last-run.txt` 也永远不会有 commit。页面顶部一般有黄色横幅（`Workflows aren't being run on this forked repository`），点 **Enable workflow** 即可；若没看到横幅，先 **Disable workflow** 再 **Enable workflow** 强制重新注册定时。
2. **公开仓库 60 天无 commit**：GitHub 会自动停用 schedule。正常情况下每次运行都会回写 `.github/last-run.txt` 产生 commit，不会触发；若停了，同样点 **Enable workflow**。

> fork 里的定时任务可能被 GitHub 再次自动停用；而且 GitHub 的 cron 是「尽力而为」的（官方文档：高负载时可能延迟、甚至丢弃运行），所以别指望它严格每 30 分钟准点。
>
> **想彻底摆脱 fork 限制，推荐下一节「七、脱离 fork」**：新建一个非 fork 的仓库，一劳永逸。
> 也可以不改仓库、只改触发方式：删掉 `schedule:` 只留 `workflow_dispatch:`，再用外部定时器（例如 Cloudflare Worker 的 cron + 一个 PAT）调
> `POST https://api.github.com/repos/你的用户名/仓库名/actions/workflows/check.yml/dispatches`，body 为 `{"ref":"main"}`。
> API 触发同样算仓库活动，不会再被「60 天无活动」规则停用，时间也更准时。

---

## 六、安全与隐私

- **UUID 不落源码**：`EDT_UUID` 只从环境变量/仓库 Secret 读取，源码里没有真值——公开仓库被翻到底也拿不到你的 UUID。
- **sub.txt 默认不发布**：它含完整 `vless://` 链接（等于 UUID），默认跳过生成并清理历史残留。`hosts.txt` / `chains.txt` 不含 UUID，可放心公开。确需公开订阅时：设 `PUBLISH_SUB=1` + Secret `EDT_UUID`（代价是 UUID 随之公开，可被他人拿去连你的 edgetunnel）。
- **检测 Worker 加密钥路径**：Worker 侧设置 `AUTH_PATH` 变量后，所有请求必须以 `/<密钥>` 开头否则 404；本仓库 workflow 用 Secret `CHECK_TOKEN` 组装地址（与 `AUTH_PATH` 同值），未配 Secret 时回退裸地址。详见检测 Worker 仓库的 `_worker.js` 注释。
- **后台 ADMIN 密码**：用长随机串（不要用用户名）。修改密码前先到 CF 面板确认已固定 `UUID` 环境变量，否则 UUID 会随密码变化、所有订阅链接失效。

---

## 七、脱离 fork：迁移到独立仓库（彻底解决「定时任务不触发」）

GitHub **没有「unfork」按钮**：fork 关系一旦建立就无法在同一个仓库里解除，只能「新建一个非 fork 仓库 + 把内容推过去」。

**不用改任何代码** —— 本项目所有地址都没写死仓库名：

- workflow 里展示的站点地址用 Pages 官方输出 `steps.deployment.outputs.page_url`；
- 清单头部的「固定地址」（`chains.txt` / `hosts.txt` / `sub.txt`）由 `vpngate.py` 从 Actions 自动注入的 `GITHUB_REPOSITORY` 推导（只有非 CI 环境才回退到写死值）；
- 同步 edgetunnel 后台时优先读本次刚生成的本地 `public/hosts.txt`（`HOSTS_FILE`），与 Pages 地址无关。

### 步骤

1. **新建空白仓库**：打开 https://github.com/new → 填名字（如 `wh-gate`）→ 选 **Public** → **不要**勾 Add a README / .gitignore / license → Create repository。
   > 必须是「空白新建」的仓库，它和上游 `hezhanleiok/gate` 没有任何 fork 关系，schedule 才不会被停用。
   > 想保留原来的 `https://你的用户名.github.io/wh-gate/` 地址：先**删除**旧 fork，再新建**同名**仓库（GitHub 允许复用已删除仓库的名字），这样订阅网址一个字都不用改。

2. **改 remote 并推送**（在本地仓库里执行）：

   ```bash
   git remote rename origin fork              # 旧 fork 留档 (没有 origin 这句就跳过)
   git remote add origin https://github.com/你的用户名/新仓库名.git
   git push -u origin main                    # 连历史一起推上去
   ```

   推送凭据：HTTPS 用 PAT（需 repo 权限）；也可换成 SSH 地址 `git@github.com:你的用户名/新仓库名.git`。
   用 GitHub CLI 可以一条命令搞定：`gh repo create 新仓库名 --public --source . --push`。

3. **补 Secrets**（**不会随 git 同步，必须重配**）：新仓库 Settings → Secrets and variables → Actions → New repository secret

   | Secret | 值 | 是否必需 |
   | :--- | :--- | :--- |
   | `EDT_ADMIN_PASSWORD` | edgetunnel 后台 ADMIN 密码 | 自动同步后台必需 |
   | `CHECK_TOKEN` | 与检测 Worker 的 `AUTH_PATH` 同值 | 推荐（不配则用裸地址） |
   | `EDT_BASE` | edgetunnel 后台地址（缺省取 `EDT_DOMAIN`） | 可选 |
   | `EDT_UUID` | edgetunnel 的 UUID（仅发布 sub.txt 时需要） | 可选 |

4. **等第一次自动运行**：新仓库不是 fork，schedule 不会被停用。到 Actions 页看到 `VPN Gate Node Check`，且 `Event` 列出现 `schedule`（一般 30 分钟内）即迁移成功。公开仓库 Actions 默认开启，无需再点 Enable workflow。

5. **停掉旧 fork（重要）**：旧仓库 Settings → 最底部 Danger Zone → **Archive this repository**（或删除）。否则两边会同时往 edgetunnel 后台写同一个托管块（哨兵注释 `# >>> wh-gate auto-sync >>>`），互相覆盖。

### 迁移后要换的只有订阅网址
Pages 地址变成 `https://你的用户名.github.io/新仓库名/`，之前手动记下的 `hosts.txt` 网址跟着换即可；edgetunnel 后台里的内容不用管——workflow 会用同样的哨兵注释整体替换旧块，不会重复堆叠。

---

*流水线：GitHub Actions（每 30 分钟 cron） → vpngate.py → 检测 Worker → GitHub Pages*

