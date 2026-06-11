from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import paramiko

logger = logging.getLogger(__name__)


class SSHClient:
    """管理与远程服务器的 SSH 连接及常用操作 (基于 Paramiko)"""

    def __init__(
        self,
        host: str,
        user: str = "root",
        port: int = 22,
        key_path: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.password = password
        self._client: Optional[paramiko.SSHClient] = None

    def connect(self, timeout: int = 120, interval: int = 10) -> None:
        """带重试机制的 SSH 连接建立"""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict = dict(
            hostname=self.host,
            port=self.port,
            username=self.user,
            timeout=10,
        )
        if self.key_path:
            connect_kwargs["key_filename"] = self.key_path
        if self.password:
            connect_kwargs["password"] = self.password

        deadline = time.time() + timeout
        last_exc: Optional[Exception] = None
        while time.time() < deadline:
            try:
                client.connect(**connect_kwargs)
                self._client = client
                logger.info("SSH 成功连接至 %s@%s:%s", self.user, self.host, self.port)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning("SSH 连接失败 (%s)，将在 %s 秒后重试 …", exc, interval)
                time.sleep(interval)

        raise TimeoutError(
            f"在 {timeout} 秒内无法通过 SSH 连接至 {self.host}。最后一次错误: {last_exc}"
        )

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            logger.debug("SSH 连接已关闭。")

    def __enter__(self) -> "SSHClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def exec(self, command: str, check: bool = True) -> tuple[int, str, str]:
        """在远端服务器执行 Shell 命令"""
        if not self._client:
            raise RuntimeError("SSH 未连接，请先调用 connect() 建立连接")
        logger.debug("SSH 执行命令: %s", command)
        _, stdout, stderr = self._client.exec_command(command, get_pty=True)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        if out:
            logger.debug("stdout: %s", out.strip())
        if err:
            logger.debug("stderr: %s", err.strip())
        if check and code != 0:
            raise RuntimeError(
                f"远端命令执行失败 (退出状态码: {code}): {command}\n{err or out}"
            )
        return code, out, err

    def upload_str(self, content: str, remote_path: str) -> None:
        """通过 SFTP 将字符串上传为远程文件"""
        if not self._client:
            raise RuntimeError("SSH 未连接，请先连接。")
        sftp = self._client.open_sftp()
        try:
            remote_dir = str(Path(remote_path).parent)
            try:
                sftp.stat(remote_dir)
            except FileNotFoundError:
                self.exec(f"mkdir -p {remote_dir}")

            with sftp.open(remote_path, "w") as f:
                f.write(content)
            logger.info("字符串内容成功上传至 %s:%s", self.host, remote_path)
        finally:
            sftp.close()

    def upload_file(self, local_path: str, remote_path: str) -> None:
        """通过 SFTP 上传本地文件"""
        if not self._client:
            raise RuntimeError("SSH 未连接，请先连接。")
        sftp = self._client.open_sftp()
        try:
            remote_dir = str(Path(remote_path).parent)
            try:
                sftp.stat(remote_dir)
            except FileNotFoundError:
                self.exec(f"mkdir -p {remote_dir}")
            sftp.put(local_path, remote_path)
            logger.info("文件上传成功: %s → %s:%s", local_path, self.host, remote_path)
        finally:
            sftp.close()
