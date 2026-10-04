"""产品注册表: 型号 <-> 产品类型(知识域) 映射与查询自动识别.

三级知识隔离的锚点:
  workspace(model)     型号个性化参数
  workspace(_domain_X) 产品类型通用知识 (同类型型号共享, 跨域隔离)
  workspace(_common)   最小普适内核
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
        common = (data.get("common") or {}).get("workspace", settings.common_workspace)
        reg._common_workspace = common
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
            )
        return reg

    def save(self) -> None:
        if not self._path:
            return
        data = {
            "common": {"workspace": self._common_workspace},
            "domains": {
                name: {"workspace": d.workspace, "kb_status": d.kb_status}
                for name, d in self.domains.items()
            },
            "products": {
                model_id: {
                    "domain": p.domain,
                    "doc_number": p.doc_number,
                    "doc_version": p.doc_version,
                }
                for model_id, p in self.products.items()
            },
        }
        self._path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    # ---------- workspace ----------
    def model_workspace(self, model_id: str) -> str:
        return model_id

    def domain_workspace(self, domain: str) -> str:
        if domain == "common":
            return self._common_workspace
        d = self.domains.get(domain)
        if d:
            return d.workspace
        return f"{self.settings.domain_workspace_prefix}{domain}"

    @property
    def common_workspace(self) -> str:
        return self._common_workspace

    def query_workspaces(self, model_id: str) -> list[str]:
        """型号查询的三层装配: [model, _domain_{type}, _common]。"""
        entry = self.products.get(model_id)
        layers = [self.model_workspace(model_id)]
        if entry:
            layers.append(self.domain_workspace(entry.domain))
        layers.append(self.common_workspace)
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
    ) -> None:
        self.ensure_domain(domain)
        self.products[model_id] = ProductEntry(
            domain=domain, doc_number=doc_number, doc_version=doc_version
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
