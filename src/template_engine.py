from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from src.config import AppConfig

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    # 增加 tojson 过滤器，使 config.json.j2 中可以调用 {{ list | tojson }}
    env.filters["tojson"] = lambda v: json.dumps(v)
    return env


class TemplateEngine:
    """负责将业务配置渲染成对应的系统与应用模板"""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._env = _make_env()

    # ── 内部辅助方法 ──────────────────────────────────────────────────

    def _primary_domain(self) -> str:
        """获取第一个配置的域名"""
        if not self.cfg.dns.domains:
            raise ValueError("Cloudflare DNS 配置中没有提供任何域名记录")
        return self.cfg.dns.domains[0].name

    def _common_ctx(self) -> dict:
        sb = self.cfg.init.singbox
        si = self.cfg.init.server_init
        return dict(
            domain=self._primary_domain(),
            uuid=sb.uuid,
            proxy_username=sb.proxy_username,
            proxy_password=sb.proxy_password,
            cert_domain=si.cert_domain,
            reality_server_name=sb.reality.server_name,
            reality_private_key=sb.reality.private_key,
            reality_public_key=sb.reality.public_key,
            # WARP
            warp_enabled=sb.warp.enabled,
            warp_private_key=sb.warp.private_key,
            warp_address=sb.warp.address,
            # Upstream SOCKS5
            upstream_socks_enabled=sb.upstream_socks.enabled,
            upstream_socks_server=sb.upstream_socks.server,
            upstream_socks_port=sb.upstream_socks.port,
        )

    # ── 模板渲染接口 ──────────────────────────────────────────────────

    def render_singbox_config(self) -> str:
        """渲染 sing-box 的 config.json"""
        tmpl = self._env.get_template("config.json.j2")
        result = tmpl.render(**self._common_ctx())
        logger.debug("sing-box config.json 渲染完成 (%d 字节)", len(result))
        return result

    def render_subscribe(self) -> str:
        """渲染纯文本节点链接清单"""
        tmpl = self._env.get_template("subscribe.txt.j2")
        ctx = {**self._common_ctx(), "node_prefix": self.cfg.init.subscribe.node_prefix}
        result = tmpl.render(**ctx)
        logger.debug("节点订阅清单渲染完成 (共计 %d 行)", result.count("\n") + 1)
        return result

    def render_subscribe_b64(self) -> str:
        """渲染 Base64 格式订阅文件 (v2ray 传统订阅标准)"""
        plain = self.render_subscribe()
        return base64.b64encode(plain.encode()).decode()

    def render_init_sh(self) -> str:
        """渲染 init.sh"""
        si = self.cfg.init.server_init
        dns = self.cfg.dns
        tmpl = self._env.get_template("init.sh.j2")
        ctx = dict(
            sync_from_source=si.sync_from_source,
            source_server_ip=si.source_server_ip,
            hostname=si.hostname,
            swap_size_mb=si.swap_size_mb,
            acme_email=si.acme_email,
            cf_token=dns.api_token,
            cf_account_id=dns.account_id,
            cert_domain=si.cert_domain,
        )
        result = tmpl.render(**ctx)
        logger.debug("init.sh 渲染完成 (%d 字节)", len(result))
        return result
