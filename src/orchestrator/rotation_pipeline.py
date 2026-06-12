from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from rich.console import Console

from src.config import AppConfig
from src.state import RotationStateManager
from src.providers.base import CloudProvider, DNSProvider

logger = logging.getLogger(__name__)
console = Console()


def _update_config_resource_id(config_path: Path, new_instance_id: str) -> None:
    """自动将 config.yaml 里的旧 resource_id 字段覆写为新的实例 ID，为下一次轮转做好准备"""
    if not config_path.exists():
        console.print(f"[bold yellow]⚠[/bold yellow] 配置文件未找到: {config_path}；无法自动重写 resource_id。")
        return

    text = config_path.read_text(encoding="utf-8")
    # 正则表达式匹配没有被注释的 resource_id: "xxx" (支持行首缩进)
    pattern = r'^(\s*resource_id:\s*)["\']([^"\']*)["\']'
    replacement = f'\\g<1>"{new_instance_id}"'
    new_text, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)

    if count == 0:
        console.print(f"[bold yellow]⚠[/bold yellow] 未在配置文件 {config_path} 中找到可更新的 'resource_id' 节点。")
        return

    config_path.write_text(new_text, encoding="utf-8")
    console.print(f" [bold green]✔[/bold green] 配置文件已更新: resource_id ➔ {new_instance_id}")


class RotationPipeline:
    """代理服务器自动轮转流水线 (幂等执行)"""

    def __init__(
        self,
        config_path: Path,
        cfg: AppConfig,
        provider: CloudProvider,
        dns_provider: DNSProvider,
    ) -> None:
        self.config_path = config_path
        self.cfg = cfg
        self.provider = provider
        self.dns_provider = dns_provider
        self.state_mgr = RotationStateManager(cfg.rotation.state_file)

    def run(self, plan_name: str = "", skip_dns: bool = False, dry_run: bool = False) -> None:
        """执行服务器轮转流程"""
        resource_id = self.cfg.rotation.resource_id

        if dry_run:
            chosen_plan = plan_name or self.cfg.rotation.plan
            try:
                plan_spec = self.provider.get_plan(chosen_plan)
                spec_str = f"{plan_spec['plan_name']} (套餐 ID: {plan_spec['plan_id']})"
            except Exception:
                spec_str = f"{chosen_plan} (因未连接云服务商，仅展示名称)"

            console.print("[bold yellow]=== 预览模式 (Dry Run) ===[/bold yellow]")
            console.print(f"  当前目标区域 : {self.cfg.provider.region}")
            console.print(f"  被轮换的源 ID : {resource_id}")
            console.print(f"  新实例套餐名 : {spec_str}")
            console.print(f"  放行的防火墙 : {self.cfg.rotation.firewall_ports}")
            console.print(f"  DNS 解析域名 : {[d.name for d in self.cfg.dns.domains]}")
            console.print("[bold yellow]=== 预览完毕 ===[/bold yellow]")
            return

        # 1. 载入并检查持久化状态
        self.state_mgr.load()
        if self.state_mgr.is_stale(resource_id):
            console.print(f"[bold yellow]⚠[/bold yellow] 检测到状态文件属于另一个资源 ({self.state_mgr.resource_id})。重置并重新开始。")
            self.state_mgr.clear()
        self.state_mgr.set_resource_id(resource_id)

        try:
            # 2. 导出/识别自定义镜像
            image_id = self._resolve_image_id(resource_id)

            # 3. 购买并开通全新主机
            new_instance_id = self._create_new_instance(image_id, plan_name=plan_name)

            # 4. 放行防火墙端口
            self._open_firewall_ports(new_instance_id)

            # 5. 更新 DNS 记录
            public_ip = self._update_dns_records(new_instance_id, skip_dns)

            # 最终成功提示
            console.print("\n" + "=" * 60, style="bold green")
            console.print("🎉 服务器实例轮换顺利完成！", style="bold green")
            console.print(f"  新实例 ID   : {new_instance_id}")
            console.print(f"  新公网 IP   : {public_ip}")
            console.print(f"  DNS 解析    : {'已同步切换' if not skip_dns else '已跳过 (DNS)'}")
            console.print("=" * 60, style="bold green")

            # 6. 退订旧实例 (仅在输入原为实例 ID 时发起)
            self._refund_old_instance(resource_id)

            # 7. 更新 config.yaml 里的机器 ID
            _update_config_resource_id(self.config_path, new_instance_id)

            # 8. 状态保留提示
            console.print(f"[yellow]提示: 轮换进度状态已保存至 {self.cfg.rotation.state_file}。若要从头开始全新的轮换，请传入 --reset-state。[/yellow]")

        except Exception as exc:
            console.print(f"\n[bold red]❌ 轮换过程中断:[/bold red] {exc}", style="red")
            console.print("[yellow]请修复相关问题后重试，将自动跳过已完成步骤继续。[/yellow]")
            sys.exit(1)

    # ── 步骤子逻辑 ────────────────────────────────────────────────────

    def _resolve_image_id(self, resource_id: str) -> str:
        if self.state_mgr.image_id:
            image_id = self.state_mgr.image_id
            console.print(f" [bold green]✔[/bold green] [步骤 1] 跳过 — 镜像在此前已经制备完成: {image_id}")
            return image_id

        if self.provider.is_image_id(resource_id):
            console.print(f" [bold green]✔[/bold green] [步骤 1] 输入的资源 ID 本身即为镜像 ID: {resource_id}")
            image_id = resource_id
            self.state_mgr.set_image_id(image_id)
        else:
            with console.status(f"[bold cyan][步骤 1] 正在将旧主机 {resource_id} 导出为自定义快照镜像 …[/bold cyan]") as status:
                image_id = self.provider.create_image_from_instance(
                    instance_id=resource_id,
                    image_name=self.cfg.rotation.snapshot_name,
                )
                self.state_mgr.set_image_id(image_id)
                status.update("[bold cyan][步骤 1] 等待自定义镜像制作完成 (通常需要 2~5 分钟) …[/bold cyan]")
                self.provider.wait_for_image_ready(
                    image_id,
                    poll_interval=self.cfg.rotation.poll_interval,
                    timeout=self.cfg.rotation.poll_timeout,
                )
            console.print(f" [bold green]✔[/bold green] [步骤 1] 自定义镜像制作完成: {image_id}")
        return image_id

    def _create_new_instance(self, image_id: str, plan_name: str = "") -> str:
        if self.state_mgr.instance_id:
            new_instance_id = self.state_mgr.instance_id
            console.print(f" [bold green]✔[/bold green] [步骤 2] 跳过 — 新实例此前已创建: {new_instance_id}")
            return new_instance_id

        with console.status("[bold cyan][步骤 2] 正在使用导出的镜像开通新机器实例 …[/bold cyan]") as status:
            chosen_plan = plan_name or self.cfg.rotation.plan
            plan_spec = self.provider.get_plan(chosen_plan)

            new_instance_id = self.provider.create_instance(
                image_id=image_id,
                plan_id=plan_spec["plan_id"],
                period=plan_spec["period"],
                charge_type=plan_spec["charge_type"],
                auto_renew=plan_spec["auto_renew"],
            )
            self.state_mgr.set_instance_id(new_instance_id)
            status.update("[bold cyan][步骤 2] 等待新机器实例启动就绪 (Running) …[/bold cyan]")
            self.provider.wait_for_instance_running(
                new_instance_id,
                poll_interval=self.cfg.rotation.poll_interval,
                timeout=self.cfg.rotation.poll_timeout,
            )

        console.print(f" [bold green]✔[/bold green] [步骤 2] 新机器开通并启动完成: {new_instance_id}")
        return new_instance_id

    def _open_firewall_ports(self, new_instance_id: str) -> None:
        if self.state_mgr.ports_opened:
            console.print(" [bold green]✔[/bold green] [步骤 3] 跳过 — 防火墙端口先前已全部开通。")
            return

        if self.cfg.rotation.firewall_ports:
            with console.status("[bold cyan][步骤 3] 正在新实例上开通防火墙端口规则 …[/bold cyan]"):
                self.provider.open_ports(new_instance_id, self.cfg.rotation.firewall_ports)
                self.state_mgr.set_ports_opened()
            console.print(" [bold green]✔[/bold green] [步骤 3] 新实例防火墙配置完成。")
        else:
            console.print(" [bold green]✔[/bold green] [步骤 3] 跳过 — 配置中未指定需要放行的端口。")

    def _update_dns_records(self, new_instance_id: str, skip_dns: bool) -> str:
        if skip_dns:
            with console.status("[bold cyan]正在获取新实例的公网 IP 编 …[/bold cyan]"):
                public_ip = self.provider.get_instance_public_ip(new_instance_id)
            self.state_mgr.set_public_ip(public_ip)
            console.print(f" [bold green]✔[/bold green] [步骤 4] 略过 DNS 切换配置。公网 IP 为: {public_ip}")
            return public_ip

        if self.state_mgr.dns_updated:
            public_ip = self.state_mgr.public_ip
            console.print(f" [bold green]✔[/bold green] [步骤 4] 跳过 — DNS 记录已同步更新 (IP: {public_ip})。")
            return public_ip

        with console.status("[bold cyan][步骤 4] 正在查询新实例公网 IP，准备切换 DNS 记录 …[/bold cyan]") as status:
            public_ip = self.provider.get_instance_public_ip(new_instance_id)
            self.state_mgr.set_public_ip(public_ip)
            if self.cfg.dns.domains:
                status.update("[bold cyan][步骤 4] 正在向 DNS 提供商更新 A 记录 …[/bold cyan]")
                self.dns_provider.update_domains(public_ip)
                self.state_mgr.set_dns_updated()
            else:
                status.update("[bold cyan]未指定域名，跳过 DNS A 记录更新。[/bold cyan]")

        console.print(" [bold green]✔[/bold green] [步骤 4] DNS 切换解析完成。")
        return public_ip

    def _refund_old_instance(self, old_instance_id: str) -> None:
        if self.provider.is_image_id(old_instance_id):
            console.print(" [bold green]✔[/bold green] [步骤 5] 源资源为镜像，跳过退订释放实例流程。")
            return

        with console.status(f"[bold cyan][步骤 5] 正在向云商提请退款并释放旧实例 {old_instance_id} …[/bold cyan]"):
            try:
                self.provider.refund_instance(old_instance_id)
                console.print(f" [bold green]✔[/bold green] [步骤 5] 旧实例退款/释放命令发送完毕。")
            except Exception as exc:
                # 阿里云退订可能因为账号风控等原因抛异常，这是一个非致命阻断
                # 新实例已经开通并可用，因此打印 warning 提示用户手动去控制台退款
                console.print(f"\n[bold yellow]⚠ 警告: 自动提请退款释放旧实例失败 (通常是阿里云风控或折扣干扰):[/bold yellow] {exc}")
                console.print("[yellow]请手动登录阿里云控制台，找到该轻量实例办理退款以防重复计费。[/yellow]\n")
