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


def _yaml_scalar(value: object) -> str:
    """标量的 YAML 字面量形态(与 ``safe_dump`` 写标量时的输出一致)。

    走 ``safe_dump([value])`` 而不是 ``safe_dump({value: None})``: 后者产出的是
    flow 映射(``{key: null}``), 按第一个 ``:`` 切会切在错误的位置, 得到
    ``null}`` 这种垃圾 —— 而且它只在真被调用时才炸, 属于最难查的那类。
    单元素列表的 dump 去掉两侧方括号后就是标量本身。
    """
    dumped = yaml.safe_dump([value], allow_unicode=True, default_flow_style=True).strip()
    assert dumped.startswith("[") and dumped.endswith("]"), dumped
    return dumped[1:-1].strip()


def _parse_registry_blocks(lines: list[str]) -> tuple[list, list] | None:
    """把 YAML 行解析成 ``(文件头, [(段名, [entry 块])])``; 形态不符返回 None。

    entry 块 = ``(条目名, 前导注释行, [(字段名, 值文本, 缩进, 字段前注释)])``。
    注释按「归属于其后第一个字段」切分 —— 段名/条目名前的注释归条目, 字段前的
    注释归该字段。
    """

    def ind(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    head: list[str] = []
    sections: list[tuple[str, list]] = []
    cur_sec: list | None = None
    cur_entry: list | None = None
    pending: list[str] = []

    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            pending.append(line)
            continue
        d = ind(line)
        if d == 0:
            if not s.endswith(":"):
                return None
            cur_sec = [s[:-1], [], pending]
            pending = []
            sections.append(cur_sec)
            cur_entry = None
            continue
        if cur_sec is None:
            return None
        if d == 2:
            if not s.endswith(":"):
                return None
            cur_entry = [s[:-1], [], pending, []]
            pending = []
            cur_sec[1].append(cur_entry)
            continue
        if d >= 4:
            if cur_entry is None or ":" not in s:
                return None
            k = s.partition(":")[0].strip()
            # 存**整行原文**而不只是值: 值没变时要原样写回, 否则
            # ``main_rail: '-54V'`` 会被重新序列化成 ``main_rail: -54V`` ——
            # 语义等价, 但每次导入都在 diff 里留一道无意义的改动, 而这个文件
            # 是每次导入都会重写的。
            cur_entry[3].append([k, line, d, pending])
            pending = []
            continue
        return None
    return head, sections


def _same_yaml_value(raw_line: str, new_value: object) -> bool:
    """这一行的**当前值**是否已经等于新值 —— 是则整行原样保留。

    比解析后的值而不是比重新序列化后的文本: ``main_rail: '-54V'`` 与
    ``main_rail: -54V`` 解析后同值, 但文本不同。这个文件每次导入都会被重写,
    按文本比就会让每轮部署的 diff 里都留下一道无意义的引号改动 —— 而 diff 一旦
    被习惯性忽略, 真改动也会跟着被扫掉。

    ``safe_load`` 吃整行得到的是 ``{字段名: 值}``, 所以要取出那个值再比; 取不到
    (行形态异常) 时按「变了」处理, 宁可多改一次也不漏。
    """
    try:
        parsed = yaml.safe_load(raw_line)
    except yaml.YAMLError:
        return False
    if not isinstance(parsed, dict) or len(parsed) != 1:
        return False
    return next(iter(parsed.values())) == new_value


def _rewrite_yaml_preserving_comments(path: Path, data: dict) -> tuple[str, bool]:
    """按新数据重建 YAML, 保留注释/空行/键序。

    做法: **按 entry 分块重建**。每个 entry 在自己的块内补齐缺失字段, 整块一次
    发出。早期版本把「文件里没有的字段」统一追加到文件末尾 —— 那在两段文件上
    必然出错: ``kb_status`` 会落到 ``products`` 段下面, 而 YAML 靠缩进表达归属,
    于是字段跑错了段, ``main_rail`` 之类的声明随之消失。

    返回 ``(文本, 是否处理得了)``; False = 形态超出本函数能力, 调用方退回整体重写。
    """
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", False

    parsed = _parse_registry_blocks(original.split("\n"))
    if parsed is None:
        return "", False
    head, sections = parsed

    for sec_name, entries, _lead in sections:
        if sec_name not in (data or {}):
            return "", False  # 文件里的段在数据里没有 -> 形态不符, 别猜
        for entry in entries:
            if entry[0] not in data[sec_name]:
                return "", False

    out: list[str] = list(head)
    #: 原文件是否以换行结尾。写回时必须保持 —— POSIX 文本文件约定末尾有换行,
    #: 少了它 `cat`/`git diff`/部分 YAML 工具会提示 "No newline at end of file",
    #: 而这个文件是**每次导入都会被重写**的, 所以这个瑕疵会反复出现在 diff 里。
    had_trailing_nl = original.endswith("\n")

    def emit_entry(lead: list[str], entry_name: str) -> None:
        out.extend(lead)
        out.append(f"  {entry_name}:")

    written: set[tuple[str, str, str]] = set()

    for sec_name, entries, _lead in sections:
        out.append(f"{sec_name}:")
        data_entries = data[sec_name]
        for entry_name, lead, _fields, field_rows in entries:
            emit_entry(lead, entry_name)
            seen: set[str] = set()
            for k, raw, d, comments in field_rows:
                if k not in data_entries[entry_name]:
                    # 文件里有、数据里没有 -> 删。注释一并带走: 注释是写给这个
                    # 字段的, 字段没了还留着会让人以为声明仍然有效。
                    continue
                out.extend(comments)
                new_value = data_entries[entry_name][k]
                if _same_yaml_value(raw, new_value):
                    out.append(raw)  # 值没变 -> 原样保留(引号等写法不动)
                else:
                    out.append(" " * d + f"{k}: {_yaml_scalar(new_value)}")
                seen.add(k)
                written.add((sec_name, entry_name, k))
            # **在块内补齐** —— 这是与早期版本的关键差别
            for k, v in data_entries[entry_name].items():
                if k not in seen:
                    out.append(f"    {k}: {_yaml_scalar(v)}")
                    written.add((sec_name, entry_name, k))

    # 追加文件里完全没有的段与条目
    for sec_name, data_entries in (data or {}).items():
        if not any(s == sec_name for s, _e, _l in sections):
            out.append("")
            out.append(f"{sec_name}:")
        known = {e for s, es, _l in sections if s == sec_name for e, _l2, _fl, _f in es}
        for entry_name, fields in data_entries.items():
            if entry_name not in known:
                out.append(f"  {entry_name}:")
                for k, v in fields.items():
                    out.append(f"    {k}: {_yaml_scalar(v)}")

    text = "\n".join(out)
    if had_trailing_nl and not text.endswith("\n"):
        text += "\n"
    return text, True


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
            raise FileNotFoundError(f"registry_path 不存在: {configured} (settings.registry_path)")
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
        """落盘。**只改值, 不动注释与键序。**

        曾经直接 ``yaml.safe_dump`` 整个 dict 覆盖文件, 后果是每次保存都把
        注册表里的**全部注释**抹掉 —— 而那些注释不是装饰: ``main_rail`` 上面
        写着「为什么不自动推断」、``schema`` 上面写着「为什么必须声明而不是现算」。
        抹掉之后文件仍然语法正确、字段齐全, 读代码的人却再也看不到依据, 只剩下
        一行看起来可以随便改的配置。

        所以这里做**行级原地更新**: 只重写那些值确实变了的行, 其余字节原样保留。
        注释、空行、键序、字段顺序全部不动。

        值多行的情况(本文件实际都是单行标量)退回整体重写 —— 那时注释已经保不住,
        但字段不会丢, 属可接受降级。
        """
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
        if not self._path.exists():
            self._path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            return
        preserved, complete = _rewrite_yaml_preserving_comments(self._path, data)
        if not complete:
            # 有多行结构, 行级改写会算错缩进 —— 退回整体重写(丢注释, 不丢字段)
            self._path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
        else:
            self._path.write_text(preserved, encoding="utf-8")

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
        doc_number: str | None = None,
        doc_version: str | None = None,
        main_rail: str | None = None,
        schema: str | None = None,
        doc_profile: str | None = None,
    ) -> None:
        """登记/更新一个型号, 然后落盘。

        **重入不得摧毁已声明的字段。** 整体替换 :class:`ProductEntry` 时, 调用方
        不传 ``main_rail``/``schema``/``doc_profile`` 就会把它们清成 ``""`` ——
        而 ``ingest_spec`` 只传 ``doc_number``/``doc_version``, 所以**每跑一次
        导入, 注册表里的主轨与 schema 声明就被抹一次**, 且文件看起来完好无损。

        这个抹除是静默且不可逆的: 主轨一没, 所有未标注轨的参数行都不再归轨
        (``entity_extract.resolve_main_rail`` 直接返回空); schema 一没,
        :meth:`schema_name` 转为抛错, 数据落在别处。

        所以这五个可选参数一律用 ``None`` 表示「调用方没提」, 与「显式给了值」
        分开:

        * 传 ``None``  -> 保留原值(已存在) / 空(新条目)
        * 传 ``""``    -> **显式清空**(想撤掉一个声明就是这么写的)
        * 传非空值     -> 覆盖

        早先用 ``""`` 当默认值, 于是「清空」与「没提」无法区分, 保留逻辑只能写成
        ``x or old`` —— 那会把显式清空也一起吃掉, 想撤声明就没路了。
        """
        self.ensure_domain(domain)
        old = self.products.get(model_id)

        def pick(new: str | None, fallback: str) -> str:
            if new is not None:
                return new
            return fallback

        self.products[model_id] = ProductEntry(
            domain=domain,
            doc_number=pick(doc_number, old.doc_number if old else ""),
            doc_version=pick(doc_version, old.doc_version if old else ""),
            main_rail=pick(main_rail, old.main_rail if old else ""),
            schema=pick(schema, old.schema if old else ""),
            doc_profile=pick(doc_profile, old.doc_profile if old else ""),
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
