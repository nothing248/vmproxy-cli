from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler

from src.config import load_config, DomainEntry
from src.state import InitStateManager
from src.providers.aliyun import AliyunProvider
from src.providers.cloudflare import CloudflareDNSProvider
from src.orchestrator.init_pipeline import InitPipeline
from src.orchestrator.rotation_pipeline import RotationPipeline

# 初始化 CLI 主程序
app = typer.Typer(
    name="vmproxy",
    help="🚀 统一的代理主机初始化与自动化轮转 CLI 工具",
    rich_markup_mode="rich",
)
console = Console()


def setup_logging(verbose: bool = False) -> None:
    """初始化高颜值终端日志系统 (使用 RichHandler)"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )
    # 降低第三方吵闹包的日志等级
    for noisy in ("paramiko", "urllib3", "alibabacloud"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ─── 统一构建与分发依赖 ──────────────────────────────────────────────

def build_orchestrators(
    config_path: str,
    cmd_region: Optional[str] = None,
    command_name: Optional[str] = None,
):
    """根据解析的配置实例化对应的云厂商、DNS服务商"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        console.print(f"[bold red]❌ 载入配置文件 {config_path} 失败:[/bold red] {e}")
        raise typer.Exit(code=1)

    # 动态确定 Region 优先级：命令行参数 > 子配置 (init.region 或 rotation.region) > 全局默认 (provider.region)
    final_region = cmd_region
    if not final_region:
        if command_name == "init":
            final_region = cfg.init.region
        elif command_name == "rotate":
            final_region = cfg.rotation.region
    if not final_region:
        final_region = cfg.provider.region

    # 将最终确定的 Region 写回
    cfg.provider.region = final_region
    if command_name == "init":
        cfg.init.region = final_region
    elif command_name == "rotate":
        cfg.rotation.region = final_region

    # 1. 验证敏感凭证的存在
    errors = []
    if not cfg.provider.access_key_id:
        errors.append("缺少云服务商 Access Key ID (可通过 ALIYUN_ACCESS_KEY_ID 环境变量或配置文件注入)")
    if not cfg.provider.access_key_secret:
        errors.append("缺少云服务商 Access Key Secret (可通过 ALIYUN_ACCESS_KEY_SECRET 环境变量或配置文件注入)")
    if not cfg.dns.api_token:
        errors.append("缺少 DNS 服务商 API Token (可通过 CF_API_TOKEN 环境变量或配置文件注入)")
    if not cfg.dns.zone_id:
        errors.append("缺少 DNS 服务商 Zone ID (可通过 CF_ZONE_ID 环境变量或配置文件注入)")
    
    if errors:
        console.print("\n[bold red]❌ 凭证缺失错误:[/bold red]")
        for err in errors:
            console.print(f"  • {err}", style="red")
        raise typer.Exit(code=1)

    # 2. 动态加载 Provider
    if cfg.provider.type == "aliyun":
        provider = AliyunProvider(
            access_key_id=cfg.provider.access_key_id,
            access_key_secret=cfg.provider.access_key_secret,
            region=final_region,
            plans=cfg.provider.plans,
            default_plan=cfg.provider.default_plan,
        )
    else:
        console.print(f"[bold red]❌ 不支持的云服务提供商类型:[/bold red] {cfg.provider.type}", style="red")
        raise typer.Exit(code=1)

    # 3. 动态加载 DNS
    if cfg.dns.type == "cloudflare":
        dns_provider = CloudflareDNSProvider(
            api_token=cfg.dns.api_token,
            zone_id=cfg.dns.zone_id,
            domains=cfg.dns.domains,
            ttl=cfg.dns.ttl,
        )
    else:
        console.print(f"[bold red]❌ 不支持的 DNS 提供商类型:[/bold red] {cfg.dns.type}", style="red")
        raise typer.Exit(code=1)

    return cfg, provider, dns_provider


# ─── 全局回调选项 ──────────────────────────────────────────────────

@app.callback()
def main(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="开启调试级别 (DEBUG) 日志输出",
    ),
):
    """
    统一的 VM 代理运维 CLI。
    支持快速从零建站初始化环境，以及基于快照/镜像的快速实例轮转。
    """
    setup_logging(verbose)


# ─── CLI 命令注册 ──────────────────────────────────────────────────

@app.command("init")
def cmd_init(
    config: str = typer.Option(
        "config.yaml",
        "--config",
        "-c",
        help="配置文件路径",
    ),
    plan: str = typer.Option(
        "",
        "--plan",
        "-p",
        help="指定或覆盖要使用的云主机套餐别名",
    ),
    domains: str = typer.Option(
        "",
        "--domains",
        "-d",
        help="指定绑定的域名（多域名用逗号分隔，如 domain1.com,domain2.com）",
    ),
    region: str = typer.Option(
        "",
        "--region",
        "-r",
        help="覆盖云服务器物理地域 (如 ap-northeast-1)",
    ),
    image_id: str = typer.Option(
        "",
        "--image-id",
        "-i",
        help="覆盖云服务器系统基础镜像 ID",
    ),
    firewall_ports: str = typer.Option(
        "",
        "--firewall-ports",
        help="覆盖放行的防火墙端口列表 (以逗号分隔，例如 22,80,443,4000/4006)",
    ),
    state_file: str = typer.Option(
        "",
        "--state-file",
        help="覆盖状态断点文件路径",
    ),
    proxy_username: str = typer.Option(
        "",
        "--proxy-username",
        help="覆盖代理登录账号",
    ),
    proxy_password: str = typer.Option(
        "",
        "--proxy-password",
        help="覆盖代理登录密码",
    ),
    proxy_uuid: str = typer.Option(
        "",
        "--proxy-uuid",
        help="覆盖代理登录 UUID",
    ),
    ssh_port: Optional[int] = typer.Option(
        None,
        "--ssh-port",
        help="覆盖 SSH 登录端口",
    ),
    ssh_user: str = typer.Option(
        "",
        "--ssh-user",
        help="覆盖 SSH 登录用户名",
    ),
    ssh_password: str = typer.Option(
        "",
        "--ssh-password",
        help="覆盖 SSH 登录密码",
    ),
    acme_email: str = typer.Option(
        "",
        "--acme-email",
        help="覆盖 acme.sh 证书注册邮箱",
    ),
    node_prefix: str = typer.Option(
        "",
        "--node-prefix",
        help="覆盖订阅节点名称前缀",
    ),
    reset_state: bool = typer.Option(
        False,
        "--reset-state",
        help="清除历史断点状态文件，从第一步重新开始",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="仅生成并预览本地模板，不发出任何云 API 请求",
    ),
):
    """
    一键初始化代理主机环境。
    流程：购买轻量主机 ➜ 防火墙开通 ➜ CF DNS 绑定 ➜ 远端系统优化 ➜ 证书申请 ➜ sing-box 与订阅部署。
    """
    cfg, provider, dns_provider = build_orchestrators(config, cmd_region=region, command_name="init")

    # 命令行覆盖并更新 cfg 对应的参数
    if image_id:
        cfg.init.image_id = image_id
    if state_file:
        cfg.init.state_file = state_file
    if firewall_ports:
        ports_list = [p.strip() for p in firewall_ports.split(",") if p.strip()]
        from src.config import _normalize_port
        try:
            cfg.init.firewall_ports = [_normalize_port(p) for p in ports_list]
        except Exception as exc:
            console.print(f"[bold red]❌ 端口格式校验失败:[/bold red] {exc}", style="red")
            raise typer.Exit(code=1)

    if proxy_username:
        cfg.init.singbox.proxy_username = proxy_username
    if proxy_password:
        cfg.init.singbox.proxy_password = proxy_password
    if proxy_uuid:
        cfg.init.singbox.uuid = proxy_uuid

    if ssh_port is not None:
        cfg.init.server_init.ssh_port = ssh_port
    if ssh_user:
        cfg.init.server_init.ssh_user = ssh_user
    if ssh_password:
        cfg.init.server_init.ssh_password = ssh_password
    if acme_email:
        cfg.init.server_init.acme_email = acme_email
    if node_prefix:
        cfg.init.subscribe.node_prefix = node_prefix

    # 提前校验所选套餐是否合法
    chosen_plan = plan or cfg.init.plan
    try:
        provider.get_plan(chosen_plan)
    except Exception as exc:
        console.print(f"[bold red]❌ 套餐校验失败:[/bold red] {exc}", style="red")
        raise typer.Exit(code=1)

    # 解析并合并域名优先级
    global_proxied = cfg.dns.domains[0].proxied if cfg.dns.domains else False
    if domains:
        final_domains = []
        for name in domains.split(","):
            name = name.strip()
            if name:
                final_domains.append(DomainEntry(name=name, proxied=global_proxied))
    else:
        final_domains = cfg.init.domains if cfg.init.domains else cfg.dns.domains

    # 检验域名是否存在
    if not final_domains:
        console.print("[bold red]❌ 错误:[/bold red] 必须在配置文件中指定域名绑定，或通过 -d/--domains 命令行参数指定！", style="red")
        raise typer.Exit(code=1)

    # 动态将最终生效的域名列表写回
    cfg.dns.domains = final_domains
    dns_provider.domains = final_domains

    # 自动将第一个生效域名设置为 cert_domain（若未配置）
    if not cfg.init.server_init.cert_domain:
        cfg.init.server_init.cert_domain = final_domains[0].name

    state_mgr = InitStateManager(cfg.init.state_file)

    if reset_state:
        state_mgr.reset()

    pipeline = InitPipeline(
        cfg=cfg,
        provider=provider,
        dns_provider=dns_provider,
        state=state_mgr,
    )
    pipeline.run(plan_name=plan, dry_run=dry_run)


@app.command("teardown")
def cmd_teardown(
    instance_id: str = typer.Option(
        ...,
        "--instance-id",
        "-i",
        help="要释放/销毁的轻量实例 ID (格式如 c574f5afcc4d484a82ba1be03519360b)",
    ),
    config: str = typer.Option(
        "config.yaml",
        "--config",
        "-c",
        help="配置文件路径 (用于读取域名等信息以进行 DNS 清理)",
    ),
    skip_dns: bool = typer.Option(
        False,
        "--skip-dns",
        help="保留 DNS 解析，不进行域名解析的清理",
    ),
    skip_refund: bool = typer.Option(
        False,
        "--skip-refund",
        help="跳过实例退款释放流程 (仅清理 DNS 记录)",
    ),
):
    """
    退订代理主机并清理绑定的 DNS 解析。
    """
    cfg, provider, dns_provider = build_orchestrators(config)
    state_mgr = InitStateManager(cfg.init.state_file)

    pipeline = InitPipeline(
        cfg=cfg,
        provider=provider,
        dns_provider=dns_provider,
        state=state_mgr,
    )
    pipeline.teardown(
        instance_id=instance_id,
        skip_dns=skip_dns,
        skip_refund=skip_refund,
    )


@app.command("rotate")
def cmd_rotate(
    config: str = typer.Option(
        "config.yaml",
        "--config",
        "-c",
        help="配置文件路径",
    ),
    resource_id: Optional[str] = typer.Option(
        None,
        "--resource-id",
        "-i",
        help="指定/覆盖要被轮换的源资源 ID (实例 ID 或镜像 ID)。若为空则默认读取配置文件中 resource_id",
    ),
    plan: str = typer.Option(
        "",
        "--plan",
        "-p",
        help="指定或覆盖轮换生成的新云主机套餐别名",
    ),
    domains: str = typer.Option(
        "",
        "--domains",
        "-d",
        help="指定绑定的域名（多域名用逗号分隔，如 domain1.com,domain2.com）",
    ),
    region: str = typer.Option(
        "",
        "--region",
        "-r",
        help="覆盖云服务器物理地域 (如 ap-northeast-1)",
    ),
    firewall_ports: str = typer.Option(
        "",
        "--firewall-ports",
        help="覆盖放行的防火墙端口列表 (以逗号分隔，例如 22,80,443)",
    ),
    state_file: str = typer.Option(
        "",
        "--state-file",
        help="覆盖状态文件路径",
    ),
    skip_dns: bool = typer.Option(
        False,
        "--skip-dns",
        help="跳过 DNS A 记录绑定切换步骤",
    ),
    reset_state: bool = typer.Option(
        False,
        "--reset-state",
        help="清除历史断点状态文件，从第一步重新开始",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="校验配置并打印轮换计划，不触发 API 请求",
    ),
):
    """
    自动化轮转旧代理主机。
    流程：对旧主机打快照镜像 ➜ 开通新主机 ➜ 新主机开防火墙 ➜ 切 DNS 绑定 ➜ 退订旧主机 ➜ 回写配置。
    """
    cfg, provider, dns_provider = build_orchestrators(config, cmd_region=region, command_name="rotate")

    # 命令行覆盖并更新 cfg 对应的参数
    if resource_id:
        cfg.rotation.resource_id = resource_id
    if state_file:
        cfg.rotation.state_file = state_file
    if firewall_ports:
        ports_list = [p.strip() for p in firewall_ports.split(",") if p.strip()]
        from src.config import _normalize_port
        try:
            cfg.rotation.firewall_ports = [_normalize_port(p) for p in ports_list]
        except Exception as exc:
            console.print(f"[bold red]❌ 端口格式校验失败:[/bold red] {exc}", style="red")
            raise typer.Exit(code=1)

    # 提前校验所选套餐是否合法
    chosen_plan = plan or cfg.rotation.plan
    try:
        provider.get_plan(chosen_plan)
    except Exception as exc:
        console.print(f"[bold red]❌ 套餐校验失败:[/bold red] {exc}", style="red")
        raise typer.Exit(code=1)

    # 解析并合并域名优先级
    global_proxied = cfg.dns.domains[0].proxied if cfg.dns.domains else False
    if domains:
        final_domains = []
        for name in domains.split(","):
            name = name.strip()
            if name:
                final_domains.append(DomainEntry(name=name, proxied=global_proxied))
    else:
        final_domains = cfg.rotation.domains if cfg.rotation.domains else cfg.dns.domains

    # 检验域名是否存在
    if not final_domains and not skip_dns:
        console.print("[bold red]❌ 错误:[/bold red] 必须在配置文件中指定域名绑定，或通过 -d/--domains 命令行参数指定！", style="red")
        raise typer.Exit(code=1)

    # 动态将最终生效的域名列表写回
    cfg.dns.domains = final_domains
    dns_provider.domains = final_domains

    # 校验 resource_id 是否存在
    if not cfg.rotation.resource_id:
        console.print("[bold red]❌ 错误:[/bold red] 必须在配置文件中指定 'resource_id' 节点，或通过 --resource-id 命令行参数指定！", style="red")
        raise typer.Exit(code=1)

    if reset_state:
        from src.state import RotationStateManager
        state_mgr = RotationStateManager(cfg.rotation.state_file)
        state_mgr.clear()

    pipeline = RotationPipeline(
        config_path=Path(config),
        cfg=cfg,
        provider=provider,
        dns_provider=dns_provider,
    )
    pipeline.run(plan_name=plan, skip_dns=skip_dns, dry_run=dry_run)


if __name__ == "__main__":
    app()
