import logging
import time
import uuid
from typing import List

from alibabacloud_swas_open20200601.client import Client as SwasClient
from alibabacloud_swas_open20200601 import models as swas_models
from alibabacloud_bssopenapi20171214.client import Client as BssClient
from alibabacloud_bssopenapi20171214 import models as bss_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

from src.providers.base import CloudProvider

logger = logging.getLogger(__name__)

MAX_IMAGES_PER_REGION = 5


class AliyunProvider(CloudProvider):
    """
    阿里云轻量应用服务器 (SWAS) 及其账单 (BSS) 的具体实现
    """

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        region: str,
        plans: dict = None,
        default_plan: str = "",
    ) -> None:
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.region = region
        self.plans = plans or {}
        self.default_plan = default_plan

        swas_api_config = open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint=f"swas.{region}.aliyuncs.com",
        )
        self.client = SwasClient(swas_api_config)

        bss_api_config = open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint="business.aliyuncs.com",
        )
        self.bss_client = BssClient(bss_api_config)
        self._runtime = util_models.RuntimeOptions()

    # ── 镜像检测 ──────────────────────────────────────────────────────────

    def is_image_id(self, resource_id: str) -> bool:
        """镜像ID以 m- 开头，机器实例以 alikafka- 或 sw- 开头"""
        return resource_id.startswith("m-")

    # ── 镜像管理 ──────────────────────────────────────────────────────

    def list_custom_images(self) -> List[dict]:
        """返回当前地域下的自定义镜像列表，并按照创建时间升序排列 (最老的排前面)"""
        request = swas_models.ListCustomImagesRequest(region_id=self.region)
        response = self.client.list_custom_images_with_options(request, self._runtime)
        images = response.body.custom_images or []
        # 按创建时间排序
        return sorted(images, key=lambda img: img.creation_time or "")

    def delete_image(self, image_id: str) -> None:
        """删除指定的自定义镜像"""
        logger.info("正在删除阿里云自定义镜像 %s …", image_id)
        request = swas_models.DeleteCustomImagesRequest(
            region_id=self.region,
            image_ids=image_id,
        )
        self.client.delete_custom_images_with_options(request, self._runtime)
        logger.info("自定义镜像 %s 已删除。", image_id)

    def ensure_image_quota(self, max_quota: int = MAX_IMAGES_PER_REGION) -> None:
        """检测配额，防止触发阿里云“每个地域最多 5 个自定义镜像”的限制"""
        images = self.list_custom_images()
        if len(images) >= max_quota:
            to_delete_count = len(images) - max_quota + 1
            logger.warning(
                "自定义镜像数量 [%d/%d] 已达或超出限制，将清理最早的 %d 个镜像。",
                len(images), max_quota, to_delete_count,
            )
            for img in images[:to_delete_count]:
                self.delete_image(img.image_id)

    def create_image_from_instance(self, instance_id: str, image_name: str) -> str:
        """从实例导出自定义镜像"""
        self.ensure_image_quota()

        logger.info("正在从实例 %s 创建自定义镜像 '%s' …", instance_id, image_name)
        request = swas_models.CreateCustomImageRequest(
            region_id=self.region,
            instance_id=instance_id,
            image_name=image_name,
        )
        response = self.client.create_custom_image_with_options(request, self._runtime)
        image_id = response.body.image_id
        logger.info("自定义镜像创建请求已提交，镜像 ID: %s", image_id)
        return image_id

    def wait_for_image_ready(
        self,
        image_id: str,
        poll_interval: int = 15,
        timeout: int = 600,
    ) -> None:
        """
        阻塞等待镜像状态为 Available (1)。
        阿里云 SWAS 自定义镜像状态码：
          0 - 复制中 (Copying)
          1 - 可用 (Available)
          2 - 不可用 (Unavailable)
          3 - 创建失败 (Creation failed)
          4 - 创建中 (Creating)
        """
        STATUS_LABELS = {
            0: "复制中",
            1: "可用",
            2: "不可用",
            3: "创建失败",
            4: "创建中",
        }
        TERMINAL_ERROR_CODES = {2, 3}

        logger.info("等待自定义镜像 %s 变为可用状态 …", image_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            request = swas_models.ListCustomImagesRequest(region_id=self.region)
            response = self.client.list_custom_images_with_options(request, self._runtime)
            images = response.body.custom_images or []
            for img in images:
                if img.image_id == image_id:
                    try:
                        status_code = int(img.status)
                    except (TypeError, ValueError):
                        status_code = -1
                    label = STATUS_LABELS.get(status_code, f"未知({img.status})")
                    logger.info("自定义镜像 %s 当前状态: %s", image_id, label)
                    if status_code == 1:
                        return
                    if status_code in TERMINAL_ERROR_CODES:
                        raise RuntimeError(
                            f"自定义镜像 {image_id} 处于失败的终态: {label}"
                        )
            time.sleep(poll_interval)
        raise TimeoutError(f"自定义镜像 {image_id} 在 {timeout} 秒内未就绪")

    # ── 实例管理 ───────────────────────────────────────────────────

    def create_instance(
        self,
        image_id: str,
        plan_id: str,
        period: int = 1,
        charge_type: str = "PrePaid",
        auto_renew: bool = False,
    ) -> str:
        """创建全新实例"""
        logger.info(
            "正在购买新实例 (镜像=%s, 套餐=%s, 计费=%s, 周期=%d个月) …",
            image_id, plan_id, charge_type, period,
        )
        request = swas_models.CreateInstancesRequest(
            region_id=self.region,
            image_id=image_id,
            plan_id=plan_id,
            period=period,
            charge_type=charge_type,
            auto_renew=auto_renew,
            amount=1,
        )
        response = self.client.create_instances_with_options(request, self._runtime)
        instance_ids = response.body.instance_ids or []
        if not instance_ids:
            raise RuntimeError("CreateInstances 接口没有返回有效的实例 ID")
        instance_id = instance_ids[0]
        logger.info("新实例已成功创建，实例 ID: %s", instance_id)
        return instance_id

    def wait_for_instance_running(
        self,
        instance_id: str,
        poll_interval: int = 15,
        timeout: int = 300,
    ) -> None:
        """阻塞等待实例变为 Running 状态"""
        logger.info("等待实例 %s 状态变为 Running …", instance_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            instances = self._list_instances()
            for inst in instances:
                if inst.instance_id == instance_id:
                    status = (inst.status or "").lower()
                    logger.info("实例 %s 当前状态: %s", instance_id, status)
                    if status == "running":
                        return
                    if status in ("stopped", "error"):
                        raise RuntimeError(f"实例 {instance_id} 进入了异常状态: {status}")
            time.sleep(poll_interval)
        raise TimeoutError(f"实例 {instance_id} 在 {timeout} 秒内未变为 Running 状态")

    def get_instance_public_ip(self, instance_id: str) -> str:
        """获取实例的公网 IP"""
        for inst in self._list_instances():
            if inst.instance_id == instance_id:
                ip = inst.public_ip_address
                if ip:
                    logger.info("实例 %s 的公网 IP 为: %s", instance_id, ip)
                    return ip
        raise RuntimeError(f"未找到实例 {instance_id} 的公网 IP 地址")

    def reset_instance_password(self, instance_id: str, password: str) -> None:
        """重置 root 账号密码"""
        logger.info("正在通过 API 重置实例 %s 的 root 密码 …", instance_id)
        request = swas_models.UpdateInstanceAttributeRequest(
            region_id=self.region,
            instance_id=instance_id,
            password=password,
        )
        self.client.update_instance_attribute_with_options(request, self._runtime)
        logger.info("实例 %s 密码修改成功。", instance_id)

    def reboot_instance(self, instance_id: str) -> None:
        """重启实例"""
        logger.info("正在重启实例 %s …", instance_id)
        request = swas_models.RebootInstanceRequest(
            region_id=self.region,
            instance_id=instance_id,
        )
        self.client.reboot_instance_with_options(request, self._runtime)
        logger.info("重启指令已发送给实例 %s。", instance_id)

    def _list_instances(self) -> list:
        request = swas_models.ListInstancesRequest(region_id=self.region)
        response = self.client.list_instances_with_options(request, self._runtime)
        return response.body.instances or []

    # ── 防火墙端口管理 ────────────────────────────────────────────────

    def open_ports(self, instance_id: str, ports: List[str]) -> None:
        """放行实例防火墙中的端口"""
        for port_str in ports:
            for protocol in ("TCP", "UDP"):
                logger.info("在实例 %s 上开放防火墙端口: %s/%s …", instance_id, port_str, protocol)
                request = swas_models.CreateFirewallRulesRequest(
                    region_id=self.region,
                    instance_id=instance_id,
                    firewall_rules=[
                        swas_models.CreateFirewallRulesRequestFirewallRules(
                            rule_protocol=protocol,
                            port=port_str,
                            remark=f"auto-opened-{protocol.lower()}-{port_str}",
                        )
                    ],
                )
                self.client.create_firewall_rules_with_options(request, self._runtime)
        logger.info("所有指定的防火墙端口放行完成。")

    # ── 退订与释放 ───────────────────────────────────────────────────

    def refund_instance(self, instance_id: str) -> None:
        """退订并释放旧实例"""
        logger.info("正在向阿里云提交退款释放实例请求 (实例: %s) …", instance_id)
        request = bss_models.RefundInstanceRequest(
            client_token=str(uuid.uuid4()),
            instance_id=instance_id,
            product_code="ace_eweb",
            product_type="",
            immediately_release=1
        )
        self.bss_client.refund_instance_with_options(request, self._runtime)
        logger.info("实例 %s 的退订/释放命令已发送完成。", instance_id)

    def get_plan(self, plan_name: str) -> dict:
        """获取并校验指定的套餐"""
        target = plan_name.strip() if plan_name else ""
        if not target:
            target = self.default_plan.strip()
        if not target:
            if self.plans:
                target = list(self.plans.keys())[0]
            else:
                raise ValueError("阿里云 plans 套餐字典为空，且没有指定 default_plan 套餐。")

        if target not in self.plans:
            raise ValueError(f"阿里云配置中未找到名为 '{target}' 的套餐配置。当前可选: {list(self.plans.keys())}")

        spec = self.plans[target]
        if not isinstance(spec, dict):
            raise ValueError(f"套餐 '{target}' 的配置格式错误，必须为键值对字典。")

        plan_id = spec.get("plan_id")
        if not plan_id or not isinstance(plan_id, str):
            raise ValueError(f"套餐 '{target}' 结构校验失败: plan_id 字段缺失或类型错误 (必须为非空字符串)。")

        try:
            period = int(spec.get("period", 1))
        except (TypeError, ValueError):
            raise ValueError(f"套餐 '{target}' 结构校验失败: period 字段类型错误 (必须为正整数)。")

        charge_type = spec.get("charge_type", "PrePaid")
        if charge_type not in ("PrePaid", "PostPaid"):
            raise ValueError(f"套餐 '{target}' 结构校验失败: charge_type 必须为 'PrePaid' 或 'PostPaid'。")

        auto_renew = bool(spec.get("auto_renew", False))

        return {
            "plan_name": target,
            "plan_id": plan_id,
            "period": period,
            "charge_type": charge_type,
            "auto_renew": auto_renew,
        }
