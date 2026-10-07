"""产品注册表: 型号 <-> 产品类型(知识域) 映射与查询自动识别.

两级知识隔离的锚点:
  workspace(model)     型号个性化参数
  workspace(_domain_X) 产品类型通用知识 (同类型型号共享, 跨域隔离)

原第三级 ``workspace(_common)``(最小普适内核)已随 common 层一起移除
(2026-10): ``domain_rules/common/`` 删除, 其唯一一条规则 ``K-CMN-001``
(SI 词头换算)并入 ``domain_rules/power/``。留着这一层只会让装配多出
一个恒为空、却仍参与检索过滤与层级标注的 workspace —— 而
``verify_layers.py`` 还得专门断言它有命中。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from aterag.config import Settings


@dataclass
class ProductEntry:
    domain: str
    doc_number: str = ""
    doc_version: str = ""
    #: 本型号用哪份文档档案 (config/doc_profiles.yaml 的 profile 名)。
    #: 留空 = 用 default_profile。**必须落在注册表里而不是靠调用方记得传** ——
    #: 否则档案改了却没有任何测试发现调用方还在用旧的 (方案 §11.4 A19:
    #: PN2000-24A 登记在册却抛 SectionKeywordNotFound, 因为档案选择只存在于
    #: MCP 工具的可选参数里)。
    doc_profile: str = ""
    #: **主输出轨**(如 ``-54V``)。规格书里有些参数行不标注所属轨(整机效率/
    #: 输出功率/待机功耗/开机延迟/负载均流度), 这些行按主轨计(2026-10-07 用户裁定)。
    #:
    #: 为什么是型号级字段而不是 profile 级: 同模板不同型号主轨不同
    #: (PA601-D54A=-54V, PN1000-48A=-48V), 而 profile 是跨型号共享的表结构知识。
    #:
    #: **留空 = 不做主轨归属**, 行保持无轨。刻意**不做自动推断**: 按「带轨行最多者」
    #: 或「额定电流最大者」猜, 猜错会把效率/功率判据挂到不存在的输出路上, 而且
    #: 不报错 —— 宁可缺声明而不猜。
    main_rail: str = ""
    #: 本型号的 PG **schema 名**(如 ``pw_sr5400``)。
    #:
    #: 为什么必须声明而不能用 :func:`storage.schema.model_schema_name` 现算:
    #: 现算规则是「型号键折叠连字符加 ``pw_`` 前缀」, 对 ``PA601-D54A`` 会得到
    #: ``pw_pa601_d54a`` —— 而板卡上实际部署的、装了 31 张表的那个 schema 是
    #: ``pw_sr5400``(初版部署时的型号键是 ``SR5400``)。现算会在同一个产品名下
    #: **再建一份空 schema**, 于是同一型号的数据分散在两个 schema 里, 而两边
    #: 都查得到 —— 正是红线 4 拒绝的双源。
    #:
    #: **留空 = 未声明**: 落库/建表类操作必须报错而不是回退到现算(回退就是上面
    #: 那个双源)。查询类操作可以只告警。
    schema: str = ""


@dataclass
class DomainEntry:
    workspace: str
    kb_status: str = "empty"  # empty | populated


@dataclass
class Registry:
    settings: Settings
    products: dict[str, ProductEntry] = field(default_factory=dict)
    domains: dict[str, DomainEntry] = field(default_factory=dict)
    _path: Path | None = None

    # ---------- 加载 / 持久化 ----------
    @classmethod
    def _resolve_path(cls, settings: Settings) -> Path:
        """定位注册表文件, **找不到就报错**。

        早先一版在路径不存在时 ``data = {}`` 静默返回空注册表, 而配置默认
        ``registry.yaml``(CWD 相对)、文件实际在 ``data/registry.yaml`` ——
        差一个目录, 于是本地永远拿到 ``products=[]`` 且不报错。后续
        ``resolve_query`` 抛的却是 ``UnknownModel: no model identified``,
        把根因藏了两层, 排查时先怀疑查询文本而不是配置路径。

        兜底顺序: 配置路径 -> ``data/<basename>``(本项目其他路径如
        ``annotations_dir="data/annotations"`` 都用 data/ 前缀) -> 报错。
        """
        configured = Path(settings.registry_path)
        if configured.exists():
            return configured
        if configured.is_absolute():
            raise FileNotFoundError(
                f"registry_path 不存在: {configured} (settings.registry_path)"
            )
        fallback = Path("data") / configured.name
        if fallback.exists():
            return fallback
        raise FileNotFoundError(
            f"产品注册表不存在: 试过 {configured} 与 {fallback}。"
            f"注册表决定系统知道哪些型号存在 —— 没有它, ingest/query 两侧"
            f"都拿不到任何型号。部署时应随代码一并提供(已在版本库内)。"
        )

    @classmethod
    def load(cls, settings: Settings) -> Registry:
        path = cls._resolve_path(settings)
        reg = cls(settings=settings, _path=path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, d in (data.get("domains") or {}).items():
            reg.domains[name] = DomainEntry(
                workspace=d.get("workspace", f"{settings.domain_workspace_prefix}{name}"),
                kb_status=d.get("kb_status", "empty"),
            )
        for model_id, p in (data.get("products") or {}).items():
            reg.products[model_id] = ProductEntry(
                domain=p["domain"],
                doc_number=str(p.get("doc_number", "")),
                doc_version=str(p.get("doc_version", "")),
                doc_profile=str(p.get("doc_profile", "")),
                main_rail=str(p.get("main_rail", "")),
                schema=str(p.get("schema", "")),
            )
        return reg

    def save(self) -> None:
        if not self._path:
            return
        data = {
            "domains": {
                name: {"workspace": d.workspace, "kb_status": d.kb_status}
                for name, d in self.domains.items()
            },
            "products": {
                model_id: {
                    "domain": p.domain,
                    "doc_number": p.doc_number,
                    "doc_version": p.doc_version,
                    **({"doc_profile": p.doc_profile} if p.doc_profile else {}),
                    **({"main_rail": p.main_rail} if p.main_rail else {}),
                    **({"schema": p.schema} if p.schema else {}),
                }
                for model_id, p in self.products.items()
            },
        }
        self._path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    # ---------- schema ----------
    def schema_name(self, model_id: str) -> str:
        """本型号的 PG schema 名, **必须已声明**否则报错。

        刻意不提供「没声明就现算」的兜底: 见 :attr:`ProductEntry.schema` 的说明 ——
        现算出的名字与板卡上实际部署的 schema 往往不同, 回退等于在同一型号下
        建第二份数据源(红线 4)。

        报错信息带上已声明的型号, 便于直接看出是「忘了声明」还是「型号名写错」。
        """
        entry = self.products.get(model_id)
        if entry is None:
            raise KeyError(f"型号未注册: {model_id}(在册: {sorted(self.products)})")
        if not entry.schema:
            declared = [k for k, v in self.products.items() if v.schema]
            raise KeyError(
                f"型号 {model_id} 未声明 PG schema 名。"
                f"请在注册表里给该型号加 schema: <名称>(已声明的有: {declared})。"
            )
        return entry.schema

    # ---------- workspace ----------
    def model_workspace(self, model_id: str) -> str:
        return model_id

    def domain_workspace(self, domain: str) -> str:
        """域 -> workspace。**不再对 "common" 特判** —— 那层已删除。"""
        d = self.domains.get(domain)
        if d:
            return d.workspace
        return f"{self.settings.domain_workspace_prefix}{domain}"

    def query_workspaces(self, model_id: str) -> list[str]:
        """型号查询的两层装配: [model, _domain_{type}]。"""
        entry = self.products.get(model_id)
        layers = [self.model_workspace(model_id)]
        if entry:
            layers.append(self.domain_workspace(entry.domain))
        return layers

    # ---------- 导入端: 注册 ----------
    def ensure_domain(self, domain: str, *, create: bool = True) -> DomainEntry:
        if domain not in self.domains:
            if not create:
                raise KeyError(f"unknown domain: {domain}")
            self.domains[domain] = DomainEntry(
                workspace=f"{self.settings.domain_workspace_prefix}{domain}",
                kb_status="empty",
            )
        return self.domains[domain]

    def register_product(
        self,
        model_id: str,
        domain: str,
        *,
        doc_number: str = "",
        doc_version: str = "",
        main_rail: str = "",
    ) -> None:
        self.ensure_domain(domain)
        self.products[model_id] = ProductEntry(
            domain=domain,
            doc_number=doc_number,
            doc_version=doc_version,
            main_rail=main_rail,
        )
        self.save()

    def domain_populated(self, domain: str) -> bool:
        d = self.domains.get(domain)
        return bool(d and d.kb_status == "populated")

    def set_domain_populated(self, domain: str) -> None:
        self.ensure_domain(domain).kb_status = "populated"
        self.save()

    # ---------- 查询端: 型号自动识别 ----------
    def match_model_in_text(self, text: str) -> list[str]:
        """从查询文本中识别已注册型号。

        匹配策略: 已注册型号优先 (大小写不敏感); 未注册时尝试通用
        型号模式 (大写字母-数字段, 如 PA601-D54A) 作为候选提示。
        """
        candidates: list[str] = []
        upper = text.upper()
        for model_id in self.products:
            if model_id.upper() in upper:
                candidates.append(model_id)
        if candidates:
            return candidates
        # 通用模式提示: XXX000-D12B 形态
        hints = re.findall(r"\b[A-Z]{2,8}\d[A-Z0-9]*(?:-[A-Z0-9]+)+\b", upper)
        return hints

    def resolve_query(self, text: str, model_id: str | None = None) -> ResolvedQuery:
        """查询解析链: 显式 model_id > 文本识别。

        raises:
          UnknownModel: 完全无法识别 (fail-closed)
          AmbiguousModel: 多候选需消歧
        """
        if model_id:
            if model_id not in self.products:
                raise UnknownModel(f"model not registered: {model_id}")
            return ResolvedQuery(
                model_id=model_id,
                domain=self.products[model_id].domain,
                source="explicit",
            )
        candidates = self.match_model_in_text(text)
        registered = [c for c in candidates if c in self.products]
        if len(registered) == 1:
            mid = registered[0]
            return ResolvedQuery(
                model_id=mid,
                domain=self.products[mid].domain,
                source="inferred",
            )
        if len(registered) > 1:
            raise AmbiguousModel(
                f"multiple models matched {registered}; specify model_id explicitly"
            )
        raise UnknownModel(
            f"no model identified from query: {text[:80]!r}; "
            f"registered models: {sorted(self.products) or '(none)'}"
        )


@dataclass
class ResolvedQuery:
    model_id: str
    domain: str
    source: str  # explicit | inferred


class UnknownModel(KeyError):
    pass


class AmbiguousModel(ValueError):
    pass
