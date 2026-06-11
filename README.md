# VMProxy CLI

统一的代理主机初始化与自动化轮转命令行运维工具。

## 🌟 核心特性
- **一键初始化 (`vmproxy init`)**：从零起步，自动购买轻量主机、设置防火墙、同步 Cloudflare DNS 解析、通过 SSH 进行主机系统优化配置、安装证书与 sing-box 服务、发布 Base64 订阅。
- **自动轮换 (`vmproxy rotate`)**：为旧主机打快照镜像、释放冗余镜像配额、开通新实例、同步配置、更新 DNS 指向、释放旧主机资源并回写配置，实现主机的无缝轮换。
- **一键拆除 (`vmproxy teardown`)**：快速通知云商释放主机资源并发起退订退款，同步下线对应的 DNS 解析记录。

## 🚀 快速开始
```bash
# 安装依赖并执行预览
uv run python main.py init --config config.example.yaml --dry-run
```
