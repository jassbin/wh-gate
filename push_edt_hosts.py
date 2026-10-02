#!/usr/bin/env python3
"""
自动同步 hosts.txt → edgetunnel 后台「自定义优选IP」
====================================================
替代 README「使用教程」里的手工步骤 (打开URL → 全选复制 → 粘贴 → 保存):

  1. 下载 hosts.txt (主地址 404/失败时自动回退备用地址)
  2. POST /login             用 ADMIN 密码换取 auth cookie
                             (cookie 值 = MD5MD5(UA+KEY+密码), 与 User-Agent 绑定 -> 全程固定 UA)
  3. GET  /admin/ADD.txt     读后台现有「自定义优选IP」内容
  4. 合并: 剔除上一次自动同步的托管块(哨兵注释) + 清掉手工粘贴的旧块、同名旧条目
     和早期「机房」遗留条目, 再把新清单追加到末尾
     (等价于 README 第 4 步「光标移到现有内容末尾粘贴」)
  5. POST /admin/ADD.txt     原文 body 保存, 校验返回 message == 自定义IP已保存
  6. GET  /admin/ADD.txt     回读校验 (确认哨兵块确实落库)

退出码: 0 = 成功 (含「内容无变化」跳过); 1 = 失败 (与 vpngate.py 一致, 不允许假成功)

用法:
  python push_edt_hosts.py                 # 同步一次 (密码: --password / 环境变量 / 交互输入)
  python push_edt_hosts.py --dry-run       # 只下载+合并预览, 不登录不保存 (无需密码)
  python push_edt_hosts.py --watch         # 常驻循环, 每 30 分钟自动同步一次
  python push_edt_hosts.py --base https://你的域名   # 指定 edgetunnel 地址

密码来源 (优先级从高到低): --password 参数 > 环境变量 EDT_ADMIN_PASSWORD > 交互输入(不回显)
注意: 密码即 edgetunnel Worker 上的 ADMIN 环境变量, 只存在你自己的环境里, 脚本不会打印它。
"""

import argparse
import os
import re
import sys
import time

import requests

# 保证日志在任何控制台编码下都能输出 (Windows GBK 控制台不会崩)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 固定 UA: 后台 auth cookie 与 User-Agent 绑定, 登录/读写必须用同一个
UA = "wh-gate-sync/1.0"

# 自动同步托管块的哨兵注释 (用于下一次运行时整块替换, 避免无限追加)
SENTINEL_START = "# >>> wh-gate auto-sync >>>"
SENTINEL_END = "# <<< wh-gate auto-sync <<<"
# vpngate.py 生成的 hosts.txt 头部特征 (识别「手工粘贴」留下的旧块)
LEGACY_MARKER = "「自定义优选IP」清单"

HTTP_TIMEOUT = 30

# ---------------------------------------------------------------------------
# 日志 (与 vpngate.py 相同的分区格式)
# ---------------------------------------------------------------------------
_section = None


def log(section, msg=""):
    global _section
    if section != _section:
        print(f"========== {section} ==========")
        _section = section
    if msg:
        print(msg, flush=True)


def die(msg):
    """硬性失败: 明确报错并退出非 0, 绝不允许假成功。"""
    log("FATAL", f"[失败] {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 配置 (均可用环境变量覆盖)
# ---------------------------------------------------------------------------
def default_base():
    """edgetunnel 后台地址: EDT_BASE > vpngate.py 的 EDT_DOMAIN > 写死兜底"""
    base = os.environ.get("EDT_BASE", "").strip().rstrip("/")
    if base:
        return base
    try:
        from vpngate import EDT_DOMAIN  # 单一来源: 与清单生成用同一个域名

        return f"https://{EDT_DOMAIN}"
    except Exception:
        return "https://xi.xiaohe.gv.uy"


def default_hosts_urls():
    """hosts.txt 来源列表 (按顺序尝试):
    HOSTS_FILE (本地文件, CI 里指刚生成的 public/hosts.txt) > HOSTS_URL > vpngate.py > 上游在线地址兜底
    """
    urls = []
    env_file = os.environ.get("HOSTS_FILE", "").strip()
    if env_file:
        urls.append(env_file)
    env_url = os.environ.get("HOSTS_URL", "").strip()
    if env_url:
        urls.append(env_url)
    try:
        from vpngate import HOSTS_URL as _u

        urls.append(_u)
    except Exception:
        pass
    urls.append("https://jerylihub.github.io/gate/hosts.txt")  # 上游在线地址 (兜底)
    # 去重, 保持顺序
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ---------------------------------------------------------------------------
# 第 1 步: 下载 hosts.txt
# ---------------------------------------------------------------------------
def _read_source(url):
    """读取一个来源。支持 http(s):// 与本地路径 / file://
    (CI 里直接读本次刚生成的 public/hosts.txt, 不依赖 GitHub Pages CDN 生效延迟)。"""
    if url.startswith("file://"):
        path = url[7:]
    elif "://" not in url:
        path = url
    else:
        path = None
    if path is not None:
        log("HOSTS", f"读取本地: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    log("HOSTS", f"下载: {url}")
    resp = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": UA})
    resp.raise_for_status()
    return resp.text


def fetch_hosts(urls):
    """按顺序尝试所有来源, 返回 (content, url)。全部失败 -> die。"""
    for url in urls:
        try:
            text = _read_source(url)
            # 基本健全性: 必须有链式代理指令, 否则可能是 404 页/登录页
            if "$sstp://" not in text:
                log("HOSTS", "  跳过: 内容不像 hosts.txt (缺少 $sstp:// 条目)")
                continue
            log("HOSTS", f"  成功: {len(text)} 字节, {text.count(chr(10)) + 1} 行")
            return text, url
        except Exception as exc:
            log("HOSTS", f"  失败: {exc}")
    die("所有 hosts.txt 地址均不可用")



# ---------------------------------------------------------------------------
# 第 2 步: 登录 edgetunnel 后台
# ---------------------------------------------------------------------------
def login(session, base, password):
    """POST /login (表单 password=...)。成功 -> 服务端 Set-Cookie: auth=..."""
    log("LOGIN", f"{base}/login")
    try:
        resp = session.post(
            f"{base}/login",
            data={"password": password},
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
        )
    except Exception as exc:
        die(f"登录请求失败: {exc}")
    if "auth" not in session.cookies.get_dict():
        die("登录失败: 未收到 auth cookie (密码错误, 或 ADMIN 环境变量为空)")
    log("LOGIN", "登录成功 (已获得 auth cookie)")


def require_auth(resp, what):
    """后台接口在 cookie 缺失/失效时返回 302 -> /login, 统一拦截。"""
    if resp.status_code == 302:
        die(f"{what}: 被重定向到登录页 (auth cookie 失效)")
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# 第 3 步: 读取后台现有「自定义优选IP」
# ---------------------------------------------------------------------------
def get_add(session, base):
    resp = session.get(f"{base}/admin/ADD.txt", timeout=HTTP_TIMEOUT, allow_redirects=False)
    require_auth(resp, "读取 admin/ADD.txt")
    return resp.text


# ---------------------------------------------------------------------------
# 第 4 步: 合并 (剔旧 + 追加)
# ---------------------------------------------------------------------------
def _looks_like_hosts_line(line):
    """判断一行是否属于 vpngate 生成的 hosts 清单 (注释/空行/条目)。"""
    s = line.strip()
    if not s:
        return True
    if s.startswith("#"):
        return True
    # 入口:443#名字$sstp://vpn:vpn@host:port
    return bool(re.match(r"^\S+:\d+#[^\s]+\$sstp://\S+$", s))


def _strip_sentinel_blocks(lines):
    """删除所有哨兵托管块 (SENTINEL_START..SENTINEL_END; 悬空 START 删到末尾)。"""
    out, skip = [], False
    for ln in lines:
        if ln.strip() == SENTINEL_START:
            skip = True
            continue
        if ln.strip() == SENTINEL_END:
            skip = False
            continue
        if not skip:
            out.append(ln)
    return out


def _strip_legacy_tail(lines):
    """删除「手工粘贴」留下的旧 hosts 块: 从最后一个头部特征行开始,
    只删长得像 hosts 清单的行, 碰到用户自己的其它内容就停 (避免误删)。"""
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if LEGACY_MARKER in lines[i] and lines[i].lstrip().startswith("#"):
            start = i
            break
    if start is None:
        return lines
    end = start
    while end < len(lines) and _looks_like_hosts_line(lines[end]):
        end += 1
    return lines[:start] + lines[end:]


def _strip_same_name_entries(lines, keep_names):
    """删除与新清单同名的旧条目 (按 #名字$sstp:// 里的名字判断),
    保证每个节点名只保留最新一条 (名字固定、地址每 30 分钟换)。"""
    out = []
    for ln in lines:
        m = re.search(r"#([^\s#]+)\$sstp://", ln)
        if m and m.group(1) in keep_names:
            continue
        out.append(ln)
    return out


# 生成器自产的节点名特征: 国家-住宅-N / 国家-机房-N
_OWNED_NAME_RE = re.compile(r"-(?:住宅|机房)-\d+$")


def _is_owned_entry(line):
    """判断一行是否是生成器自产的条目 (含 #名字$sstp:// 或裸 名字$sstp://)。"""
    m = re.search(r"#([^\s#]+)\$sstp://", line) or re.match(r"^\s*([^\s#]+)\$sstp://", line)
    return bool(m and _OWNED_NAME_RE.search(m.group(1)))


def _strip_owned_entries(lines):
    """删除所有生成器自产的条目 (国家-住宅-N / 国家-机房-N)。

    切换到「仅住宅」后, 早期版本下发的「机房」条目会残留在后台, 这里一并清掉,
    保证每个节点名只有最新一条、且不再出现机房节点 (名字固定语义)。
    用户自己填的其它内容不受影响。
    """
    return [ln for ln in lines if not _is_owned_entry(ln)]



def merge(existing, new_text):
    """把新 hosts 清单合并进后台现有内容, 返回合并后的完整文本。"""
    # 新清单里的全部节点名 (用于清理同名旧条目)
    keep_names = set(re.findall(r"#([^\s#]+)\$sstp://", new_text))

    lines = existing.replace("\r\n", "\n").split("\n")
    lines = _strip_sentinel_blocks(lines)
    lines = _strip_legacy_tail(lines)
    lines = _strip_owned_entries(lines)
    lines = _strip_same_name_entries(lines, keep_names)

    # 去掉尾部空行, 追加哨兵托管块 (等价于「光标移到末尾 Ctrl+V」)
    while lines and not lines[-1].strip():
        lines.pop()
    head = "\n".join(lines).rstrip("\n")
    block = new_text.replace("\r\n", "\n")
    if not block.endswith("\n"):
        block += "\n"

    parts = []
    if head:
        parts.append(head + "\n")
    parts.append(SENTINEL_START + "\n")
    parts.append(block)
    parts.append(SENTINEL_END + "\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 第 5/6 步: 保存 + 回读校验
# ---------------------------------------------------------------------------
def push_add(session, base, content):
    resp = session.post(
        f"{base}/admin/ADD.txt",
        data=content.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
        timeout=HTTP_TIMEOUT,
        allow_redirects=False,
    )
    require_auth(resp, "保存 admin/ADD.txt")
    try:
        body = resp.json()
    except Exception:
        die(f"保存返回的不是 JSON: {resp.text[:200]}")
    if not body.get("success") or body.get("message") != "自定义IP已保存":
        die(f"保存未确认: {body}")
    log("SAVE", "保存成功: 自定义IP已保存")

    # 回读校验: 哨兵块必须真的落到 KV
    again = get_add(session, base)
    if SENTINEL_START not in again:
        die("回读校验失败: 后台内容里找不到本次同步的哨兵块")
    log("SAVE", f"回读校验通过 (后台共 {len(again)} 字节)")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def sync_once(base, password, hosts_urls, dry_run=False):
    hosts_text, hosts_url = fetch_hosts(hosts_urls)
    log("SYNC", f"清单来源: {hosts_url}")

    if dry_run:
        # 不登录不保存: 用空内容演示合并结果
        merged = merge("", hosts_text)
        names = sorted(set(re.findall(r"#([^\s#]+)\$sstp://", hosts_text)))
        log("DRY-RUN", f"清单条目 {len(names)} 个, 合并后 {len(merged)} 字节")
        log("DRY-RUN", "预览前 15 行:")
        for ln in merged.split("\n")[:15]:
            print(f"  | {ln}")
        log("DRY-RUN", "未连接后台 (--dry-run)")
        return True

    if not password:
        die("未提供后台密码 (用 --password / 环境变量 EDT_ADMIN_PASSWORD / 交互输入)")

    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    login(session, base, password)

    existing = get_add(session, base)
    log("MERGE", f"后台现有内容 {len(existing)} 字节")
    merged = merge(existing, hosts_text)
    if merged == existing:
        log("SYNC", "内容无变化, 跳过保存")
        return True
    push_add(session, base, merged)
    return True


def main():
    parser = argparse.ArgumentParser(description="自动同步 hosts.txt 到 edgetunnel 后台「自定义优选IP」")
    parser.add_argument("--base", default=default_base(),
                        help="edgetunnel 后台地址 (默认读 vpngate.py 的 EDT_DOMAIN)")
    parser.add_argument("--hosts-url", action="append", default=None,
                        help="hosts.txt 地址, 可重复指定; 默认按顺序尝试 env/vpngate.py/上游")
    parser.add_argument("--password", default=os.environ.get("EDT_ADMIN_PASSWORD", ""),
                        help="后台密码 (默认读环境变量 EDT_ADMIN_PASSWORD)")
    parser.add_argument("--dry-run", action="store_true", help="只下载并预览合并结果, 不连接后台")
    parser.add_argument("--watch", action="store_true", help="常驻循环, 每 interval 秒同步一次")
    parser.add_argument("--interval", type=int, default=1800,
                        help="watch 模式同步间隔秒数 (默认 1800 = 30 分钟)")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    hosts_urls = args.hosts_url or default_hosts_urls()
    password = args.password

    if not args.dry_run and not password and sys.stdin.isatty():
        import getpass

        password = getpass.getpass("edgetunnel 后台密码 (ADMIN): ")

    if args.watch:
        log("WATCH", f"常驻模式: 每 {args.interval} 秒同步一次 (Ctrl+C 退出)")
        while True:
            try:
                sync_once(base, password, hosts_urls, dry_run=args.dry_run)
            except SystemExit:
                raise
            except Exception as exc:
                log("WATCH", f"[异常] {exc} (下轮重试)")
            log("WATCH", f"等待 {args.interval} 秒...")
            time.sleep(args.interval)
    else:
        sync_once(base, password, hosts_urls, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
    except Exception as exc:  # 兜底: 任何未预料的异常都按失败处理
        die(f"程序异常: {exc}")

