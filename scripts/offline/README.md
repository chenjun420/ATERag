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
  requirements.lock    # uv.lock 导出的精确版本, **每条都带 sha256**
  INDEX_USED           # 轮子实际取自哪个源(排障第一线索)
  wheelhouse/          # aarch64 轮子(仅二进制, --only-binary :all:)
  app/                 # 源码树(不含 .env / .venv)
  install.sh           # 离线安装, 七步各自对应一种「装到一半才发现不对」
  verify.sh            # 装后自检, 每条对着「装上但没生效」设计
  README.md            # 本文件
```

**为什么清单要两份**: `MANIFEST.sha256` 的存在意义是「包自己损坏时也能发现」——
`sha256sum` 不依赖 python、不依赖包里的任何代码。只有 JSON 清单的话,
校验就得先能跑起包里的代码, 那是循环论证。

## 下载为什么走镜像, 而完整性仍由仓库担保

板卡实测(2026-10-06): 直连 `files.pythonhosted.org` 时**索引页 2.3 MB/s,
但轮子文件本身只有 7~23 kB/s** —— 索引快、文件慢是这个链路的实况。
227 MB 的 wheelhouse 跑了两个多小时没下完, 而且日志停在
`Downloading <当前包>` 那一行, **看上去像网络慢, 实际是龟速**。

清华 tuna 镜像托管的是文件本身, 实测 20~29 MB/s: 227 MB / 116 个轮子
**90 秒**下完。所以 `make_bundle.sh` 默认走镜像。

**换源不等于把包内容交给镜像。** `requirements.lock` 里每条依赖都带
uv.lock 的 sha256, 下载走 `pip download --require-hashes`, 逐个校验。
实测把一个已下载的轮子尾部追加 15 字节再让它装, pip 报
`THESE PACKAGES DO NOT MATCH THE HASHES` 并以 rc=1 退出。
镜像只提供带宽, 内容对不对由仓库里已审过的锁说了算。

`make_bundle.sh` 在导出后有一道断言: 锁里没有 sha256 就**拒绝组装**。
不带 hash 的锁从第三方源取包, 那不是校验, 是信任 —— 这条要在组装阶段
响亮地坏掉, 而不是悄悄降级成无校验下载。

阿里云镜像也快(ortools 27.6 MB 实测 12 MB/s), 但**缺** psycopg-binary
与 scikit-learn 的 aarch64 轮子 —— 镜像覆盖度不是想当然的, 所以不进
默认列表。取包源用 `PYPI_INDEX` 覆盖, 缺轮子时 pip 会报出来。

## 怎么产出

在**板卡上**组装(轮子必须是 aarch64; 开发机是 Windows x86_64, 交叉编译
带 C/Rust 扩展的轮子不现实):

```sh
cd /opt/aterag
bash scripts/offline/make_bundle.sh <git-sha>
# 可选: PYPI_INDEX=https://pypi.org/simple bash scripts/offline/make_bundle.sh <sha>
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

装到**非默认路径**或在一台正跑着 aterag 的机器上验证本包时, 加 `SYSTEMD_SKIP=1`:
那三个 service 单元的 `WorkingDirectory` / `ExecStart` / `EnvironmentFile`
**全写死 `/opt/aterag`**, 不跳过就会用演练目录的单元重启线上服务。跳过时
脚本会明说跳过了, 不静默略过。

`install.sh` 的步骤与它们各自要挡的失败:

| 步 | 挡什么 |
|---|---|
| 1 `sha256sum -c` | 包损坏时在装任何东西之前发现 |
| 2 解释器版本 | 缺 CPython ≥ 3.11 时报清楚要什么 |
| 3 `--no-index` 装依赖 | 断网环境唯一的合法装法; 锁里的 sha256 在这一步再校验一次 |
| 4 应用文件 + `.env` 断言 | `EMBED_DIM` 缺省会让嵌入落到服务端原生维度(实测 2048)而 pgvector 上限 2000, 表现是「装完检索变差」而不是启动失败 |
| 5 `alembic upgrade head` | schema 先于数据 |
| 6 领域知识**前置条件核对** + 与库对账 | 规则源缺失(联网补装载必然失败);已装载则报条数, 未装载则**明说**并给出补装载命令 |
| 7 systemd + 自检 | 服务起来但工具不在册 |

## 已知边界(不藏)

- **wheelhouse 只覆盖当前平台**: aarch64/debian12。换架构要重新组装 ——
  跨架构轮子不能通用, 这不是缺陷是物理事实。
- **轮子数少于锁定包数是正常的**: 锁里有 `sys_platform == 'win32'`、
  `python_full_version >= '3.12'` 这类条目, 在本平台被环境标记筛掉。
  板卡实测 锁定 126 / 实得 116。`make_bundle.sh` 只断言「一个轮子都没拿到
  就拒绝继续」, 不去断言两者相等 —— 那会把平台的正常筛除当成故障。
- **`.env` 不入包**: 里面有 API key。`install.sh` 在缺 `.env` 时从
  `.env.example` 生成一份并**停下**要求填密钥, 不带着空密钥往下装。
- **`make_bundle.sh` 需要一次网络**: 下载轮子那一步。装的过程不需要。
- **领域知识不在包内, 离线也装不了**: 每个 chunk 必须带向量入库, 而向量
  唯一来源是 `EmbeddingClient.embed()` —— 它无条件 POST 供应商 HTTP
  (`src/aterag/models/embed_client.py`), **没有离线路径**。所以离线装完
  `_domain_power` 仍是空的, 此时 `search_cases` 的 domain 层**恒空**, 而
  工具照常返回结果, 看起来一切正常。`install.sh` 第 6 步与 `verify.sh`
  第 3 步都会把这件事**显式报出来**(前者给命令, 后者判 FAIL)。
  联网后补装载:

  ```sh
  cd /opt/aterag && ./.venv/bin/python scripts/build_domain.py power
  ```

  该命令**幂等**(先清空该 workspace 再整批写), 重复执行安全。
- **`.env` 的行尾会被严格对待**: `install.sh` 不再用 shell 解析 `.env`,
  配置一律问应用自己(pydantic-settings)。实测板卡 `.env` 是 CRLF 行尾,
  `grep -E | cut -d=` 取值会把 `\r` 带进路径(`domain_rules\r/power`),
  于是目录不存在而安装中断; 而同一份文件 pydantic-settings 读出的是
  `domain_rules`。同一配置两个值 —— 所以第二份解析器必须删掉, 不是加
  `tr -d`。
- **PG 本身不在包内**: 目标机要自备 PostgreSQL 与 pgvector(与现有部署一致)。
