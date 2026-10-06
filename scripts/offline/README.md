# 离线版本包(ATERag offline bundle)

一份**可重放、离线可装**的交付物: 同 commit + 同依赖锁 → 同一份包,安装全程不触网。

## 为什么需要它

板卡部署此前依赖两条网络假设: `uv pip install` 拉依赖、`uv export` 解析锁。
目标环境一旦断网(或换一台同架构板卡), 这两步就断了 —— 而代码、数据、
依赖其实都已经齐备, 缺的只是「带着走」的那份轮子。

## 包的构成

```
aterag-offline-<时间戳>-<git-sha>/
  MANIFEST.json        # 每个文件的 sha256 + 版本事实(给人和程序读)
  MANIFEST.sha256      # 同一份摘要, 给 sha256sum -c 读(不依赖包里的代码)
  requirements.lock    # uv.lock 导出的精确版本
  wheelhouse/          # aarch64 轮子(仅二进制, --only-binary :all:)
  app/                 # 源码树(不含 .env / .venv)
  install.sh           # 离线安装, 七步各自对应一种「装到一半才发现不对」
  verify.sh            # 装后自检, 每条对着「装上但没生效」设计
  README.md            # 本文件
```

**为什么清单要两份**: `MANIFEST.sha256` 的存在意义是「包自己损坏时也能发现」——
`sha256sum` 不依赖 python、不依赖包里的任何代码。只有 JSON 清单的话,
校验就得先能跑起包里的代码, 那是循环论证。

## 怎么产出

在**板卡上**组装(轮子必须是 aarch64; 开发机是 Windows x86_64, 交叉编译
带 C/Rust 扩展的轮子不现实):

```sh
cd /opt/aterag
bash scripts/offline/make_bundle.sh <git-sha>
```

产物 `/opt/aterag-bundle/aterag-offline-<时间戳>-<sha>.tar.gz` 及同名 `.sha256`。

## 怎么装(离线)

```sh
tar -xzf aterag-offline-<时间戳>-<sha>.tar.gz
cd aterag-offline-<时间戳>-<sha>
cp /path/to/your.env app/.env      # 密钥不入包
APP_DIR=/opt/aterag bash install.sh
bash verify.sh
```

`install.sh` 的步骤与它们各自要挡的失败:

| 步 | 挡什么 |
|---|---|
| 1 `sha256sum -c` | 包损坏时在装任何东西之前发现 |
| 2 解释器版本 | 缺 CPython ≥ 3.11 时报清楚要什么 |
| 3 `--no-index` 装依赖 | 断网环境唯一的合法装法 |
| 4 应用文件 + `.env` 断言 | `EMBED_DIM` 缺省会让嵌入落到服务端原生维度(实测 2048)而 pgvector 上限 2000, 表现是「装完检索变差」而不是启动失败 |
| 5 `alembic upgrade head` | schema 先于数据 |
| 6 知识装载 + 对账 | 「装上了但没生效」 |
| 7 systemd + 自检 | 服务起来但工具不在册 |

## 已知边界(不藏)

- **wheelhouse 只覆盖当前平台**: aarch64/debian12。换架构要重新组装 ——
  跨架构轮子不能通用, 这不是缺陷是物理事实。
- **`.env` 不入包**: 里面有 API key。`install.sh` 在缺 `.env` 时从
  `.env.example` 生成一份并**停下**要求填密钥, 不带着空密钥往下装。
- **`make_bundle.sh` 需要一次网络**: 下载轮子那一步。装的过程不需要。
- **PG 本身不在包内**: 目标机要自备 PostgreSQL 与 pgvector(与现有部署一致)。