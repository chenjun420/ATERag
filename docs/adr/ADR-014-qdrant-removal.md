# ADR-014 移除 Qdrant，检索层改由 pgvector 承担

- 状态: **Completed** (2026-10-07 全量落地；此前为 Accepted 中间态)
- 日期: 2026-10-03
- 关联: V6.0 §1.7（ADR-002）、§2.3、§3.5.2；ADR-013

## 落地记录 (2026-10-07)

W3 验收完成后按本 ADR 全量执行：

- 代码：`Settings.retrieval_backend` / `qdrant_url` / `qdrant_collection` 字段删除；
  `checks.py` 的 Qdrant 健康检查删除；`pipeline.py` 的 `_legacy_qdrant` /
  `_ensure_qdrant_legacy` / `legacy_vector_search` 删除；`rag/service.py` 的
  Qdrant 分支删除（RagService 保留，检索只走 `retrieval/hybrid.py`）。
- 依赖：`qdrant-client` 自 `pyproject.toml` 移除；`mypy` 豁免清单同步。
- 部署：`deploy/qdrant/`、`deploy/native/03-qdrant.sh` 删除；
  `docker-compose.yml` 的 qdrant 服务删除；板卡上 qdrant systemd 服务停用 +
  `/opt/qdrant`（二进制与数据盘）删除；`.env` 的 `QDRANT_*` 行清除。
- 过时验证脚本：`verify_lightrag_deployed.py` 删除（其验证的 lightrag_vdb_*
  表与 lightrag_vectors 集合均已是历史形态，超集由 `verify_board_rag.py` 承担）。
- 回归：`tests/test_retrieval_hybrid.py` 的不变量测试升级为「Settings 不得
  残留 qdrant/retrieval_backend 字段」——第二套向量存储从配置层就构造不出来。

## 背景

ADR-002 要求全栈统一 PostgreSQL、单一存储底座。ATERag 当前部署**违反**了这条：

- `deploy/native/03-qdrant.sh` 安装并拉起 Qdrant 服务
- `deploy/docker-compose.yml` 含 qdrant 服务
- `deploy/qdrant/init_tenant.py` 建 `lightrag_vectors` 集合
- `pyproject.toml` 声明 `qdrant-client>=1.12`
- `rag/service.py` 走 **Qdrant 预过滤 + PG BM25 + RRF** 融合路径
- `pyproject.toml` 注释自述「换 embedding 要重建 Qdrant」
  （`DELIVERY-REPORT.md:249`）

即 Qdrant 不是「冗余的第二套东西」，而是**当前检索路径的实际承载者**。

同时 PostgreSQL 侧的 `vector` 扩展**已装、已用**（`deploy/postgres/initdb/01-extensions.sql:4`
创建扩展，LightRAG 的 `PGVectorStorage` 也在用它）。也就是说能力已经具备，
只是业务检索没走。

删依赖不能只看 `pyproject.toml`。真正的成本在 `rag/service.py`。

## 决策

1. 移除 Qdrant 全部痕迹：`qdrant-client` 依赖、`deploy/native/03-qdrant.sh`、
   `deploy/qdrant/`、`docker-compose.yml` 的 qdrant 服务、`Settings.qdrant_*`、
   `checks.py` 的 Qdrant 健康检查。
2. 检索层由 `retrieval/hybrid.py` 承担，pgvector + pg_textsearch BM25 + RRF，
   **融合算法与原 `rag/service.py` 保持同一形态**（先向量预过滤再 BM25 再 RRF），
   避免「换存储」被顺手换成「换检索策略」。
3. 分两步落地：W0 先建 `retrieval/hybrid.py` 并让新旧路径都能跑；
   A-W3 完成新路径验收后，再删 `src/aterag/rag/service.py` 与 Qdrant 部署脚本。
   **中间态允许两套并存，不允许出现「已删依赖但代码仍 import」的破损状态。**

## 理由

备选方案与否决理由：

| 备选 | 否决理由 |
|---|---|
| 保留 Qdrant 作为主检索，PG 只做 BM25 | 直接违反 ADR-002。而且是当前最痛的一个矛盾点：两套存储意味着两套备份、两套故障域、两套凭据管理，而 `DELIVERY-REPORT.md` 已记录「换 embedding 必须重建 Qdrant」——这类运维负担正是单存储底座要消除的。 |
| 直接删 `qdrant-client` 与部署脚本 | 会让 `rag/service.py` 在 import 期就炸，且 `pyproject.toml:34-39` 已写明 pgvector/asyncpg 是被 LightRAG 间接 import 的——「本仓库 0 import 就删」的审计规则在这里会误判（该注释就是上次踩坑后补的）。必须先建替代路径再删。 |
| 换成 FAISS/Chroma | 进程内索引，无法与 PG 事务共享 ACID（违反方案原则四），且多进程部署要各自加载一份。 |

## 影响

- 正面:
  - 消除 ADR-002 违规；备份/故障域从两套收敛到一套。
  - 向量与业务数据同库同事务，`fact` 与其 embedding 可以原子写入
    （当前跨 Qdrant + PG 写入无原子性，只能靠 `namespace` 软隔离）。
  - 租户隔离从 Qdrant 的 `is_tenant=True` 单字段过滤
    （`deploy/qdrant/init_tenant.py:41-50`）升级为 PostgreSQL RLS，
    由数据库强制（ADR-003 + §5.8）。
- 负面 / 代价:
  - HNSW 检索吞吐低于 Qdrant。规格书库规模（每型号百级 chunk 量级）远未触及
    pgvector 的瓶颈区间，可接受。若日后语料量级上到百万 chunk，
    需要重新评估分区与索引参数，而不是偷偷把 Qdrant 加回来。
  - A-W3 之前有两套检索路径并存，需要在 UI/日志上标明当前用的是哪套，
    避免排查时搞混。
- 后续需要做的:
  - A-W3 完成后执行删除，并按 ADR-013 把 `embed_dim` 单一化。
  - `deploy/docker-compose.yml` 若只余 PG + 应用，需重写或废弃
    （现有文件同时含 qdrant 与 board 部署痕迹）。
  - `DELIVERY-REPORT.md` 与 `README.md` 中的 Qdrant 架构图需要更新。