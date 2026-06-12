from __future__ import annotations

import logging
import sys
from rich.console import Console

from src.config import AppConfig
from src.state import InitStateManager
from src.providers.base import CloudProvider, DNSProvider
from src.template_engine import TemplateEngine
from src.ssh_client import SSHClient

logger = logging.getLogger(__name__)
console = Console()


class InitPipeline:
    """代理服务器快速初始化流水线 (幂等执行)"""

    def __init__(
        self,
        cfg: AppConfig,
        provider: CloudProvider,
        dns_provider: DNSProvider,
        state: InitStateManager,
    ) -> None:
        self.cfg = cfg
        self.provider = provider
        self.dns_provider = dns_provider
        self.state_mgr = state
        self.engine = TemplateEngine(cfg)

    def _primary_domain(self) -> str:
        """获取当前生效的第一个域名"""
        if not self.cfg.dns.domains:
            raise ValueError("DNS 配置中没有提供任何域名记录")
        return self.cfg.dns.domains[0].name

    def run(self, plan_name: str = "", dry_run: bool = False) -> None:
        """运行完整的快速初始化流程"""
        if dry_run:
            console.print("[bold yellow]=== 预览模式 (Dry Run)：仅渲染模板，不调用任何 API ===[/bold yellow]")
            console.print(f"  当前目标区域 : {self.cfg.provider.region}")
            console.print(f"  镜像 ID      : {self.cfg.init.image_id}")
            console.print(f"  防火墙端口   : {self.cfg.init.firewall_ports}")
            console.print(f"  状态文件     : {self.cfg.init.state_file}")
            console.print(f"  代理账号密码 : {self.cfg.init.singbox.proxy_username} / {self.cfg.init.singbox.proxy_password}")
            console.print(f"  SSH 端口/用户: {self.cfg.init.server_init.ssh_port} / {self.cfg.init.server_init.ssh_user}")
            console.print(f"  acme 注册邮箱: {self.cfg.init.server_init.acme_email}")
            console.print("\n[bold cyan]--- sing-box config.json (前 300 字节) ---[/bold cyan]")
            console.print(self.engine.render_singbox_config()[:300])
            console.print("\n[bold cyan]--- subscribe.txt 节点配置 ---[/bold cyan]")
            console.print(self.engine.render_subscribe())
            console.print("\n[bold cyan]--- init.sh 初始化脚本 (前 15 行) ---[/bold cyan]")
            console.print("\n".join(self.engine.render_init_sh().splitlines()[:15]))
            console.print("\n[bold yellow]=== 预览完成 ===[/bold yellow]")
            return

        try:
            # Step 1: 创建实例
            instance_id, ip = self._step1_create_instance(plan_name=plan_name)

            # Step 1b: API 重置密码 + 重启
            self._step1b_reset_password(instance_id)

            # Step 2: 开放端口
            self._step2_open_ports(instance_id)

            # Step 3: 更新 DNS
            self._step3_update_dns(ip)

            # Step 4: 上传 sing-box 配置文件
            self._step4_upload_config(ip)

            # Step 5: 渲染并在远端运行初始化脚本
            self._step5_init_server(ip)

            # Step 6 & 7: 渲染并上传订阅文件
            self._step6_upload_subscribe(ip)

            # 最终成功提示
            console.print("\n" + "=" * 60, style="bold green")
            console.print("🎉 代理服务器初始化已成功完成！", style="bold green")
            console.print(f"  实例 ID : {self.state_mgr.state.instance_id}")
            console.print(f"  公网 IP   : {self.state_mgr.state.instance_ip}")
            console.print(f"  配置域名  : {self.cfg.dns.domains[0].name if self.cfg.dns.domains else 'None'}")
            console.print(f"  目标区域  : {self.cfg.provider.region}")
            console.print("=" * 60, style="bold green")

        except Exception as exc:
            console.print(f"\n[bold red]❌ 步骤执行失败:[/bold red] {exc}", style="red")
            console.print("[yellow]请排查上述错误后再次运行，工具会自动跳过已完成的步骤。[/yellow]")
            sys.exit(1)

    # ── 步骤子逻辑 ────────────────────────────────────────────────────

    def _step1_create_instance(self, plan_name: str = "") -> tuple[str, str]:
        s = self.state_mgr.state
        if s.instance_id and s.instance_ip:
            console.print(f" [bold green]✔[/bold green] [Step 1] 跳过 — 实例已存在: {s.instance_id} ({s.instance_ip})")
            return s.instance_id, s.instance_ip

        with console.status("[bold cyan][Step 1] 正在向云商申请创建实例 …[/bold cyan]") as status:
            chosen_plan = plan_name or self.cfg.init.plan
            plan_spec = self.provider.get_plan(chosen_plan)

            instance_id = self.provider.create_instance(
                image_id=self.cfg.init.image_id,
                plan_id=plan_spec["plan_id"],
                period=plan_spec["period"],
                charge_type=plan_spec["charge_type"],
                auto_renew=plan_spec["auto_renew"],
            )
            status.update("[bold cyan][Step 1] 等待实例就绪 (Running) …[/bold cyan]")
            self.provider.wait_for_instance_running(
                instance_id,
                poll_interval=self.cfg.init.retry.instance_poll_interval,
                timeout=self.cfg.init.retry.instance_start_timeout,
            )
            status.update("[bold cyan][Step 1] 获取公网 IP 中 …[/bold cyan]")
            ip = self.provider.get_instance_public_ip(instance_id)

            self.state_mgr.set_instance(instance_id, ip)

        console.print(f" [bold green]✔[/bold green] [Step 1] 完成 — {instance_id} @ {ip}")
        return instance_id, ip

    def _step1b_reset_password(self, instance_id: str) -> None:
        if self.state_mgr.state.password_reset:
            console.print(" [bold green]✔[/bold green] [Step 1b] 跳过 — 实例管理员密码已重置。")
            return

        with console.status("[bold cyan][Step 1b] 正在重置 root 密码，并自动重启机器以生效 …[/bold cyan]") as status:
            self.provider.reset_instance_password(instance_id, self.cfg.init.server_init.ssh_password)
            self.provider.reboot_instance(instance_id)
            
            status.update("[bold cyan][Step 1b] 实例正在重启，等待重新就绪 …[/bold cyan]")
            self.provider.wait_for_instance_running(
                instance_id,
                poll_interval=self.cfg.init.retry.instance_poll_interval,
                timeout=self.cfg.init.retry.instance_start_timeout,
            )
            self.state_mgr.mark_password_reset()

        console.print(" [bold green]✔[/bold green] [Step 1b] 完成 — 密码重置成功，实例已就绪。")

    def _step2_open_ports(self, instance_id: str) -> None:
        if self.state_mgr.state.ports_opened:
            console.print(" [bold green]✔[/bold green] [Step 2] 跳过 — 防火墙端口此前已成功放行。")
            return

        with console.status("[bold cyan][Step 2] 正在开通防火墙规则以放行指定端口 …[/bold cyan]"):
            self.provider.open_ports(instance_id, self.cfg.init.firewall_ports)
            self.state_mgr.mark_ports_opened()

        console.print(" [bold green]✔[/bold green] [Step 2] 完成 — 防火墙端口开通。")

    def _step3_update_dns(self, ip: str) -> None:
        if self.state_mgr.state.dns_updated:
            console.print(" [bold green]✔[/bold green] [Step 3] 跳过 — DNS 记录已存在。")
            return

        with console.status(f"[bold cyan][Step 3] 正在更新 DNS A 记录至 {ip} …[/bold cyan]"):
            self.dns_provider.update_domains(ip)
            self.state_mgr.mark_dns_updated()

        console.print(" [bold green]✔[/bold green] [Step 3] 完成 — DNS 解析记录已更新。")

    def _step4_upload_config(self, ip: str) -> None:
        if self.state_mgr.state.config_synced:
            console.print(" [bold green]✔[/bold green] [Step 4] 跳过 — sing-box 配置文件已成功同步至远端。")
            return

        with console.status("[bold cyan][Step 4] 正在渲染并上传 sing-box 配置文件 config.json …[/bold cyan]") as status:
            config_content = self.engine.render_singbox_config()

            si = self.cfg.init.server_init
            with SSHClient(
                host=ip,
                user=si.ssh_user,
                port=si.ssh_port,
                password=si.ssh_password,
            ) as ssh:
                status.update("[bold cyan][Step 4] 尝试建立 SSH 密钥/密码连接 …[/bold cyan]")
                ssh.connect(
                    timeout=self.cfg.init.retry.ssh_connect_timeout,
                    interval=self.cfg.init.retry.ssh_connect_interval,
                )
                status.update("[bold cyan][Step 4] 上传中 …[/bold cyan]")
                ssh.exec("mkdir -p /etc/sing-box")
                ssh.upload_str(config_content, "/etc/sing-box/config.json")

            self.state_mgr.mark_config_synced()

        console.print(" [bold green]✔[/bold green] [Step 4] 完成 — sing-box 配置文件成功上传。")

    def _step5_init_server(self, ip: str) -> None:
        if self.state_mgr.state.server_initialized:
            console.print(" [bold green]✔[/bold green] [Step 5] 跳过 — 远端服务器已经执行过初始化。")
            return

        console.print("[bold cyan][Step 5] 开始执行远端服务器初始化脚本 (这通常需要 3~5 分钟) …[/bold cyan]")
        init_sh_content = self.engine.render_init_sh()
        remote_init_path = "/tmp/proxy_init.sh"
        si = self.cfg.init.server_init

        with SSHClient(
            host=ip,
            user=si.ssh_user,
            port=si.ssh_port,
            password=si.ssh_password,
        ) as ssh:
            ssh.connect(
                timeout=self.cfg.init.retry.ssh_connect_timeout,
                interval=self.cfg.init.retry.ssh_connect_interval,
            )
            ssh.upload_str(init_sh_content, remote_init_path)
            ssh.exec(f"chmod +x {remote_init_path}")
            
            # 由于指令执行过程需要实时看到系统安装状态，这里用 print 打印其标准输出，保持终端可见性
            ssh.exec(f"bash {remote_init_path} > /tmp/init.log 2>&1", check=True)

        self.state_mgr.mark_server_initialized()
        console.print(" [bold green]✔[/bold green] [Step 5] 完成 — 远端初始化脚本执行完毕。")

    def _step6_upload_subscribe(self, ip: str) -> None:
        if self.state_mgr.state.subscribe_uploaded:
            console.print(" [bold green]✔[/bold green] [Step 6+7] 跳过 — 订阅文件已上传。")
            return

        with console.status("[bold cyan][Step 6+7] 正在生成 Base64 订阅并同步至 Web 静态目录 …[/bold cyan]"):
            sub_b64 = self.engine.render_subscribe_b64()
            sub_cfg = self.cfg.init.subscribe
            remote_path = f"{sub_cfg.remote_dir.rstrip('/')}/{sub_cfg.filename}"

            si = self.cfg.init.server_init
            with SSHClient(
                host=ip,
                user=si.ssh_user,
                port=si.ssh_port,
                password=si.ssh_password,
            ) as ssh:
                ssh.connect(
                    timeout=self.cfg.init.retry.ssh_connect_timeout,
                    interval=self.cfg.init.retry.ssh_connect_interval,
                )
                ssh.exec(f"mkdir -p {sub_cfg.remote_dir}")
                ssh.upload_str(sub_b64, remote_path)

            self.state_mgr.mark_subscribe_uploaded()

        domain = self._primary_domain()
        console.print(f" [bold green]✔[/bold green] [Step 6+7] 完成 — 订阅同步成功！")
        console.print(f"       订阅链接为: https://{domain}/sub/{sub_cfg.filename}", style="bold underline")

    # ── Teardown ──

    def teardown(
        self,
        instance_id: str,
        skip_dns: bool = False,
        skip_refund: bool = False,
    ) -> None:
        """退订实例并清理对应的 DNS A 记录"""
        console.print("\n" + "=" * 60, style="bold red")
        console.print("⚠️ 开始执行 TEARDOWN 清理任务", style="bold red")
        console.print(f"  实例 ID : {instance_id}")
        console.print(f"  DNS 记录 : {', '.join(d.name for d in self.cfg.dns.domains)}")
        console.print("=" * 60, style="bold red")

        # 1. 提请云厂商退订机器
        if skip_refund:
            console.print("[Teardown] 跳过退订机器 (--skip-refund)")
        else:
            with console.status(f"[Teardown] 正在通知云商释放机器 {instance_id} 并发起退款 …"):
                self.provider.refund_instance(instance_id)
            console.print(" [bold red]✔[/bold red] [Teardown] 实例退款退订请求发送成功。")

        # 2. 清理 DNS A 记录
        if skip_dns:
            console.print("[Teardown] 跳过 DNS 记录删除 (--skip-dns)")
        else:
            with console.status("[Teardown] 正在清理 Cloudflare DNS 解析记录 …"):
                self.dns_provider.delete_all_domains()
            console.print(" [bold red]✔[/bold red] [Teardown] DNS 解析记录清理完成。")

        console.print("\n🎉 [bold green]TEARDOWN 清理任务全部完成 ✓[/bold green]\n")
