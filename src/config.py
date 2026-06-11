from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv


@dataclass
class ProviderConfig:
    type: str = "aliyun"
    access_key_id: str = ""
    access_key_secret: str = ""
    region: str = "ap-northeast-1"
    plans: dict = field(default_factory=dict)
    default_plan: str = ""


@dataclass
class DomainEntry:
    name: str
    proxied: bool


@dataclass
class DNSConfig:
    type: str = "cloudflare"
    api_token: str = ""
    zone_id: str = ""
    account_id: str = ""
    ttl: int = 60
    domains: List[DomainEntry] = field(default_factory=list)


# ─── 全局覆盖参数结构 ────────────────────────────────────────────────

@dataclass
class GlobalSSHConfig:
    user: str = "root"
    port: int = 22
    password: str = ""


@dataclass
class GlobalSingboxConfig:
    proxy_username: str = ""
    proxy_password: str = ""
    uuid: str = ""


# ─── Init Pipeline 相关的子配置 ─────────────────────────────────────

@dataclass
class RealityConfig:
    server_name: str = ""
    private_key: str = ""
    public_key: str = ""


@dataclass
class WarpConfig:
    enabled: bool = False
    private_key: str = ""
    address: List[str] = field(default_factory=list)


@dataclass
class UpstreamSocksConfig:
    enabled: bool = False
    server: str = ""
    port: int = 1080


@dataclass
class SingboxConfig:
    proxy_username: str = ""
    proxy_password: str = ""
    uuid: str = ""
    reality: RealityConfig = field(default_factory=RealityConfig)
    warp: WarpConfig = field(default_factory=WarpConfig)
    upstream_socks: UpstreamSocksConfig = field(default_factory=UpstreamSocksConfig)


@dataclass
class ServerInitConfig:
    hostname: str = ""
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_password: str = ""
    sync_from_source: bool = False
    source_server_ip: str = ""
    swap_size_mb: int = 1024
    acme_email: str = ""
    cert_domain: str = ""


@dataclass
class SubscribeConfig:
    filename: str = "sub.txt"
    remote_dir: str = "/usr/local/openresty/nginx/html/sub"
    node_prefix: str = "node"


@dataclass
class RetryConfig:
    instance_start_timeout: int = 300
    instance_poll_interval: int = 15
    ssh_connect_timeout: int = 120
    ssh_connect_interval: int = 10


@dataclass
class InitPipelineConfig:
    image_id: str = ""
    plan: str = ""
    domains: List[DomainEntry] = field(default_factory=list)
    firewall_ports: List[str] = field(default_factory=list)
    singbox: SingboxConfig = field(default_factory=SingboxConfig)
    server_init: ServerInitConfig = field(default_factory=ServerInitConfig)
    subscribe: SubscribeConfig = field(default_factory=SubscribeConfig)
    state_file: str = ".proxy_init_state.json"
    retry: RetryConfig = field(default_factory=RetryConfig)
    region: str = ""


# ─── Rotation Pipeline 相关的子配置 ─────────────────────────────────

@dataclass
class RotationPipelineConfig:
    resource_id: str = ""
    plan: str = ""
    domains: List[DomainEntry] = field(default_factory=list)
    firewall_ports: List[str] = field(default_factory=list)
    snapshot_name: str = "rotation-snapshot"
    poll_interval: int = 15
    poll_timeout: int = 600
    state_file: str = "rotation_state.json"
    region: str = ""


# ─── 统一集成的 AppConfig ──────────────────────────────────────────

@dataclass
class AppConfig:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    dns: DNSConfig = field(default_factory=DNSConfig)
    init: InitPipelineConfig = field(default_factory=InitPipelineConfig)
    rotation: RotationPipelineConfig = field(default_factory=RotationPipelineConfig)
    ssh: GlobalSSHConfig = field(default_factory=GlobalSSHConfig)
    singbox: GlobalSingboxConfig = field(default_factory=GlobalSingboxConfig)
    acme_email: str = ""
    image_id: str = ""
    resource_id: str = ""
    firewall_ports: List[str] = field(default_factory=list)
    state_file: str = ""


# ─── 配置加载辅助函数 ───────────────────────────────────────────────

def _normalize_port(entry) -> str:
    """标准化端口/端口范围格式，支持 22, '22', '4000/4006', '4000-4006'"""
    s = str(entry).strip()
    if "-" in s and not s.startswith("-"):
        parts = s.split("-", 1)
        start, end = parts[0].strip(), parts[1].strip()
        if not (start.isdigit() and end.isdigit()):
            raise ValueError(f"无效的端口范围: {entry!r}")
        if int(start) > int(end):
            raise ValueError(f"端口范围起始大于结束: {entry!r}")
        return f"{start}/{end}"
    if "/" in s:
        parts = s.split("/", 1)
        start, end = parts[0].strip(), parts[1].strip()
        if not (start.isdigit() and end.isdigit()):
            raise ValueError(f"无效的端口范围: {entry!r}")
        if int(start) > int(end):
            raise ValueError(f"端口范围起始大于结束: {entry!r}")
        return f"{start}/{end}"
    if not s.isdigit():
        raise ValueError(f"无效的端口格式: {entry!r}")
    return s


def _parse_domains(raw_domains: list, global_proxied: bool) -> List[DomainEntry]:
    """统一解析域名配置列表的结构"""
    parsed_domains = []
    for d in raw_domains:
        if isinstance(d, dict):
            parsed_domains.append(
                DomainEntry(
                    name=str(d.get("name", d.get("domain", ""))),
                    proxied=bool(d.get("proxied", global_proxied)),
                )
            )
        else:
            parsed_domains.append(
                DomainEntry(
                    name=str(d),
                    proxied=global_proxied,
                )
            )
    return parsed_domains


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """
    加载配置文件，并通过环境变量进行重写以维护敏感凭据的安全。
    支持向后兼容旧版云商与子命令关联的套餐结构配置，以及多层级域名配置解析与全局/局部参数覆盖合并。
    """
    load_dotenv()

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件未找到: {config_path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # 1. 解析全局顶级配置节点
    global_ssh_raw = raw.get("ssh", {})
    global_ssh = GlobalSSHConfig(
        user=global_ssh_raw.get("user", "root") if global_ssh_raw else "root",
        port=int(global_ssh_raw.get("port", 22)) if global_ssh_raw and global_ssh_raw.get("port") else 22,
        password=os.environ.get("SERVER_SSH_PASSWORD", global_ssh_raw.get("password", "") if global_ssh_raw else ""),
    )

    global_sb_raw = raw.get("singbox", {})
    global_sb = GlobalSingboxConfig(
        proxy_username=global_sb_raw.get("proxy_username", "") if global_sb_raw else "",
        proxy_password=global_sb_raw.get("proxy_password", "") if global_sb_raw else "",
        uuid=global_sb_raw.get("uuid", "") if global_sb_raw else "",
    )

    global_acme_email = raw.get("acme_email", "")
    global_image_id = raw.get("image_id", "")
    global_resource_id = raw.get("resource_id", "")
    global_fw_ports = [_normalize_port(p) for p in raw.get("firewall_ports", [])]
    global_state_file = raw.get("state_file", "")

    # 2. 解析 Provider 配置
    provider_raw = raw.get("provider", {})
    if not provider_raw and "aliyun" in raw:
        provider_raw = raw["aliyun"]

    provider_type = provider_raw.get("type", "aliyun")
    
    # 专属节点（如 provider.aliyun）
    plans = {}
    default_plan = ""
    if provider_type in provider_raw:
        provider_specific = provider_raw[provider_type]
        if isinstance(provider_specific, dict):
            plans = provider_specific.get("plans", {})
            default_plan = provider_specific.get("default_plan", "")
    else:
        plans = provider_raw.get("plans", {})
        default_plan = provider_raw.get("default_plan", "")

    plans = dict(plans)

    provider_cfg = ProviderConfig(
        type=provider_type,
        access_key_id=os.environ.get("ALIYUN_ACCESS_KEY_ID", provider_raw.get("access_key_id", "")),
        access_key_secret=os.environ.get("ALIYUN_ACCESS_KEY_SECRET", provider_raw.get("access_key_secret", "")),
        region=os.environ.get("ALIYUN_REGION", provider_raw.get("region", "ap-northeast-1")),
        plans=plans,
        default_plan=default_plan,
    )

    # 3. 解析 DNS 配置
    dns_raw = raw.get("dns", {})
    if not dns_raw and "cloudflare" in raw:
        dns_raw = raw["cloudflare"]

    dns_type = dns_raw.get("type", "cloudflare")
    global_proxied = dns_raw.get("proxied", False)
    dns_domains = _parse_domains(dns_raw.get("domains", []), global_proxied)

    dns_cfg = DNSConfig(
        type=dns_type,
        api_token=os.environ.get("CF_API_TOKEN", dns_raw.get("api_token", "")),
        zone_id=os.environ.get("CF_ZONE_ID", dns_raw.get("zone_id", "")),
        account_id=os.environ.get("CF_ACCOUNT_ID", dns_raw.get("account_id", "")),
        ttl=int(dns_raw.get("ttl", 60)),
        domains=dns_domains,
    )

    # 4. 校验与提取 Init 管道配置
    init_raw = raw.get("init", {})
    if not init_raw:
        init_raw = raw

    # 提取 server 属性 (可能含有 image_id，但也可能有旧的 plan_id 等参数)
    srv_raw = init_raw.get("server", {})
    if not srv_raw and "aliyun" in raw and "server" in raw["aliyun"]:
        srv_raw = raw["aliyun"]["server"]
    
    init_image_id = init_raw.get("image_id", srv_raw.get("image_id", ""))
    if not init_image_id:
        init_image_id = global_image_id

    init_plan = init_raw.get("plan", "")

    # 向后兼容：如果在 init.server 中定义了 plan_id，动态创建 legacy_init 套餐
    if "plan_id" in srv_raw and srv_raw["plan_id"]:
        legacy_plan_name = "legacy_init"
        provider_cfg.plans[legacy_plan_name] = {
            "plan_id": srv_raw["plan_id"],
            "period": int(srv_raw.get("period", 1)),
            "charge_type": srv_raw.get("charge_type", "PrePaid"),
            "auto_renew": bool(srv_raw.get("auto_renew", False)),
        }
        init_plan = legacy_plan_name

    # 解析 init 命令专属的域名
    init_domains = _parse_domains(init_raw.get("domains", []), global_proxied)

    # 提取防火墙端口
    fw_ports_raw = init_raw.get("firewall_ports")
    if fw_ports_raw is None:
        if "aliyun" in raw and "firewall_ports" in raw["aliyun"]:
            fw_ports_raw = raw["aliyun"]["firewall_ports"]
        else:
            fw_ports_raw = global_fw_ports
    if fw_ports_raw is None:
        fw_ports = []
    else:
        fw_ports = [_normalize_port(p) for p in fw_ports_raw]

    # 提取 sing-box 配置
    sb_raw = init_raw.get("singbox", {})
    sb_reality_raw = sb_raw.get("reality", {})
    sb_warp_raw = sb_raw.get("warp", {})
    sb_us_raw = sb_raw.get("upstream_socks", {})

    sb_proxy_username = sb_raw.get("proxy_username")
    if sb_proxy_username is None:
        sb_proxy_username = global_sb.proxy_username

    sb_proxy_password = sb_raw.get("proxy_password")
    if sb_proxy_password is None:
        sb_proxy_password = global_sb.proxy_password

    sb_uuid = sb_raw.get("uuid")
    if sb_uuid is None:
        sb_uuid = global_sb.uuid

    singbox_cfg = SingboxConfig(
        proxy_username=sb_proxy_username,
        proxy_password=sb_proxy_password,
        uuid=sb_uuid,
        reality=RealityConfig(
            server_name=sb_reality_raw.get("server_name", ""),
            private_key=sb_reality_raw.get("private_key", ""),
            public_key=sb_reality_raw.get("public_key", ""),
        ),
        warp=WarpConfig(
            enabled=bool(sb_warp_raw.get("enabled", False)),
            private_key=sb_warp_raw.get("private_key", ""),
            address=list(sb_warp_raw.get("address", [])),
        ),
        upstream_socks=UpstreamSocksConfig(
            enabled=bool(sb_us_raw.get("enabled", False)),
            server=sb_us_raw.get("server", ""),
            port=int(sb_us_raw.get("port", 1080)),
        ),
    )

    # 提取 server_init并应用全局覆盖
    si_raw = init_raw.get("server_init", {})
    si_ssh_port = si_raw.get("ssh_port")
    if si_ssh_port is None:
        si_ssh_port = global_ssh.port
    else:
        si_ssh_port = int(si_ssh_port)

    si_ssh_user = si_raw.get("ssh_user")
    if si_ssh_user is None:
        si_ssh_user = global_ssh.user

    si_ssh_password = os.environ.get("SERVER_SSH_PASSWORD")
    if not si_ssh_password:
        si_ssh_password = si_raw.get("ssh_password")
    if not si_ssh_password:
        si_ssh_password = global_ssh.password

    si_acme_email = si_raw.get("acme_email")
    if not si_acme_email:
        si_acme_email = global_acme_email
    if not si_acme_email and "dns" in raw and "acme_email" in raw["dns"]:
        si_acme_email = raw["dns"]["acme_email"]

    server_init_cfg = ServerInitConfig(
        hostname=si_raw.get("hostname", ""),
        ssh_user=si_ssh_user,
        ssh_port=si_ssh_port,
        ssh_password=si_ssh_password,
        sync_from_source=bool(si_raw.get("sync_from_source", False)),
        source_server_ip=si_raw.get("source_server_ip", ""),
        swap_size_mb=int(si_raw.get("swap_size_mb", 1024)),
        acme_email=si_acme_email,
        cert_domain=si_raw.get("cert_domain", ""),
    )

    # 提取 subscribe
    sub_raw = init_raw.get("subscribe", {})
    subscribe_cfg = SubscribeConfig(
        filename=sub_raw.get("filename", "sub.txt"),
        remote_dir=sub_raw.get("remote_dir", "/usr/local/openresty/nginx/html/sub"),
        node_prefix=sub_raw.get("node_prefix", "node"),
    )

    # 提取 state_file 并向下兼容
    init_state_file = init_raw.get("state_file")
    if init_state_file is None:
        if isinstance(init_raw.get("state"), dict):
            init_state_file = init_raw["state"].get("file")
    if not init_state_file:
        init_state_file = global_state_file
    if not init_state_file:
        init_state_file = ".proxy_init_state.json"

    re_raw = init_raw.get("retry", {})
    retry_cfg = RetryConfig(
        instance_start_timeout=int(re_raw.get("instance_start_timeout", 300)),
        instance_poll_interval=int(re_raw.get("instance_poll_interval", 15)),
        ssh_connect_timeout=int(re_raw.get("ssh_connect_timeout", 120)),
        ssh_connect_interval=int(re_raw.get("ssh_connect_interval", 10)),
    )

    init_pipeline_cfg = InitPipelineConfig(
        image_id=init_image_id,
        plan=init_plan,
        domains=init_domains,
        firewall_ports=fw_ports,
        singbox=singbox_cfg,
        server_init=server_init_cfg,
        subscribe=subscribe_cfg,
        state_file=init_state_file,
        retry=retry_cfg,
        region=init_raw.get("region", ""),
    )

    # 5. 提取 Rotation 管道配置并应用向下兼容和全局覆盖
    rot_raw = raw.get("rotation", {})
    if not rot_raw:
        rot_raw = raw

    rot_plan = rot_raw.get("plan", "")
    rot_srv_raw = rot_raw.get("server_spec", {})
    
    # 向后兼容：如果在 rotation.server_spec 中定义了 plan_id，动态创建 legacy_rotation 套餐
    if "plan_id" in rot_srv_raw and rot_srv_raw["plan_id"]:
        legacy_plan_name = "legacy_rotation"
        provider_cfg.plans[legacy_plan_name] = {
            "plan_id": rot_srv_raw["plan_id"],
            "period": int(rot_srv_raw.get("period", 1)),
            "charge_type": rot_srv_raw.get("charge_type", "PrePaid"),
            "auto_renew": bool(rot_srv_raw.get("auto_renew", False)),
        }
        rot_plan = legacy_plan_name

    # 解析 rotation 命令专属的域名
    rot_domains = _parse_domains(rot_raw.get("domains", []), global_proxied)

    # 提取端口防火墙配置
    rot_ports_raw = rot_raw.get("firewall_ports")
    if rot_ports_raw is None:
        rot_ports_raw = rot_raw.get("ports")
    if rot_ports_raw is None:
        rot_ports_raw = global_fw_ports
    if rot_ports_raw is None:
        rot_ports = []
    else:
        rot_ports = [_normalize_port(p) for p in rot_ports_raw]

    # 生成唯一的 snapshot 名字
    snapshot_base = rot_raw.get("snapshot_name", "rotation-snapshot")
    snapshot_name = f"{snapshot_base}-{uuid_suffix()}"

    res_id = os.environ.get("RESOURCE_ID")
    if not res_id:
        res_id = rot_raw.get("resource_id")
    if not res_id:
        res_id = global_resource_id

    rot_state_file = rot_raw.get("state_file")
    if not rot_state_file:
        rot_state_file = global_state_file
    if not rot_state_file:
        rot_state_file = str(path.parent / "rotation_state.json")

    rotation_pipeline_cfg = RotationPipelineConfig(
        resource_id=res_id,
        plan=rot_plan,
        domains=rot_domains,
        firewall_ports=rot_ports,
        snapshot_name=snapshot_name,
        poll_interval=int(rot_raw.get("poll_interval", 15)),
        poll_timeout=int(rot_raw.get("poll_timeout", 600)),
        state_file=rot_state_file,
        region=rot_raw.get("region", ""),
    )

    return AppConfig(
        provider=provider_cfg,
        dns=dns_cfg,
        init=init_pipeline_cfg,
        rotation=rotation_pipeline_cfg,
        ssh=global_ssh,
        singbox=global_sb,
        acme_email=global_acme_email,
        image_id=global_image_id,
        resource_id=global_resource_id,
        firewall_ports=global_fw_ports,
        state_file=global_state_file,
    )


def uuid_suffix() -> str:
    import uuid
    return uuid.uuid4().hex[:6]
