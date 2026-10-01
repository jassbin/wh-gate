# VPN Gate SSTP 节点自动优选（edgetunnel 链式代理）

自动抓取 [VPN Gate](https://www.vpngate.net/) 的 SSTP 家宽/机房节点，调用检测 Worker 逐个验证可用性，按国家分组、标注住宅/机房，生成可直接粘贴进 edgetunnel 后台的链式代理清单。**每 30 分钟自动更新一次。**

> 核心价值：VPN Gate 的 SSTP 节点 30 分钟就换一批，手动测试筛选太痛苦。本仓库把它全自动了——你只需定期打开一个固定 URL 复制粘贴。

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
      │  GET /check?sstp=vpn:vpn@host:port
      │  返回 success + 出口 IP(住宅/机房判定)
      ▼
保留成功节点 → 按国家分组 → 住宅/机房标注 → 延迟排序
      │
      ▼
生成 hosts.txt (走 GitHub Pages 发布)
      │  你全选复制
      ▼
粘贴进 edgetunnel 后台「自定义优选IP」框
      │  edgetunnel 自动把链式代理指令编码进节点 path
      ▼
客户端订阅 edgetunnel 订阅 → 使用 SSTP 家宽节点
```

---

## 一、完整部署教程（从零开始，面向新用户）

### 前置条件

- 一个 Cloudflare 账号（免费即可）
- 一个 GitHub 账号
- 一个**已经部署好的 edgetunnel**（含自己的域名 + UUID，部署方法见 [edgetunnel 文档](https://github.com/cmliu/edgetunnel)）

> 下文所有「你的GitHub用户名 / 仓库名 / 域名 / UUID / Worker域名」都是占位符，替换成你自己的。

### 第 1 步：部署检测 Worker（CheckSocks5）

检测 Worker 负责验证「SSTP 节点能不能用」以及「出口是住宅还是机房」，必须自己部署一个：

1. 打开 https://github.com/lsh8848/cm-Workers-CheckSocks5 ，点 **Fork**（或直接下载其中的 _worker.js）
2. 进 Cloudflare 控制台 → Workers 和 Pages → 创建 → 创建 Worker
3. 把 _worker.js 的全部内容粘贴进编辑器，点「部署」
4. 记下这个 Worker 的域名，形如 https://xxx.你的用户名.workers.dev （也可绑自定义域名）
5. 验证：浏览器打开 https://你的Worker域名/check?sstp=vpn:vpn@任意节点:端口 ，能返回 JSON 即成功

> 该 Worker 不需要任何环境变量、没有鉴权，部署完即用；它原生支持 SSTP 检测，无需改代码。

### 第 2 步：Fork 本仓库

在 GitHub 上打开本仓库，点 **Fork**，复制到你账号下（变成 你的GitHub用户名/仓库名）。

### 第 3 步：修改配置（重点，Fork 后要改的全在这）

进你 fork 的仓库，改下面几处：

| 文件 | 位置 | 改成什么 | 为什么 |
| :--- | :--- | :--- | :--- |
| .github/workflows/check.yml | env 里的 CHECK_WORKER | 你的检测 Worker 域名，形如 https://xxx.workers.dev/check?sstp=vpn:vpn@ | 检测统一走你自己的 Worker |
| vpngate.py | 约 515 行 EDT_DOMAIN | 你的 edgetunnel 域名 | 链式代理入口的 SNI/host |
| 仓库 Secret `EDT_UUID`（环境变量） | — | 你的 edgetunnel UUID | **源码不保存真值**；仅在需要生成公开 sub.txt 时注入 |
| vpngate.py | 约 455 行 EDGE_HOSTS | 你测出来的优选域名 | 入口用谁，决定稳不稳 |
| vpngate.py | CHAIN_URL / HOSTS_URL | 把里面写死的固定地址换成 你的用户名/仓库名 | 清单注释头里的固定地址（sub.txt 默认不发布，见「六、安全与隐私」） |
| 仓库 Secret `CHECK_TOKEN` | — | 检测 Worker 的密钥路径，与 Worker 的 `AUTH_PATH` 变量同值 | 防止公开的检测地址被第三方白嫖 |
| .github/workflows/check.yml | 最后的 Show site URL | 把里面写死的站点地址换成你的 | 运行日志里显示的站点地址 |

> CHECK_WORKER 通过 workflow 环境变量传给脚本、会覆盖 vpngate.py 里的默认值，所以检测 Worker 域名只需在 workflow 里改一处。EDT_DOMAIN / EDGE_HOSTS 是 vpngate.py 里的默认值，直接改源码；**EDT_UUID 不进源码**，用仓库 Secret 注入（见「六、安全与隐私」）。

### 第 4 步：开启 GitHub Pages 与 Actions

1. 进你 fork 的仓库 → Settings → Pages，Source 设为 **GitHub Actions**（首次运行 workflow 也会尝试自动开启）
2. 进 Actions 页，若提示启用 Actions 就点启用
3. 手动触发一次：Actions → VPN Gate Node Check → Run workflow → Run workflow
4. 等它跑完（约 1 分钟），看到绿色 ✓ 即成功

### 第 5 步：确认产物

跑完后，你的站点地址是：

```text
https://你的GitHub用户名.github.io/仓库名/hosts.txt
```

浏览器打开，能看到一堆「优选域名:443#国家-住宅-XX …」的行，就说明全部打通了。

### 第 6 步：使用（见下面「使用教程」）

---

## 二、使用教程

### 前置条件
- 已部署 edgetunnel（自己的域名 + UUID）
- 一个客户端：v2rayN / Clash Verge / v2rayNG 等

### 步骤（约 1 分钟）

1. 打开 https://你的GitHub用户名.github.io/仓库名/hosts.txt
2. 浏览器里 Ctrl+A 全选 → Ctrl+C 复制
3. 进 edgetunnel 后台（你的域名/admin），找到「自定义优选IP」文本框
4. 光标移到现有内容末尾，Ctrl+V 粘贴
5. 点保存（右下角提示「自定义IP已保存」）
6. 客户端里更新/刷新订阅（订阅地址是 edgetunnel 后台给你的那个）
7. 测延迟，选一个节点用

### 自动同步（推荐，免手工复制粘贴）

上面第 1~5 步可以用仓库自带的 `push_edt_hosts.py` 一键完成：下载 hosts.txt → 登录后台 → 追加到「自定义优选IP」末尾 → 保存 → 回读校验（确认右下角那句「自定义IP已保存」的接口返回）。

```bash
# 1. 预览（只下载+合并，不连后台，无需密码）
python push_edt_hosts.py --dry-run

# 2. 同步一次（密码交互式输入、不回显；也可用环境变量 EDT_ADMIN_PASSWORD）
python push_edt_hosts.py

# 3. 常驻模式：每 30 分钟自动同步一次（与节点更新频率一致）
python push_edt_hosts.py --watch
```

说明：

- 后台地址默认读 `vpngate.py` 的 `EDT_DOMAIN`，可用 `--base https://你的域名` 或环境变量 `EDT_BASE` 覆盖
- 密码就是 edgetunnel Worker 上的 `ADMIN` 环境变量；脚本不会把它打印出来
- 重复运行**不会无限追加**：每次同步前先清掉上一轮的自动块和同名旧条目（名字固定、地址换新的语义），你自己填的其它内容原样保留在前面
- hosts.txt 主地址（本仓库 Pages）404 时自动回退到在线的备用地址，两边都挂才报错退出（退出码 1，不会假成功）

**接入 GitHub Actions 做到全自动**：仓库 Settings → Secrets and variables → Actions → New repository secret，添加 `EDT_ADMIN_PASSWORD`（可选再加 `EDT_BASE`）。之后每 30 分钟 workflow 跑完会自动推送后台；未配置该 Secret 时这一步自动跳过，不影响原有流水线。

### 节点名含义

节点名格式：国家-住宅-编号 / 国家-机房-编号，例如 日本-住宅-01、韩国-机房-02。住宅和机房各自独立编号，一眼区分。

### 每 30 分钟更新
节点每 30 分钟换一批，想换新节点时：重新打开 hosts.txt → 全选复制 → 覆盖粘贴。名字保持不变，只是背后的节点地址换了。懒得手动弄就用上面的 `python push_edt_hosts.py --watch`（或配置 Secret `EDT_ADMIN_PASSWORD` 交给 Actions），每 30 分钟自动替换后台里的旧条目。

---

## 三、如何更换优选域名

入口地址用的是「优选域名」，决定客户端连 Cloudflare 用哪个 IP、稳不稳。域名被墙或延迟高，可用节点就少。

### 在哪个文件改
- 文件：vpngate.py
- 位置：约 455 行 EDGE_HOSTS = [ ... ]

### 改法
1. 用测速工具（如 bestcf）测一批 Cloudflare 优选域名，挑「延迟低 + 实际能连通」的
2. 打开 vpngate.py，把 EDGE_HOSTS 里的域名列表换成你测出来的（逗号分隔，格式 域名:443）
3. 提交推送，等下一次自动运行（最多 30 分钟）或手动触发 Action

### 示例
```python
EDGE_HOSTS = [
    h.strip()
    for h in os.environ.get(
        "EDGE_HOSTS",
        "saas.072159.xyz:443,hzytjy.cn:443,ali.nonull.pp.ua:443,"
        "auto.dolby.dpdns.org:443,cdn.cnno.de:443,saas.sin.fan:443,"
        "cf.777791.xyz:443",
    ).split(",")
    if h.strip()
]
```

### 技巧
- 只留实测能通的域名：bestcf 里延迟低 ≠ 一定能通，挑「延迟低 + 实际连接成功」的
- 数量建议 5～10 个：太少单域名负担重，太多容易混进被墙的域名拖累可用率

---

## 四、配置速查表（vpngate.py）

| 常量 | 约位置 | 说明 |
| :--- | :--- | :--- |
| EDGE_HOSTS | 455 行 | 入口优选域名（换域名改这里） |
| EDT_DOMAIN | 515 行 | 你的 edgetunnel 域名 |
| EDT_UUID | 环境变量 / Secret | edgetunnel UUID；**源码不保存真值**，未设置时不生成 sub.txt |
| EDT_FINGERPRINT | 516 行 | TLS 指纹（默认 chrome） |
| WORKER_CHECK_URL | 54 行 | 检测 Worker（本地运行默认值，Action 里用 workflow 的 CHECK_WORKER 覆盖） |
| COUNTRY_ZH | 78 行 | 国家中文名映射 |

---

## 五、常见问题

### 只有几个节点能连
入口优选域名大部分被墙。用 bestcf 重新测速，把 EDGE_HOSTS 换成实测能通的域名（见「三」）。

### 全部 -1
检查：edgetunnel 是否部署好、域名是否解析到 Cloudflare、UUID 是否填对、传输协议是否对得上（默认按 ws/TLS 生成）。

### 30 分钟没更新
到 Actions 页看最近一次运行是否成功、cron 是否还在（.github/workflows/check.yml 里的 */30 * * * *）。

### 检测 Worker 报错
确认 Worker 部署成功、域名填对（workflow 里的 CHECK_WORKER），浏览器直接访问 https://你的Worker/check?sstp=... 看是否返回 JSON。

---

## 六、安全与隐私

- **UUID 不落源码**：`EDT_UUID` 只从环境变量/仓库 Secret 读取，源码里没有真值——公开仓库被翻到底也拿不到你的 UUID。
- **sub.txt 默认不发布**：它含完整 `vless://` 链接（等于 UUID），默认跳过生成并清理历史残留。`hosts.txt` / `chains.txt` 不含 UUID，可放心公开。确需公开订阅时：设 `PUBLISH_SUB=1` + Secret `EDT_UUID`（代价是 UUID 随之公开，可被他人拿去连你的 edgetunnel）。
- **检测 Worker 加密钥路径**：Worker 侧设置 `AUTH_PATH` 变量后，所有请求必须以 `/<密钥>` 开头否则 404；本仓库 workflow 用 Secret `CHECK_TOKEN` 组装地址（与 `AUTH_PATH` 同值），未配 Secret 时回退裸地址。详见检测 Worker 仓库的 `_worker.js` 注释。
- **后台 ADMIN 密码**：用长随机串（不要用用户名）。修改密码前先到 CF 面板确认已固定 `UUID` 环境变量，否则 UUID 会随密码变化、所有订阅链接失效。

---

*流水线：GitHub Actions（每 30 分钟 cron） → vpngate.py → 检测 Worker → GitHub Pages*

