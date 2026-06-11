from abc import ABC, abstractmethod
from typing import List, Optional

class CloudProvider(ABC):
    """
    云服务商接口，用于管理 VM 实例与镜像/快照
    """

    @abstractmethod
    def is_image_id(self, resource_id: str) -> bool:
        """判断 resource_id 是否为镜像/快照 ID"""
        pass

    @abstractmethod
    def list_custom_images(self) -> List[dict]:
        """列出所有的自定义镜像（按创建时间升序排列，即最老的在前面）"""
        pass

    @abstractmethod
    def delete_image(self, image_id: str) -> None:
        """删除指定自定义镜像"""
        pass

    @abstractmethod
    def ensure_image_quota(self, max_quota: int = 5) -> None:
        """确保自定义镜像数量不会超限，超限时清理最老的镜像"""
        pass

    @abstractmethod
    def create_image_from_instance(self, instance_id: str, image_name: str) -> str:
        """从已有实例创建自定义镜像并返回新创建的镜像 ID"""
        pass

    @abstractmethod
    def wait_for_image_ready(
        self,
        image_id: str,
        poll_interval: int = 15,
        timeout: int = 600,
    ) -> None:
        """阻塞等待自定义镜像制作就绪"""
        pass

    @abstractmethod
    def create_instance(
        self,
        image_id: str,
        plan_id: str,
        period: int = 1,
        charge_type: str = "PrePaid",
        auto_renew: bool = False,
    ) -> str:
        """创建一个全新的实例并返回实例 ID"""
        pass

    @abstractmethod
    def wait_for_instance_running(
        self,
        instance_id: str,
        poll_interval: int = 15,
        timeout: int = 300,
    ) -> None:
        """阻塞等待实例状态变为 Running"""
        pass

    @abstractmethod
    def get_instance_public_ip(self, instance_id: str) -> str:
        """获取实例的公网 IP 编"""
        pass

    @abstractmethod
    def reset_instance_password(self, instance_id: str, password: str) -> None:
        """重置实例的 root/administrator 密码"""
        pass

    @abstractmethod
    def reboot_instance(self, instance_id: str) -> None:
        """重启实例"""
        pass

    @abstractmethod
    def open_ports(self, instance_id: str, ports: List[str]) -> None:
        """放行实例的防火墙端口"""
        pass

    @abstractmethod
    def refund_instance(self, instance_id: str) -> None:
        """释放并退订旧实例"""
        pass

    @abstractmethod
    def get_plan(self, plan_name: str) -> dict:
        """从专属套餐列表中获取特定名称的套餐规格，并进行云商专属的非空与强类型匹配检验"""
        pass


class DNSProvider(ABC):
    """
    DNS 解析服务商接口
    """

    @abstractmethod
    def update_domains(self, ip: str) -> None:
        """更新配置中所有域名解析指向指定的 IP"""
        pass

    @abstractmethod
    def delete_all_domains(self) -> None:
        """删除配置中所有域名的解析记录"""
        pass
