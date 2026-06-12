from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Init State ──────────────────────────────────────────────────────

@dataclass
class InitRunState:
    instance_id: Optional[str] = None
    instance_ip: Optional[str] = None
    password_reset: bool = False
    ports_opened: bool = False
    dns_updated: bool = False
    config_synced: bool = False
    server_initialized: bool = False
    subscribe_uploaded: bool = False


class InitStateManager:
    """Init 流水线的断点续跑状态持久化管理器"""

    def __init__(self, state_file: str) -> None:
        self._path = Path(state_file)
        self._state = self._load()

    def _load(self) -> InitRunState:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                state = InitRunState(**{k: v for k, v in data.items()
                                    if k in InitRunState.__dataclass_fields__})
                logger.info("从 %s 加载了已有的初始化进度状态: %s", self._path, asdict(state))
                return state
            except Exception as exc:
                logger.warning("解析初始化进度状态文件 (%s) 失败，将重新开始: %s", self._path, exc)
        return InitRunState()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(asdict(self._state), indent=2), encoding="utf-8")
        logger.debug("状态保存至 %s", self._path)

    @property
    def state(self) -> InitRunState:
        return self._state

    def set_instance(self, instance_id: str, ip: str) -> None:
        self._state.instance_id = instance_id
        self._state.instance_ip = ip
        self._save()

    def mark_password_reset(self) -> None:
        self._state.password_reset = True
        self._save()

    def mark_ports_opened(self) -> None:
        self._state.ports_opened = True
        self._save()

    def mark_dns_updated(self) -> None:
        self._state.dns_updated = True
        self._save()

    def mark_config_synced(self) -> None:
        self._state.config_synced = True
        self._save()

    def mark_server_initialized(self) -> None:
        self._state.server_initialized = True
        self._save()

    def mark_subscribe_uploaded(self) -> None:
        self._state.subscribe_uploaded = True
        self._save()

    def reset(self) -> None:
        """清理状态并重新开始"""
        self._state = InitRunState()
        if self._path.exists():
            self._path.unlink()
        logger.warning("初始化进度状态已被清理。")


# ─── Rotation State ──────────────────────────────────────────────────

class RotationStateManager:
    """Rotation 流水线的断点续跑状态持久化管理器"""

    def __init__(self, state_file: str) -> None:
        self.path = Path(state_file)
        self._data: dict = {}

    def load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as fh:
                    self._data = json.load(fh)
                logger.info("从 %s 加载了已有的轮换进度状态", self.path)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("读取轮换状态文件失败 (%s)；将重新开始。", exc)
                self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        logger.debug("轮换状态保存至 %s", self.path)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
            logger.info("清理轮换进度状态文件: %s", self.path)
        self._data = {}

    @property
    def resource_id(self) -> str:
        return self._data.get("resource_id", "")

    @property
    def image_id(self) -> str:
        return self._data.get("image_id", "")

    @property
    def instance_id(self) -> str:
        return self._data.get("instance_id", "")

    @property
    def public_ip(self) -> str:
        return self._data.get("public_ip", "")

    @property
    def ports_opened(self) -> bool:
        return bool(self._data.get("ports_opened", False))

    @property
    def dns_updated(self) -> bool:
        return bool(self._data.get("dns_updated", False))

    @property
    def refunded(self) -> bool:
        return bool(self._data.get("refunded", False))

    def set_resource_id(self, v: str) -> None:
        self._data.setdefault("created_at", time.time())
        self._data["resource_id"] = v
        self.save()

    def set_image_id(self, v: str) -> None:
        self._data["image_id"] = v
        self.save()

    def set_instance_id(self, v: str) -> None:
        self._data["instance_id"] = v
        self.save()

    def set_public_ip(self, v: str) -> None:
        self._data["public_ip"] = v
        self.save()

    def set_ports_opened(self) -> None:
        self._data["ports_opened"] = True
        self.save()

    def set_dns_updated(self) -> None:
        self._data["dns_updated"] = True
        self.save()

    def set_refunded(self) -> None:
        self._data["refunded"] = True
        self.save()

    def is_stale(self, current_resource_id: str) -> bool:
        """检查保存的状态是否属于不同的旧资源"""
        return bool(self._data) and self.resource_id != current_resource_id
