# 🚀 VMProxy CLI

[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![GitHub CLI](https://img.shields.io/badge/gh--cli-supported-purple)](https://cli.github.com/)

**VMProxy CLI** 是一款面向个人与中小型团队的**统一代理主机初始化与自动化无感轮换命令行运维工具**。支持一键式的轻量 VM 采购配置、DNS 解析注入、系统优化服务部署，并基于快照/镜像技术实现生产主机的无缝轮换和资源的高效回收。

---

## 🌟 核心特性

* **一键初始化 (`vmproxy init`)**
  从零开始，自动完成：云商主机采购 ➜ API 重置管理员密码 ➜ 开通防火墙端口 ➜ Cloudflare DNS A 记录同步 ➜ SSH 远端优化（配置虚拟内存、BBR 调优、安装证书等） ➜ sing-box 代理服务自启部署 ➜ Base64 订阅分发。
* **自动化轮换 (`vmproxy rotate`)**
  通过旧主机制备自定义镜像 ➜ 采用该快照镜像跨周期开通新实例（环境无缝继承） ➜ 防火墙与 DNS 自动指向切换 ➜ 提请退款释放旧主机 ➜ 回写配置，实现成本与 IP 活性的最佳平衡。
* **安全幂等断点续跑**
  为 `init` 与 `rotate` 设计了完全幂等的进度保存机制（`.proxy_init_state.json` 与 `.rotation_state.json`）。发生任何接口或网络异常中断，重新运行将**自动跳过已成功步骤**继续。并且将旧实例的退款释放状态也纳入校验，彻底解决重复提请退订带来的云商 400 状态报错。
* **精细化状态查询 (`--status`)**
  无需干跑或翻阅日志，可使用 `--status` 快速查看当前初始化或轮换任务的详细执行断点，高颜值列表清晰展示每一步的完成情况。
* **一键拆除与退订 (`vmproxy teardown`)**
  快速清理 DNS 记录并同步提请退款释放云端主机资源，支持配置驱动与域名多级合并，防止资源冗余浪费。

---

## 🛠️ 多层配置覆盖系统 (配置优先级说明)

VMProxy CLI 支持极高灵活性的**全局 -> 组件/子命令 -> 命令行参数**多级参数覆盖，让您可以灵活应对各种场景：

### 1. 域名与 `cert_domain` 自动解析
* **免去单独配置**：配置文件中的 `cert_domain` 允许留空，在代码逻辑中会自动识别并使用当前**生效域名列表中的第一个域名**作为证书申请域名。
- **生效域名优先级**：命令行 `--domains` / `-d` ➔ 专属模块 `init.domains` (或 `rotation.domains` / `teardown.domains`) ➔ 全局默认 `dns.domains`。

### 2. 节点前缀 `node_prefix` 多级合并
用于渲染节点订阅链接时的名称前缀，其覆盖优先级由高到低依次为：
1. **命令行参数**：`--node-prefix`
2. **专属组件配置**：`init.subscribe.node_prefix`
3. **专属简写配置**：`init.node_prefix`
4. **全局参数配置**：全局根节点的 `node_prefix`
5. **代码硬兜底**：`"node"`

### 3. `teardown` 专用配置
`teardown` 命令的参数不再依赖其他步骤的残留逻辑，遵循极简、安全的原则：
- **`instance_id`**：命令行参数 `--instance-id` > 专属配置 `teardown.instance_id`。如果两者均留空，则**直接报错拦截**，确保不会意外误删其它历史任务的主机。
- **`domains`**：命令行参数 `--domains` > 专属配置 `teardown.domains` > 降级使用 `rotation.domains` > `init.domains` > 全局 `dns.domains`。

---

## 🚀 快速开始

### 1. 安装环境与依赖
推荐使用极其快速的 [uv](https://github.com/astral-sh/uv) 运行：
```bash
# 获取源码
git clone git@github.com:nothing248/vmproxy-cli.git
cd vmproxy-cli/merge

# 安装依赖并运行帮助信息
uv run python main.py --help
```

### 2. 准备配置文件与敏感凭证
拷贝配置模版并创建 `.env` 文件：
```bash
cp config.example.yaml config.yaml
touch .env
```
在 `.env` 中填入云商与 Cloudflare 的访问凭证（为保障安全，工具会自动加载读取）：
```ini
ALIYUN_ACCESS_KEY_ID=your_aliyun_access_key
ALIYUN_ACCESS_KEY_SECRET=your_aliyun_access_secret
CF_API_TOKEN=your_cloudflare_api_token
CF_ZONE_ID=your_cloudflare_zone_id
CF_ACCOUNT_ID=your_cloudflare_account_id
```

---

## 📖 命令行使用指南

### 1. 初始化代理主机 (`init`)
```bash
# 执行预览模式 (Dry Run)：仅在本地渲染并打印模板，不发出任何 API 请求
uv run python main.py init --dry-run

# 正式开始初始化
uv run python main.py init

# 覆盖订阅前缀并执行
uv run python main.py init --node-prefix jp-tokyo

# 查看当前断点执行状态
uv run python main.py init --status

# 重置断点状态并彻底从第一步重新开始
uv run python main.py init --reset-state
```

### 2. 自动化主机轮转 (`rotate`)
```bash
# 查看轮换执行状态
uv run python main.py rotate --status

# 开始自动轮换 (如果上一次轮换已跑完，建议携带 --reset-state 开始新一轮)
uv run python main.py rotate --reset-state
```
> **关于 `rotate` 的 `--reset-state` 重置逻辑：**
> 在携带 `--reset-state` 启动新一轮轮转时，程序会**在最开始**清理上一次已保存的轮换断点进度状态文件（`.rotation_state.json`），以便开启本轮全新阶段的轮转。

### 3. 服务器回收拆除 (`teardown`)
```bash
# 自动使用配置文件 teardown 专属项拆除机器并删除 DNS 记录
uv run python main.py teardown

# 手动指定特定实例 ID 与域名执行拆除
uv run python main.py teardown --instance-id c574f5afcc4d484a82ba1be03519360b -d jp.example.com

# 仅删除 DNS 记录，保留/不退订机器
uv run python main.py teardown -i c574f5afcc4d484a82ba1be03519360b --skip-refund

# 仅退订释放机器，保留/不清理 DNS 记录
uv run python main.py teardown -i c574f5afcc4d484a82ba1be03519360b --skip-dns
```

---

## 📝 配置文件结构 (`config.example.yaml`)

具体多级字段和各子命令参数模版定义，请直接参阅项目目录下的 [config.example.yaml](file:///Users/nickyang/Documents/projetcs/self/vmproxy-cli/merge/config.example.yaml)。

---

## 🤝 许可证
本项目采用 [MIT License](LICENSE) 许可协议。
