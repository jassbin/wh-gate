#!/usr/bin/env python3
"""
VPN Gate SSTP 节点检测流水线
============================
流程:
  1. 获取 VPN Gate 原始节点 (官方 api/iphone CSV, 失败时回退 GitHub 预解析镜像)
  2. 只保留「带 TCP 入口」的中继 = SSTP 可用节点
     (OpenVPN 配置里 proto tcp + remote <ip> <port>; UDP-only 中继无法走 SSTP/xray 链, 直接丢弃)
  3. 按 host+port+protocol 去重
  4. 并发调用已部署的 Cloudflare Worker:  GET {WORKER}/check?sstp=vpn:vpn@host:port (WORKER 由 CHECK_WORKER 环境变量传入, 可含 /<密钥> 前缀)
     (单节点 HTTP 成功 != 节点可用; 以 Worker 返回 JSON 的 success 字段为准)
  5. 只保留 success=true 且住宅 (residential) 的节点 (机房/未知丢弃), 按国家分组, 生成 public/data.json + public/index.html
      + public/chains.txt + public/hosts.txt (EDT_UUID + PUBLISH_SUB=1 时才另生成 public/sub.txt)
  6. 网页端 (GitHub Pages) 读取 data.json 展示

退出码:
  0 = 正常完成 (允许部分节点检测失败)
  1 = 硬性失败 (数据源全挂 / 解析不出 SSTP 节点 / Worker 完全不可达 / 程序异常)
     这些情况绝不允许"假成功"
"""

import base64
import csv
import io
import json
import os
import re
import socket
import ssl
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote

import requests

# 保证日志在任何控制台编码下都能输出 (Windows GBK 控制台不会崩)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 配置 (均可用环境变量覆盖, 便于本地测试)
# ---------------------------------------------------------------------------
REPO_DIR = os.path.dirname(os.path.abspath(__file__))


def _env(name, default=""):
    """读取环境变量并去掉其中所有空白字符。

    CI 里这些值由仓库 Secret 拼装而成, Secret 若混入换行/空格会破坏 URL
    (实测: CHECK_TOKEN 带尾部换行 -> 拼出的 CHECK_WORKER 含换行 -> 全部请求异常)。
    """
    return "".join(os.environ.get(name, default).split())


def _pages_base():
    """本仓库 GitHub Pages 根地址 (清单头部「固定地址」用, 见 CHAIN_URL/HOSTS_URL/SUB_URL)。

    Actions 会自动注入 GITHUB_REPOSITORY=owner/repo, 据此推导:
    仓库改名 / 换账号 / 从 fork 迁到独立仓库后, 地址自动跟着变, 不必改源码。
    非 CI 环境 (本地运行) 没有该变量, 回退到写死地址。
    """
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" in slug:
        owner, repo = slug.split("/", 1)
        # 用户主页仓库 (<owner>.github.io): Pages 地址不带仓库名
        if repo.lower() == f"{owner.lower()}.github.io":
            return f"https://{owner}.github.io"
        return f"https://{owner}.github.io/{repo}"
    return "https://whua898.github.io/wh-gate"


PAGES_BASE = _pages_base()


VPNGATE_API = _env("VPNGATE_API", "http://www.vpngate.net/api/iphone/")
# 官方接口失败时的回退数据源: 预解析 JSON 镜像 (字段与官方 CSV 同源)
VPNGATE_MIRROR = _env(
    "VPNGATE_MIRROR",
    "https://raw.githubusercontent.com/fdciabdul/Vpngate-Scraper-API/main/json/data.json",
)
# 已部署的 Cloudflare Worker 检测接口 (GET /check?sstp=vpn:vpn@host:port, 实测确认)
WORKER_CHECK_URL = _env("CHECK_WORKER", "https://你的检测Worker域名/check?sstp=vpn:vpn@")  # ⚠️ 必改：换成你自己的检测 Worker 域名
# 检测模式: direct = 本机直连检测 (不依赖 Cloudflare, 默认); worker = 经 Cloudflare Worker 检测
CHECK_MODE = os.environ.get("CHECK_MODE", "direct").strip().lower()
DIRECT_SSTP_TIMEOUT = float(os.environ.get("DIRECT_SSTP_TIMEOUT", "15"))  # 直连单节点超时 (秒)
CONCURRENCY = max(1, int(os.environ.get("CHECK_CONCURRENCY", "32")))   # 与 Worker 网页端一致的并发模型
CHECK_TIMEOUT = float(os.environ.get("CHECK_TIMEOUT", "90"))          # 单请求客户端超时 (秒)
MAX_CHECK_NODES = int(os.environ.get("MAX_CHECK_NODES", "0"))         # 0=不限; 本地测试可设小值
# 下发过滤: 只要住宅 (residential), 丢弃机房/未知; 用 ed 其他节点补非住宅场景
# 环境变量 ONLY_RESIDENTIAL=0 可关闭 (默认 1 = 只收录住宅)
ONLY_RESIDENTIAL = os.environ.get("ONLY_RESIDENTIAL", "1").strip().lower() in ("1", "true", "yes")
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "60"))              # 拉取数据源超时
PUBLIC_DIR = os.environ.get("PUBLIC_DIR", os.path.join(REPO_DIR, "public"))
TEMPLATE_HTML = os.path.join(REPO_DIR, "web", "index.html")
# 清单头部「每 X 分钟重新检测」的 X 从何而来:
# Actions 不会向 job 注入 cron 表达式, workflow 层直接声明一个自描述的环境变量即可
# (改频率时只改 check.yml 里的 cron 和这里对应的 REFRESH_MINUTES, 生成的注释自动跟上)。
REFRESH_MINUTES = os.environ.get("REFRESH_MINUTES", "30").strip() or "30"
REFRESH_LABEL = f"每 {REFRESH_MINUTES} 分钟重新检测"
# 「名字固定, 地址自动换」类注释的后半句, 由 REFRESH_MINUTES 派生 (默认「30 分钟自动更换」)
REFRESH_CHANGE_LABEL = f"{REFRESH_MINUTES} 分钟自动更换"

# 出口数据中心的关键词启发 (判断"是否住宅 IP"用, 页面标注为估算)
DATA_CENTER_ORG_KEYWORDS = [
    "GOOGLE", "AMAZON", "AWS", "MICROSOFT", "OVH", "HETZNER", "DIGITALOCEAN",
    "AKAMAI", "CLOUDFLARE", "FASTLY", "RACKSPACE", "EQUINIX", "LINODE", "VULTR",
    "HURRICANE", "TENCENT", "ALIBABA", "ALIYUN", "LEASWEB",
]
# 常见住宅宽带运营商关键词
RESIDENTIAL_ORG_KEYWORDS = [
    "NTT EAST", "NTT WEST", "NTT COMMUNICATIONS", "NTT BROADBAND", "KDDI", "DOCOMO",
    "SOFTBANK", "AU COMMUNICATIONS", "J:COM", "JCOM", "OCN", "BIGLOBE",
    "IIJ", "SEIKO", "CLEVER-NET", "AT&T", "COMCAST", "XFINITY", "VERIZON",
    "TELUS", "ROGERS", "BELL CANADA", "VODAFONE", "ORANGE", "DEUTSCHE TELEKOM",
    "BREEZE", "TIM S.P.A", "LIBERO", "FASTWEB", "FREE FRANCE", "BT OPEN",
]

# ISO 国家码 -> 中文名 (edgetunnel 清单展示用; 未收录则回退英文原名)
COUNTRY_ZH = {
    "JP": "日本", "KR": "韩国", "US": "美国", "CA": "加拿大", "RU": "俄罗斯",
    "RO": "罗马尼亚", "TH": "泰国", "VN": "越南", "DE": "德国", "FR": "法国",
    "GB": "英国", "UK": "英国", "SG": "新加坡", "TW": "台湾", "HK": "香港",
    "CN": "中国", "AU": "澳大利亚", "NL": "荷兰", "SE": "瑞典", "CH": "瑞士",
    "IT": "意大利", "ES": "西班牙", "PL": "波兰", "IN": "印度", "BR": "巴西",
    "MX": "墨西哥", "ID": "印度尼西亚", "MY": "马来西亚", "PH": "菲律宾",
    "TR": "土耳其", "UA": "乌克兰", "CZ": "捷克", "GR": "希腊", "PT": "葡萄牙",
    "FI": "芬兰", "NO": "挪威", "DK": "丹麦", "IE": "爱尔兰", "BE": "比利时",
    "AT": "奥地利", "HU": "匈牙利", "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚",
    "NZ": "新西兰", "ZA": "南非", "IL": "以色列", "AE": "阿联酋", "SA": "沙特",
    "EG": "埃及", "HR": "克罗地亚", "BY": "白俄罗斯", "GD": "格林纳达",
    "LV": "拉脱维亚", "EE": "爱沙尼亚", "LT": "立陶宛", "SK": "斯洛伐克",
    "SI": "斯洛文尼亚", "BG": "保加利亚", "RS": "塞尔维亚", "GE": "格鲁吉亚",
    "MD": "摩尔多瓦", "AM": "亚美尼亚", "KZ": "哈萨克斯坦", "UZ": "乌兹别克斯坦",
    "MN": "蒙古", "NP": "尼泊尔", "LK": "斯里兰卡", "MM": "缅甸",
}

# ---------------------------------------------------------------------------
# 日志 (用户要求的分区格式)
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
# 第 1 步: 获取 VPN Gate 原始节点
# ---------------------------------------------------------------------------
def fetch_vpngate():
    """返回 (rows, source)。rows: [{host, ip, country_long, country_short, config_b64}]
    官方 API 失败时回退镜像 JSON; 两个都失败 -> 直接 die (exit 1)。"""
    # --- 主源: 官方 CSV ---
    try:
        log("VPN GATE", f"获取官方 API: {VPNGATE_API}")
        resp = requests.get(
            VPNGATE_API,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; gate-checker)"},
        )
        resp.raise_for_status()
        rows = parse_csv(resp.text)
        if rows:
            log("VPN GATE", f"主源(官方 API) 获取到 {len(rows)} 个原始节点")
            return rows, "vpngate.net/api/iphone"
        raise RuntimeError("官方 API 返回 0 行数据")
    except Exception as exc:
        log("VPN GATE", f"官方 API 获取失败: {exc}")

    # --- 回退源: GitHub 预解析镜像 ---
    try:
        log("VPN GATE", f"回退镜像: {VPNGATE_MIRROR}")
        resp = requests.get(VPNGATE_MIRROR, timeout=HTTP_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        rows = parse_mirror_json(resp.json())
        if rows:
            log("VPN GATE", f"回退源(镜像) 获取到 {len(rows)} 个原始节点")
            return rows, "github-mirror"
    except Exception as exc:
        log("VPN GATE", f"回退镜像也失败: {exc}")
    die("VPN Gate 官方 API 与回退镜像均不可用, 数据源完全失败 (不生成空结果, 本次运行判定失败)")


def parse_csv(text):
    """解析官方 CSV。表头行含 'HostName'; 按列名映射, 列名缺失时用固定位置回退。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_idx = None
    for i, ln in enumerate(lines):
        if ln.lstrip("#").startswith("HostName"):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("找不到 CSV 表头行 (HostName)")

    header = lines[header_idx].lstrip("#").split(",")
    data_lines = lines[header_idx + 1:]
    # 列名映射 (不假设固定位置, 列名变化时自动适配; 全缺失时回退到已知位置)
    idx = {}
    for col in ("hostname", "ip", "countrylong", "countryshort", "openvpn_configdata_base64"):
        for i, h in enumerate(header):
            if h.strip().lstrip("*").lower() == col:
                idx[col] = i
                break
    if "openvpn_configdata_base64" not in idx:
        for i, h in enumerate(header):
            if "base64" in h.lower():
                idx["openvpn_configdata_base64"] = i
                break
    pos = {"hostname": idx.get("hostname", 0),
           "ip": idx.get("ip", 1),
           "countrylong": idx.get("countrylong", 5),
           "countryshort": idx.get("countryshort", 6),
           "openvpn_configdata_base64": idx.get("openvpn_configdata_base64", len(header) - 1)}

    rows = []
    for ln in data_lines:
        fields = next(csv.reader(io.StringIO(ln)))
        if len(fields) < 7:
            continue
        host = fields[pos["hostname"]].strip()
        ip = fields[pos["ip"]].strip()
        if not host or not ip:
            continue
        rows.append({
            "host": host,
            "ip": ip,
            "country_long": fields[pos["countrylong"]].strip(),
            "country_short": fields[pos["countryshort"]].strip(),
            "config_b64": fields[pos["openvpn_configdata_base64"]].strip(),
        })
    return rows


def parse_mirror_json(data):
    """解析 GitHub 镜像 JSON: [ { "servers": [ {hostname, ip, countrylong, countryshort, openvpn_configdata_base64} ] } ]"""
    servers = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("servers"), list):
            servers.extend(item["servers"])
        elif isinstance(item, dict):
            servers.append(item)
    rows = []
    for s in servers:
        host = str(s.get("hostname") or s.get("host") or "").strip()
        ip = str(s.get("ip") or "").strip()
        if not host or not ip:
            continue
        rows.append({
            "host": host,
            "ip": ip,
            "country_long": str(s.get("countrylong") or s.get("country_long") or s.get("country") or "").strip(),
            "country_short": str(s.get("countryshort") or s.get("country_short") or "").strip(),
            "config_b64": str(s.get("openvpn_configdata_base64") or s.get("config_b64") or "").strip(),
        })
    return rows


# ---------------------------------------------------------------------------
# 第 2 步: 筛选 SSTP 节点 (只保留带 TCP 入口的中继)
# ---------------------------------------------------------------------------
_PROTO_TCP_RE = re.compile(r"^proto\s+(tcp|tcp4|tcp6)\b", re.M)
_REMOTE_RE = re.compile(r"^remote\s+\S+\s+(\d+)", re.M)


def to_sstp_nodes(rows):
    """把原始行转成 SSTP 节点: 解码 OpenVPN 配置, 仅保留 proto tcp + remote 端口。
    host 统一为 <short>.opengw.net 形式; 返回去重前的节点列表。"""
    nodes = []
    for r in rows:
        cfg = ""
        if r["config_b64"]:
            try:
                cfg = base64.b64decode(r["config_b64"], validate=False).decode("utf-8", "replace")
            except Exception:
                cfg = ""
        if not _PROTO_TCP_RE.search(cfg):
            continue  # 无 TCP 入口 -> 不是 SSTP 可用节点, 丢弃
        m = _REMOTE_RE.search(cfg)
        if not m:
            continue
        port = int(m.group(1))
        if not (1 <= port <= 65535):
            continue
        host = r["host"]
        if not host.endswith(".opengw.net"):
            host = f"{host}.opengw.net"
        nodes.append({
            "host": host,
            "port": port,
            "ip": r["ip"],
            "country": r["country_long"],
            "country_code": r["country_short"],
        })
    return nodes


def dedupe(nodes):
    """按 host+port+protocol 去重。"""
    seen = set()
    out = []
    for n in nodes:
        key = (n["host"].lower(), n["port"], "sstp")
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


# ---------------------------------------------------------------------------
# 第 3 步: 并发调用 Cloudflare Worker
# ---------------------------------------------------------------------------
def classify_network(host, exit_org, is_datacenter=None):
    """住宅/机房分类, 按可信度排序:
    1) Worker 返回的真实 is_datacenter 标志 (IP 情报库);
    2) 出口 ASN 组织名关键词;
    3) host 前缀启发式 (最后兜底, 属估算)。"""
    # 1) 真实数据中心标志 (SSTP 版 Worker 顶层 exit 直接给出)
    if is_datacenter is True:
        return "datacenter"
    if is_datacenter is False:
        return "residential"
    # 2) 出口组织名关键词
    org = (exit_org or "").upper()
    if org:
        if any(k in org for k in DATA_CENTER_ORG_KEYWORDS):
            return "datacenter"
        if any(k in org for k in RESIDENTIAL_ORG_KEYWORDS):
            return "residential"
    # 3) host 前缀启发式 (估算)
    h = host.lower()
    if h.startswith("public-vpn"):
        return "datacenter"      # VPN Gate 官方公共中继 (机房/托管)
    if re.match(r"^vpn\d{5,}", h) or re.match(r"^vpnv\d+", h):
        return "residential"     # 数字编号 = 注册的家用宽带中继 (家宽, 估算)
    return "unknown"


# ---------------------------------------------------------------------------
# 直连检测 (不依赖 Cloudflare Worker)
# ---------------------------------------------------------------------------
def _direct_sstp_check(host, port):
    """TCP + TLS + SSTP HTTP 握手。返回 (success, latency_ms, error)。"""
    start = time.monotonic()
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=DIRECT_SSTP_TIMEOUT)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        tls = ctx.wrap_socket(sock, server_hostname=host)
        tls.settimeout(DIRECT_SSTP_TIMEOUT)
        sock = tls
        corr_id = str(uuid.uuid4()).upper()
        req = (
            f"SSTP_DUPLEX_POST /sra_{{BA195980-CD49-458b-9E23-C84EE0ADCD75}}/ HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Content-Length: 18446744073709551615\r\n"
            f"SSTPCORRELATIONID: {{{corr_id}}}\r\n"
            f"\r\n"
        )
        tls.sendall(req.encode())
        data = b""
        while b"\r\n" not in data:
            chunk = tls.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 8192:
                break
        latency_ms = int((time.monotonic() - start) * 1000)
        status_line = data.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        if re.match(r"HTTP/\d(?:\.\d)?\s+2\d\d", status_line, re.I):
            return True, latency_ms, None
        return False, latency_ms, f"SSTP handshake rejected: {status_line[:80]}"
    except socket.timeout:
        return False, None, "connection timeout"
    except ConnectionRefusedError:
        return False, None, "connection refused"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"[:100]
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def _direct_ip_lookup(ip):
    """ip-api.com 查 ASN/地理。返回 dict 或 None。"""
    try:
        r = requests.get(
            f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,city,org,isp,as,query",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (gate-checker)"},
        )
        j = r.json()
        if j.get("status") != "success":
            return None
        org = j.get("org") or j.get("isp") or ""
        asn_num = None
        asn_str = j.get("as") or ""
        if asn_str.startswith("AS"):
            try:
                asn_num = int(asn_str.split()[0][2:])
            except (ValueError, IndexError):
                pass
        return {
            "ip": j.get("query") or ip,
            "country": j.get("country"),
            "country_code": j.get("countryCode"),
            "city": j.get("city"),
            "asn": asn_num,
            "org": org,
        }
    except Exception:
        return None


def check_one_direct(node):
    """直连模式：本机直接检测 SSTP 节点，不经过 Cloudflare Worker。
    返回与 check_one 相同格式的 dict。"""
    out = dict(node)
    out["protocol"] = "sstp"
    out["link"] = f"sstp://vpn:vpn@{node['host']}:{node['port']}"
    out["status"] = "failed"
    out["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out["exit"] = None
    out["residential"] = "unknown"
    out["check_mode"] = "direct"

    ok, latency_ms, err = _direct_sstp_check(node["host"], int(node.get("port") or 443))
    out["success"] = ok
    out["status"] = "success" if ok else "failed"
    out["latency_ms"] = latency_ms
    out["error"] = err
    if not ok:
        return out

    # 存活才查 IP 信息（省配额）
    info = _direct_ip_lookup(node.get("ip") or node["host"])
    if info:
        out["exit"] = {
            "ip": info["ip"],
            "country": info["country"],
            "country_code": info["country_code"],
            "city": info["city"],
            "asn": info["asn"],
            "org": info["org"],
            "type": None,
            "is_datacenter": None,  # 直连模式无精确标志，靠 classify_network 关键词判断
        }
        out["residential"] = classify_network(out["host"], info["org"], None)
    else:
        out["residential"] = classify_network(out["host"], None, None)
    return out


def check_one(node, session):
    """调用 Worker 检测单节点。返回节点+检测结果的合并 dict。
    单节点失败 (网络错误/非 200/坏 JSON) 不会抛出, 统一记 success=False。"""
    if CHECK_MODE == "direct":
        return check_one_direct(node)
    url = WORKER_CHECK_URL + quote(f"{node['host']}:{node['port']}", safe="")
    out = dict(node)
    out["protocol"] = "sstp"
    out["link"] = f"sstp://vpn:vpn@{node['host']}:{node['port']}"
    out["status"] = "failed"
    out["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out["exit"] = None
    out["residential"] = "unknown"
    try:
        r = session.get(url, timeout=CHECK_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (gate-checker)"})
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
            out["worker_error"] = True
            return out
        j = r.json()
        ok = bool(j.get("success"))
        out["success"] = ok
        out["status"] = "success" if ok else "failed"
        out["latency_ms"] = j.get("responseTime")
        out["colo"] = j.get("colo")
        out["error"] = (None if ok else (j.get("error") or j.get("message") or "check failed"))
        # SSTP 版 Worker: 顶层直接返回 exit, 含真实 is_datacenter 标志 + 嵌套 asn 对象
        exit_info = j.get("exit") or {}
        if exit_info:
            asn = exit_info.get("asn") or {}
            org = asn.get("org") or asn.get("name") or ""
            out["exit"] = {
                "ip": exit_info.get("ip"),
                "country": exit_info.get("country"),
                "country_code": exit_info.get("country_code"),
                "city": exit_info.get("city"),
                "continent": exit_info.get("continent"),
                "asn": asn.get("asn"),
                "org": org,
                "type": asn.get("type"),
                "is_datacenter": exit_info.get("is_datacenter"),
            }
            out["residential"] = classify_network(out["host"], org, exit_info.get("is_datacenter"))
        else:
            out["residential"] = classify_network(out["host"], None, None)
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["worker_error"] = True
        return out


def check_all(nodes, session):
    """32 并发 (与网页端一致)。单节点失败不影响整体; 但区分'节点不可用'与'Worker 异常'。"""
    results = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(check_one, n, session) for n in nodes]
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


# ---------------------------------------------------------------------------
# 第 4 步: 生成网页数据
# ---------------------------------------------------------------------------
def build_outputs(results, raw_count, sstp_count, source):
    success_all = [r for r in results if r.get("success")]
    dropped_non_res = 0
    if ONLY_RESIDENTIAL:
        dropped_non_res = sum(1 for r in success_all if r.get("residential") != "residential")
        available = [r for r in success_all if r.get("residential") == "residential"]
        if success_all and not available:
            die(f"本轮 {len(success_all)} 个可用节点全是非住宅, 已全部过滤 (不再下发机房/未知) —— 不生成空结果")
    else:
        available = success_all
    countries = {}
    for n in available:
        c = n["country"] or "未知"
        countries.setdefault(c, {"code": n["country_code"] or "?", "nodes": []})["nodes"].append(n)

    stats = {
        "raw_nodes": raw_count,
        "sstp_nodes": sstp_count,
        "checked": len(results),
        "success": len(available),
        "success_all": len(success_all),
        "dropped_non_residential": dropped_non_res,
        "only_residential": ONLY_RESIDENTIAL,
        "failed": len(results) - len(success_all),
        "countries": len(countries),
        "residential_est": sum(1 for n in available if n["residential"] == "residential"),
        "datacenter_est": sum(1 for n in available if n["residential"] == "datacenter"),
    }

    by_country = {}
    for name, grp in countries.items():
        grp["count"] = len(grp["nodes"])
        grp["residential"] = sum(1 for n in grp["nodes"] if n["residential"] == "residential")
        grp["datacenter"] = sum(1 for n in grp["nodes"] if n["residential"] == "datacenter")
        grp["nodes"].sort(key=lambda n: (n.get("latency_ms") is None, n.get("latency_ms") or 0, n["host"]))
        by_country[name] = grp

    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": source,
        "worker": WORKER_CHECK_URL,
        "stats": stats,
        "countries": by_country,
        "available": available,
    }
    return data


CHAIN_URL = _env("CHAIN_URL", f"{PAGES_BASE}/chains.txt")


def build_chains_text(data):
    """生成 edgetunnel 链式代理清单: 按国家分组, 每国独立编号, 仅收录住宅节点 (机房/未知已过滤)。
    每行 = 「名字 + $sstp://vpn:vpn@host:port」, 名字不变, 指令随 REFRESH_MINUTES 自动换。"""
    countries = data["countries"]
    lines = [
        "# VPN Gate SSTP 节点 -> edgetunnel 链式代理清单 (仅住宅)",
        f"# 自动更新: {data['generated_at']} ({REFRESH_LABEL})",
        f"# 固定地址: {CHAIN_URL}",
        "#",
        "# 用法: 在 edgetunnel 节点备注里直接粘贴下面任意一行 (名字与指令连写, 逗号分隔多行)",
        "#   例: 日本-住宅-01$sstp://vpn:vpn@vpnxxx.opengw.net:443",
        "# 非住宅需求请用 ed 其他节点补; 本清单只收录住宅",
        f"# 名字保持不变, 只有 $sstp:// 后面的地址{REFRESH_CHANGE_LABEL}",
        "# 账号密码固定 vpn:vpn ; 端口必须保留",
        "# ========================================================",
    ]
    ordered = sorted(
        countries.items(),
        key=lambda kv: (-int(kv[1].get("count") or 0), str(kv[1].get("code") or kv[0])),
    )
    for cname, grp in ordered:
        code = str(grp.get("code") or "?").upper()
        zh = COUNTRY_ZH.get(code) or (code if code and code != "?" else cname)
        nodes = sorted(
            grp["nodes"],
            key=lambda n: (
                n.get("latency_ms") is None,
                n.get("latency_ms") or 0,
                n.get("host") or "",
            ),
        )
        lines.append("")
        lines.append(
            f"# ---- {zh} {code} · {grp['count']} 住宅节点 ----"
        )
        for i, n in enumerate(nodes, 1):
            lines.append(f"{zh}-住宅-{i:02d}$sstp://vpn:vpn@{n['host']}:{n['port']}")
    return "\n".join(lines) + "\n"


# edgetunnel 入口地址池: 客户端直连 Cloudflare 的优选 IP:端口 (循环分配给每个国家节点当入口)
# 可通过环境变量 EDGE_HOSTS 覆盖 (逗号分隔)
EDGE_HOSTS = [
    h.strip()
    for h in os.environ.get(
        "EDGE_HOSTS",
        "www.5199dy.com:443,hzytjy.cn:443,ali.nonull.pp.ua:443,auto.dolby.dpdns.org:443,"
        "cdn.cnno.de:443,saas.sin.fan:443,cf.1o.ee:443",
    ).split(",")
    if h.strip()
]

HOSTS_URL = _env("HOSTS_URL", f"{PAGES_BASE}/hosts.txt")


def build_hosts_text(data):
    """生成可直接粘贴到 edgetunnel 后台「自定义优选IP」框的清单。
    每行 = 入口地址#名字$sstp://... ; 名字固定, 底下 SSTP 节点随 REFRESH_MINUTES 自动换。"""
    countries = data["countries"]
    # 入口: 优选域名循环分配; 可用 HOSTS_ENTRY 覆盖(逗号分隔), 用 EDGE_HOSTS 环境变量覆盖整张默认表
    _entry = os.environ.get("HOSTS_ENTRY", "").strip()
    edge = [e.strip() for e in _entry.split(",") if e.strip()] or EDGE_HOSTS or [f"{EDT_DOMAIN}:443"]
    lines = [
        "# edgetunnel「自定义优选IP」清单 (仅住宅, 整段复制, 追加到后台现有内容后面)",
        f"# 自动更新: {data['generated_at']} ({REFRESH_LABEL})",
        f"# 固定地址: {HOSTS_URL}",
        "# 每行 = 入口地址#名字$sstp://vpn:vpn@节点:端口",
        "# 入口用 7 个实测可用优选域名循环分配",
        "# 名字 = 国家-住宅-编号 (仅住宅; 非住宅需求请用 ed 其他节点补)",
        f"# 名字固定; 只有 $sstp:// 后面的节点地址{REFRESH_CHANGE_LABEL}",
        "# 账号密码固定 vpn:vpn ; 节点端口必须保留",
        "# ========================================================",
    ]
    idx = 0
    ordered = sorted(
        countries.items(),
        key=lambda kv: (-int(kv[1].get("count") or 0), str(kv[1].get("code") or kv[0])),
    )
    for cname, grp in ordered:
        code = str(grp.get("code") or "?").upper()
        zh = COUNTRY_ZH.get(code) or (code if code and code != "?" else cname)
        nodes = sorted(
            grp["nodes"],
            key=lambda n: (
                n.get("latency_ms") is None,
                n.get("latency_ms") or 0,
                n.get("host") or "",
            ),
        )
        lines.append("")
        lines.append(
            f"# ---- {zh} {code} · {grp['count']} 住宅节点 ----"
        )
        for i, n in enumerate(nodes, 1):
            entry = edge[idx % len(edge)]
            idx += 1
            lines.append(f"{entry}#{zh}-住宅-{i:02d}$sstp://vpn:vpn@{n['host']}:{n['port']}")
    return "\n".join(lines) + "\n"


# edgetunnel 完整订阅 (vless://) 配置
# EDT_UUID 不写死在源码里 (公开仓库会泄露 UUID, 别人拿到即可白嫖你的 edgetunnel):
# 只从环境变量读取 —— CI 用仓库 Secret EDT_UUID 注入, 本地用环境变量; 为空时不生成 sub.txt
EDT_UUID = _env("EDT_UUID", "").lower()
EDT_DOMAIN = _env("EDT_DOMAIN", "mm-66t.pages.dev")
EDT_FINGERPRINT = _env("EDT_FINGERPRINT", "chrome")
SUB_URL = _env("SUB_URL", f"{PAGES_BASE}/sub.txt")


def _b64_secret_encode(plaintext, secret):
    """复刻 edgetunnel 的 base64SecretEncode: UTF-8 循环密钥 XOR + 标准 base64。"""
    data = plaintext.encode("utf-8")
    key = secret.encode("utf-8")
    mixed = bytes(data[i] ^ key[i % len(key)] for i in range(len(data)))
    return base64.b64encode(mixed).decode("ascii")


def _socks5_account(address, default_port=80):
    """复刻 edgetunnel 的 获取SOCKS5账号: user:pass@host:port -> {username,password,hostname,port}。"""
    address = re.sub(r"^(socks5|http|https|turn|sstp)://", "", address.strip(), flags=re.I).split("#")[0].strip()
    at = address.rfind("@")
    auth, hostpart = (address[:at], address[at + 1:]) if at != -1 else ("", address)
    hostpart = hostpart.split("/")[0]
    username = password = None
    if auth:
        if ":" not in auth:
            try:
                auth = base64.b64decode(auth + "=" * (-len(auth) % 4)).decode("utf-8")
            except Exception:
                pass
        parts = auth.split(":", 1)
        username = parts[0]
        password = parts[1] if len(parts) > 1 else None
    hostname, port = hostpart, default_port
    if hostpart.count(":") == 1 and not hostpart.startswith("["):
        h, p = hostpart.rsplit(":", 1)
        if p.isdigit():
            hostname, port = h, int(p)
    return {"username": username, "password": password, "hostname": hostname, "port": port}


def build_sub_text(data):
    """生成 edgetunnel 完整 vless:// 订阅 (链式代理编码在 path)。
    填进 edgetunnel 后台「订阅链接」URL, 客户端定时拉取即可自动轮换。"""
    if not EDT_UUID:
        die("EDT_UUID 未设置, 无法生成 sub.txt (订阅含 UUID, 请通过环境变量/Secret 注入)")
    countries = data["countries"]
    lines = [
        "# edgetunnel 完整订阅 (vless://) —— 填进后台「订阅链接」URL",
        f"# 自动更新: {data['generated_at']} ({REFRESH_LABEL})",
        f"# 固定地址: {SUB_URL}",
        f"# 节点域名: {EDT_DOMAIN} (传输 ws / TLS / fingerprint {EDT_FINGERPRINT})",
        f"# 名字固定; $sstp:// 链式代理(编码在 path){REFRESH_CHANGE_LABEL}",
        "# 账号密码固定 vpn:vpn ; 节点端口已编码进 path",
        "# ========================================================",
    ]
    ordered = sorted(
        countries.items(),
        key=lambda kv: (-int(kv[1].get("count") or 0), str(kv[1].get("code") or kv[0])),
    )
    for cname, grp in ordered:
        code = str(grp.get("code") or "?").upper()
        zh = COUNTRY_ZH.get(code) or (code if code and code != "?" else cname)
        nodes = sorted(
            grp["nodes"],
            key=lambda n: (
                n.get("latency_ms") is None,
                n.get("latency_ms") or 0,
                n.get("host") or "",
            ),
        )
        for i, n in enumerate(nodes, 1):
            name = f"{zh}-住宅-{i:02d}"
            chain = {"type": "sstp", **_socks5_account(f"vpn:vpn@{n['host']}:{n['port']}", 443)}
            chain_json = json.dumps(chain, separators=(",", ":"))
            enc = _b64_secret_encode(chain_json, EDT_UUID)
            path = quote("/video/" + enc, safe="")
            link = (
                f"vless://{EDT_UUID}@{EDT_DOMAIN}:443?security=tls&type=ws"
                f"&host={EDT_DOMAIN}&fp={EDT_FINGERPRINT}&sni={EDT_DOMAIN}"
                f"&path={path}&encryption=none&alpn=#{quote(name, safe='')}"
            )
            lines.append(link)
    return "\n".join(lines) + "\n"


def write_outputs(data):
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    data_path = os.path.join(PUBLIC_DIR, "data.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    # 固定网页: 始终用 web/index.html 模板生成同一个 index.html (数据来自 data.json)
    html_path = os.path.join(PUBLIC_DIR, "index.html")
    if os.path.exists(TEMPLATE_HTML):
        with open(TEMPLATE_HTML, "r", encoding="utf-8") as f:
            html = f.read()
    else:
        html = ("<html><head><meta charset='utf-8'><title>VPN Gate SSTP 节点</title></head>"
                "<body><h1>VPN Gate SSTP 节点</h1><pre id='out'></pre></body>"
                "<script>fetch('data.json').then(r=>r.json()).then(d=>out.textContent=JSON.stringify(d.stats)).catch(e=>out.textContent='加载失败:'+e)</script></html>")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # edgetunnel 链式代理清单 (固定 URL, 方案一: 名字不变、指令自动换)
    chains_path = os.path.join(PUBLIC_DIR, "chains.txt")
    with open(chains_path, "w", encoding="utf-8") as f:
        f.write(build_chains_text(data))

    # 可直接粘贴进后台「自定义优选IP」框的清单 (入口地址#名字$sstp://...)
    hosts_path = os.path.join(PUBLIC_DIR, "hosts.txt")
    with open(hosts_path, "w", encoding="utf-8") as f:
        f.write(build_hosts_text(data))

    # 完整 vless:// 订阅: 含 UUID, 默认不发布到公开 Pages (泄露 UUID = 别人可白嫖你的 edgetunnel)
    # 需要时设 PUBLISH_SUB=1 且提供 EDT_UUID (CI 走仓库 Secret); 用 edgetunnel 后台自带的订阅地址则无需开启
    sub_path = None
    stale_sub = os.path.join(PUBLIC_DIR, "sub.txt")
    if os.environ.get("PUBLISH_SUB", "").strip().lower() in ("1", "true", "yes") and EDT_UUID:
        sub_path = stale_sub
        with open(sub_path, "w", encoding="utf-8") as f:
            f.write(build_sub_text(data))
    else:
        log("SUB", "跳过 sub.txt: 默认不公开发布 (需要时设 PUBLISH_SUB=1 + 环境变量 EDT_UUID)")
        if os.path.exists(stale_sub):
            os.remove(stale_sub)
            log("SUB", "已清理上一版残留的公开 sub.txt")
    return data_path, html_path, chains_path, hosts_path, sub_path


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    session = requests.Session()

    # 1) 数据源
    rows, source = fetch_vpngate()
    raw_count = len(rows)
    if raw_count == 0:
        die("VPN Gate 返回 0 个原始节点 (数据源异常, 不允许生成空结果)")

    # 2) SSTP 筛选 + 去重
    sstp_nodes = to_sstp_nodes(rows)
    sstp_count = len(sstp_nodes)
    if sstp_count == 0:
        die(f"从 {raw_count} 个原始节点中没有解析出任何 SSTP(TCP) 节点 — 数据格式可能已变化, 需要人工适配")
    uniq = dedupe(sstp_nodes)

    if MAX_CHECK_NODES > 0:
        uniq = uniq[:MAX_CHECK_NODES]

    log("VPN GATE", f"获取原始节点: {raw_count}")
    log("VPN GATE", f"SSTP 节点: {sstp_count}")
    log("VPN GATE", f"去重后: {len(uniq)}")

    # 3) 并发检测
    log("CLOUDFLARE WORKER", f"提交检测: {len(uniq)} (并发 {CONCURRENCY}, 单请求超时 {CHECK_TIMEOUT}s)")
    t0 = time.time()
    results = check_all(uniq, session)
    elapsed = time.time() - t0

    success = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    worker_errors = [r for r in failed if r.get("worker_error")]

    log("CLOUDFLARE WORKER", f"检测成功: {len(success)}")
    log("CLOUDFLARE WORKER", f"检测失败: {len(failed)}" + (f" (其中 Worker 异常 {len(worker_errors)})" if worker_errors else ""))
    log("CLOUDFLARE WORKER", f"耗时: {elapsed:.1f}s")

    # 硬性失败: Worker 完全不可达 (没有任何一个请求拿到正常响应)
    if uniq and not success and len(worker_errors) == len(uniq):
        # 429 = Cloudflare Error 1027: 免费版 10 万请求/UTC 日 额度用尽。
        # 该额度是【账号级共享】的 —— 同账号所有 Worker 一起被顶掉,
        # 00:00 UTC 重置后自动恢复 (所以表现为每天固定时段突然全挂)。
        if all(r.get("error") == "HTTP 429" for r in worker_errors):
            die(
                "检测 Worker 全部返回 429 = Cloudflare Error 1027: "
                "Workers 免费版当天请求数已达 100,000 (账号级共享, 同账号其它 Worker 也会一起挂), "
                "00:00 UTC 会自动重置。"
                "排查: Cloudflare Dashboard → Workers & Pages → Metrics 看当天请求量, "
                "找出消耗大户 (爬虫/宽路由/Pages Functions/subrequest 扇出); "
                "根治: 升级 Workers Paid, 或把检测 Worker 拆到另一个 CF 账号。"
                " 本次运行判定失败 (不生成空结果)"
            )
        die("Worker 全部请求异常, 检测服务不可用 — 本次运行判定失败 (不生成空结果)")

    # 4) 结果 + 网页
    data = build_outputs(results, raw_count, sstp_count, source)
    log("RESULT", f"可用节点: {len(success)} (其中住宅 {data['stats']['success']}, 已过滤非住宅 {data['stats']['dropped_non_residential']})")
    log("RESULT", f"国家数量: {data['stats']['countries']}")

    data_path, html_path, chains_path, hosts_path, sub_path = write_outputs(data)
    log("WEBSITE", f"生成 {os.path.relpath(data_path, REPO_DIR)}")
    log("WEBSITE", f"生成 {os.path.relpath(html_path, REPO_DIR)}")
    log("WEBSITE", f"生成 {os.path.relpath(chains_path, REPO_DIR)}")
    log("WEBSITE", f"生成 {os.path.relpath(hosts_path, REPO_DIR)}")
    if sub_path is not None:
        log("WEBSITE", f"生成 {os.path.relpath(sub_path, REPO_DIR)}")
    log("WEBSITE", "完成 (GitHub Pages 部署由 workflow 执行)")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        die(f"程序异常: {type(exc).__name__}: {exc}")
