import logging
from typing import List, Optional

import cloudflare

from src.providers.base import DNSProvider

logger = logging.getLogger(__name__)


class CloudflareDomainConfig:
    def __init__(self, name: str, proxied: bool) -> None:
        self.name = name
        self.proxied = proxied


class CloudflareDNSProvider(DNSProvider):
    """
    Cloudflare DNS A-record 管理的具体实现
    """

    def __init__(
        self,
        api_token: str,
        zone_id: str,
        domains: List[CloudflareDomainConfig],
        ttl: int = 60,
    ) -> None:
        self.api_token = api_token
        self.zone_id = zone_id
        self.domains = domains
        self.ttl = ttl
        self.cf = cloudflare.Cloudflare(api_token=api_token)

    # ── 内部辅助方法 ──────────────────────────────────────────────────

    def _get_a_record(self, name: str) -> Optional[object]:
        """查询指定域名对应的 A 记录"""
        records = self.cf.dns.records.list(
            zone_id=self.zone_id, name=name, type="A"
        )
        for record in records:
            return record
        return None

    # ── 接口实现 ──────────────────────────────────────────────────────

    def upsert_a_record(self, name: str, ip: str, proxied: bool) -> None:
        """创建或更新 DNS A 记录指向新的公网 IP"""
        existing = self._get_a_record(name)
        params = dict(
            zone_id=self.zone_id,
            type="A",
            name=name,
            content=ip,
            proxied=proxied,
            ttl=self.ttl,
        )
        try:
            if existing:
                logger.info("正在更新 DNS A 记录: '%s' → %s (小黄云代理=%s)", name, ip, proxied)
                self.cf.dns.records.update(dns_record_id=existing.id, **params)
            else:
                logger.info("正在创建 DNS A 记录: '%s' → %s (小黄云代理=%s)", name, ip, proxied)
                self.cf.dns.records.create(**params)
        except Exception as exc:
            err = str(exc)
            if any(code in err for code in ("403", "10000", "Authentication")):
                raise RuntimeError(
                    f"Cloudflare DNS 鉴权失败。请检查:\n"
                    f"  1. CF_API_TOKEN 权限配置是否包含 'Zone / DNS / Edit'\n"
                    f"  2. CF_ZONE_ID 与该域名所属的托管 Zone 是否一致\n"
                    f"原始错误: {exc}"
                ) from exc
            raise

    def update_domains(self, ip: str) -> None:
        """更新所有的配置域名"""
        for domain_cfg in self.domains:
            self.upsert_a_record(domain_cfg.name, ip, domain_cfg.proxied)
        logger.info("已成功将所有 Cloudflare DNS 记录更新为公网 IP: %s", ip)

    def delete_all_domains(self) -> None:
        """删除所有的配置域名记录 (Teardown 时使用)"""
        for domain_cfg in self.domains:
            existing = self._get_a_record(domain_cfg.name)
            if existing:
                logger.info("正在删除域名 '%s' 的 DNS A 记录 (记录 ID: %s) …", domain_cfg.name, existing.id)
                self.cf.dns.records.delete(
                    dns_record_id=existing.id,
                    zone_id=self.zone_id,
                )
                logger.info("域名 '%s' 的 DNS 记录已删除。", domain_cfg.name)
            else:
                logger.info("未找到域名 '%s' 的 DNS 记录，跳过删除。", domain_cfg.name)
