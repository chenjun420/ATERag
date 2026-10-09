"""模板身份与漂移门禁 (方案 §4.0) 的不变量测试。

定位必须先说清: 门禁是**辅助工具, 不是防线**。抽取过程本身对模板失配 fail-closed
(红线 12: 章节选不中抛 SectionKeywordNotFound, 表头不认识抛 TableSchemaUnmapped)。
所以这里钉的不是「门禁能挡住所有模板问题」—— 它挡不住, 也不假装能; 钉的是三件
更朴素的事:

1. **指纹只对语义敏感**: 改文案不改指纹, 改抽取参数一定改指纹。指纹若对文案
   敏感, 每改一句注释就要重采全部基线, 久了没人采, 基线就烂了。
2. **产品里记着模板身份**: 事后能反查历史产物用的哪套参数, 且缺 ``template_id``
   时**报错**而不是静默填空。
3. **「测不了」不等于「没漂移」**: 型号没入库时报 ERROR 而不是 PASS。这条最要紧
   —— 报 PASS 等于告诉人「查过了没问题」, 而实际上什么都没查。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from template_drift import (  # noqa: E402
    SECTION_TOLERANCE,
    TOLERANCE,
    check,
    diff_by_section,
    diff_metrics,
    load_baseline,
    render,
    update,
)

from aterag.extract.api import DocProfile, ProfileBook, extract_test_conditions  # noqa: E402
from aterag.extract.assembler import PatternBook  # noqa: E402
from aterag.extract.configs import (  # noqa: E402
    FINGERPRINT_EXCLUDED_KEYS,
    role_vocabulary,
    template_fingerprint,
    template_identity,
)

DP = "config/doc_profiles.yaml"
BASELINE = "config/template_baseline.yaml"


@pytest.fixture(scope="module")
def prof_book():
    return ProfileBook.load(DP)


@pytest.fixture(scope="module")
def kinds():
    return frozenset(PatternBook.load("config/condition_patterns.yaml").kinds)


@pytest.fixture(scope="module")
def fields():
    from aterag.ingest.table_schema import load_registry

    return sorted(load_registry("config/table_schemas.yaml").known_fields)


def fp(profile, prof_book, kinds, fields) -> str:
    return template_fingerprint(
        profile,
        known_kinds=kinds,
        known_roles=role_vocabulary(prof_book),
        schema_fields=fields,
    )


class TestFingerprintSensitivity:
    """指纹的敏感性必须**窄**: 只对会改变抽取结果的改动敏感。"""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("description", "换一句说明"),
            ("note", "换一条注释"),
            ("review_dispositions", ({"req_id": "X", "kind": "parameter"},)),
            ("name", "renamed_profile"),
        ],
    )
    def test_volatile_fields_do_not_change_fingerprint(
        self, prof_book, kinds, fields, field, value
    ):
        """文案与人签字的处置记录**不该**让指纹变。

        处置记录尤其重要: 它随评审推进而增, 若进指纹则每签一条判据就等于换了一
        版模板, 基线会天天红 —— 天天红的基线等于没有基线。
        """
        p = prof_book.profiles["power_spec_cn"]
        assert fp(replace(p, **{field: value}), prof_book, kinds, fields) == fp(
            p, prof_book, kinds, fields
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("section_keywords", ("功能/性能要求", "新增章节")),
            ("exclude_words", ("不要求", "新增剔除词")),
            ("default_role", "other"),
            ("default_limits_to", "input"),
            ("req_id_pattern", r"\b(Z)\b"),
            ("reference_markers", ("详见", "另见")),
            ("include_prose", True),
            ("template_version", "2.0"),
        ],
    )
    def test_semantic_fields_change_fingerprint(self, prof_book, kinds, fields, field, value):
        """会改变抽取结果的改动必须改指纹 —— 否则门禁等于没接。"""
        p = prof_book.profiles["power_spec_cn"]
        assert fp(replace(p, **{field: value}), prof_book, kinds, fields) != fp(
            p, prof_book, kinds, fields
        )

    def test_section_priors_change_fingerprint(self, prof_book, kinds, fields):
        p = prof_book.profiles["power_spec_cn"]
        altered = dict(p.section_priors)
        # 取一个当前 limits_to 不是 input 的先验, 否则「改成 input」等于没改
        # (4.3.1 本来就是 input_domain/input, 拿它做对照会得到假阳性)。
        target = next(
            (sec for sec, pr in altered.items() if pr.limits_to != "input"),
            None,
        )
        assert target, "档案里应至少有一个 limits_to != input 的先验可供对照"
        altered[target] = replace(altered[target], limits_to="input")
        assert fp(replace(p, section_priors=altered), prof_book, kinds, fields) != fp(
            p, prof_book, kinds, fields
        )

    def test_section_prior_note_does_not_change_fingerprint(self, prof_book, kinds, fields):
        """先验的 note 是给人看的说明, 改了不该动指纹。"""
        p = prof_book.profiles["power_spec_cn"]
        altered = dict(p.section_priors)
        target = next(iter(altered))
        altered[target] = replace(altered[target], note="换一句说明")
        assert fp(replace(p, section_priors=altered), prof_book, kinds, fields) == fp(
            p, prof_book, kinds, fields
        )

    def test_vocabulary_changes_change_fingerprint(self, prof_book, kinds, fields):
        """词表与表字段白名单是模板的一部分: 它们变了抽取结果就变。"""
        p = prof_book.profiles["power_spec_cn"]
        base = fp(p, prof_book, kinds, fields)
        assert (
            template_fingerprint(
                p,
                known_kinds=kinds | {"new_kind"},
                known_roles=role_vocabulary(prof_book),
                schema_fields=fields,
            )
            != base
        )
        assert (
            template_fingerprint(
                p,
                known_kinds=kinds,
                known_roles=role_vocabulary(prof_book) | {"new_role"},
                schema_fields=fields,
            )
            != base
        )
        assert (
            template_fingerprint(
                p,
                known_kinds=kinds,
                known_roles=role_vocabulary(prof_book),
                schema_fields=list(fields) + ["zzz"],
            )
            != base
        )

    def test_excluded_keys_are_explicit(self):
        """排除清单是刻意的, 不是遗漏 —— 所以它本身要被钉住。"""
        assert FINGERPRINT_EXCLUDED_KEYS == {
            "description",
            "note",
            "review_dispositions",
            "name",
        }

    def test_method_book_is_not_in_the_fingerprint(self):
        """方法库不进指纹。

        它描述「怎么补齐」不是「怎么解析」。混进来之后每补一条业界方法都要重采
        全部模板基线, 而补方法与解析能力无关。
        """
        import inspect

        sig = inspect.signature(template_fingerprint)
        params = " ".join(sig.parameters)
        assert "method" not in params.lower(), f"签名里不该出现方法库: {params}"
        assert "test_methods" not in params


class TestTemplateIdentity:
    def test_repository_profiles_declare_template_id(self, prof_book):
        """每个档案都要声明模板身份 —— 「模板」此前根本没被建模。"""
        for name, p in prof_book.profiles.items():
            assert p.template_id, f"档案 {name} 未声明 template_id"
            assert p.template_version, f"档案 {name} 未声明 template_version"

    def test_same_family_shares_template_id(self, prof_book):
        """power_spec_cn 与 pn2000 是同一家模板的两种章节号 -> 同一 template_id。

        挂两个 id 就永远看不出「这俩其实是同一个模板被改过章节号」, 而那正是
        §4.0 要抓的那类变更。
        """
        a = prof_book.profiles["power_spec_cn"]
        b = prof_book.profiles["power_spec_cn_pn2000"]
        assert a.template_id == b.template_id

    def test_unrelated_profile_has_own_template_id(self, prof_book):
        """minimal 是对照/冒烟档案, 不该挂在真实模板 id 上。

        挂了就会让「改了 minimal」看起来像「改了真实模板」, 基线跟着一起红。
        """
        assert (
            prof_book.profiles["minimal"].template_id
            != prof_book.profiles["power_spec_cn"].template_id
        )

    def test_same_family_different_priors_give_different_fingerprints(
        self, prof_book, kinds, fields
    ):
        """同族但档案不同 -> 指纹必须不同 (章节先验确实不同)。"""
        assert fp(prof_book.profiles["power_spec_cn"], prof_book, kinds, fields) != fp(
            prof_book.profiles["power_spec_cn_pn2000"], prof_book, kinds, fields
        )

    def test_missing_template_id_raises_not_falls_back(self, prof_book):
        """缺 template_id 必须报错, 不能回落到档案名。

        回落会让「模板身份」退化成「档案名」, 而一个模板族可以有多个档案 ——
        分组维度错了, 后面所有按 (template_id, model_id) 存的基线都会串。
        """
        stripped = DocProfile(name="no_template", section_keywords=("x",), template_id="")
        with pytest.raises(ValueError) as ei:
            template_identity(stripped)
        assert "template_id" in str(ei.value)

    def test_identity_returns_three_fields(self, prof_book, kinds, fields):
        ident = template_identity(
            prof_book.profiles["power_spec_cn"],
            known_kinds=kinds,
            known_roles=role_vocabulary(prof_book),
            schema_fields=fields,
        )
        assert set(ident) == {"template_id", "template_version", "fingerprint"}
        assert len(ident["fingerprint"]) == 16


class TestProductCarriesTemplateIdentity:
    def test_extraction_result_has_template(self):
        """抽取产物必须带模板身份 —— 否则事后无从追责。"""
        r = extract_test_conditions("PA601-D54A")
        assert r.template["template_id"] == "power_spec_cn_v1"
        assert r.template["template_version"] == "1.0"
        assert r.template["fingerprint"]

    def test_to_dict_includes_template(self):
        """序列化也要带上 —— 落库/导出走的正是 to_dict。"""
        d = extract_test_conditions("PA601-D54A").to_dict()
        assert d["template"]["template_id"] == "power_spec_cn_v1"

    def test_template_identity_does_not_change_conditions(self):
        """加模板身份不得影响抽取结果本身 (纯记录, 不是逻辑)。"""
        r = extract_test_conditions("PA601-D54A")
        assert r.stats["conditions_total"] == 95, "条件数变了说明接入时碰了逻辑"


class TestBaselineFile:
    def test_baseline_exists_and_is_versioned(self):
        doc = load_baseline(Path(BASELINE))
        assert doc["version"] == 1
        assert doc["entries"], "基线应至少覆盖一个在册且已入库的型号"

    def test_baseline_entries_have_the_fields_the_gate_compares(self):
        """基线条目必须存全被比较的字段 —— 缺字段会让比较静默通过。

        ``needs_review_by_section`` 也在内: 它是被**独立**比较的字段
        (见 :func:`diff_by_section`), 缺了它不会让比较通过, 而是把当前每一章
        都报成「新增待审」—— 一样是假红, 一样会让人养成忽略门禁输出的习惯。
        """
        doc = load_baseline(Path(BASELINE))
        for mid, e in doc["entries"].items():
            assert e["fingerprint"], f"{mid} 基线缺 fingerprint"
            assert e["template_id"], f"{mid} 基线缺 template_id"
            for k in TOLERANCE:
                assert k in e["metrics"], f"{mid} 基线缺指标 {k}"
            assert e["governed_sections"] is not None, f"{mid} 基线缺 governed_sections"
            assert e.get("needs_review_by_section") is not None, (
                f"{mid} 基线缺 needs_review_by_section —— 章节级判定会全章报成新增"
            )

    def test_baseline_layers_add_up_to_the_queue(self):
        """基线里两层的待审数必须能对上「待审总数」的语义。

        这条不是形式检查: 分层是为了让两个方向相反的量各有各的容差, 而一旦分层
        与总数脱节(某层漏存/多存), 门禁就在拿两个互不相关的数字做判断。分层
        判据正确性的前提是它们仍覆盖同一个集合。
        """
        doc = load_baseline(Path(BASELINE))
        for mid, e in doc["entries"].items():
            dist = e.get("needs_review_by_section") or {}
            for layer, counts in dist.items():
                key = f"needs_review_{layer}"
                assert key in TOLERANCE, f"{mid} 分布层 {layer} 没有对应的指标 {key}"
                assert sum(counts.values()) == e["metrics"][key], (
                    f"{mid} {key} 指标={e['metrics'][key]} 但分布合计={sum(counts.values())}"
                )

    def test_section_tolerance_catches_what_the_count_tolerance_hides(self):
        """章节容差与总数容差是**互补**关系, 不是谁比谁严。

        章节容差的存在只为抓一种情况: **总数在容差内, 但变化集中在一章**。
        所以判据是「存在一组数, 总数差 <= 该指标容差 而某章差 > SECTION_TOLERANCE」。

        写成「章节容差必须严于总数容差」是错的 —— 抽取层总数基线只有 2 条
        (``needs_manual_digitization``, 全在 4.3.1), 总数容差 2 已经贴着地板,
        章节容差 3 比它宽是合理的: 那一章本来就这么短, 涨 3 条才值得问。
        强求 3 < 2 只会逼着人把容差调到没有物理意义的数上去迎合断言。
        """
        from template_drift import diff_by_section

        # 落在容差内的构造: 4.3.1 减少 4、4.3.2 增加 4 -> 总数差 0(远在 assess
        # 容差 8 之内), 但 4.3.2 单章 +4 已超章节容差 3。这种「一增一减相抵」
        # 正是总数口径看不见的变化。
        base = {"extract": {}, "assess": {"4.3.1": 5, "4.3.2": 3, "4.3.3": 2}}
        now = {"extract": {}, "assess": {"4.3.1": 1, "4.3.2": 7, "4.3.3": 2}}
        total_delta = sum(now["assess"].values()) - sum(base["assess"].values())
        assert total_delta <= TOLERANCE["needs_review_assess"], (
            f"样本没落在总数容差内: 总数差 {total_delta} > {TOLERANCE['needs_review_assess']}"
        )
        out = diff_by_section(now, base, tolerance=SECTION_TOLERANCE)
        assert any("4.3.2" in d for d in out), (
            f"4.3.2 单独 +4 超章节容差 {SECTION_TOLERANCE}, 必须被报出来; 实得 {out}"
        )

    def test_section_tolerance_is_positive(self):
        """容差 0 会让任何一条待审项的新增/消失都变红 —— 包括纯噪声。"""
        assert SECTION_TOLERANCE >= 1

    def test_baseline_documents_that_missing_is_not_clean(self):
        """基线文件本身要写明「缺条目 ≠ 无漂移」。"""
        text = Path(BASELINE).read_text(encoding="utf-8")
        assert "缺条目" in text and "无漂移" in text
        assert "fail-closed" in text, "要写明抽取过程才是防线"

    def test_baseline_header_is_generated_by_code(self):
        """头部说明必须由代码生成, 不能手工维护。

        手工维护的注释会在**第一次 --update 时被冲掉**, 而那段说明恰恰是防止
        「直接 --update 把门禁的牙拔了」的唯一东西 —— 冲掉之后它就永久消失了,
        且没有任何人会发现。
        """
        from template_drift import BASELINE_HEADER
        from template_drift import main as td_main

        actual = Path(BASELINE).read_text(encoding="utf-8")
        assert actual.startswith(BASELINE_HEADER), "实际文件的头部与代码里的头部不一致"
        # 且 header 里必须包含关键告诫, 而不只是版本行
        for token in ("缺条目", "fail-closed", "不要跳过"):
            assert token in BASELINE_HEADER, f"生成的头部缺 {token}"
        assert callable(td_main)

    def test_conditions_total_tolerance_is_zero(self):
        """条件数不许波动 —— 变了就是漏抽或多抽, 没有解释。"""
        assert TOLERANCE["conditions_total"] == 0


class TestDiffMetrics:
    def test_no_change_yields_nothing(self):
        over, within = diff_metrics({"conditions_total": 95}, {"conditions_total": 95})
        assert over == [] and within == []

    def test_conditions_total_change_is_over_tolerance(self):
        over, within = diff_metrics({"conditions_total": 90}, {"conditions_total": 95})
        assert len(over) == 1 and "conditions_total" in over[0]
        assert within == []

    def test_small_change_is_reported_separately_not_swallowed(self):
        """变了但未超容差**也要报**。

        静默掉它, 「变了 3 条待审」和「没变」在输出上一模一样, 而人无法区分这两
        种情况。真正的代价不是红, 是学会了忽略输出。
        """
        over, within = diff_metrics({"needs_review_extract": 1}, {"needs_review_extract": 2})
        assert over == []
        assert len(within) == 1 and "needs_review_extract" in within[0]

    def test_arrow_shows_direction(self):
        # 变化量要**明确超过**容差, 否则会落进 within 段 —— 容差 8 时 +8 刚好
        # 不算超。这条第一次写成 50->42(+8) 就撞了这个坑: 断言 over[0] 却拿到
        # 空列表, IndexError 掩盖了「测试数据选得不对」这个真实原因。
        over, _ = diff_metrics({"needs_review_assess": 60}, {"needs_review_assess": 42})
        assert "增加" in over[0]
        over2, _ = diff_metrics({"needs_review_assess": 1}, {"needs_review_assess": 42})
        assert "减少" in over2[0]

    def test_the_two_layers_have_separate_tolerances(self):
        """抽取层涨 = 退化, 评估层涨 = 工作量。两者不能共用一个容差。

        这条钉的是 :data:`TOLERANCE` 里两个键**都在**且**值不同**。合成一个键
        会让评估层的正当增长(签字通道入队)吃掉抽取层的退化余量, 反之亦然 ——
        两种情况都会报「无漂移」。
        """
        assert "needs_review_extract" in TOLERANCE
        assert "needs_review_assess" in TOLERANCE
        assert TOLERANCE["needs_review_extract"] != TOLERANCE["needs_review_assess"]
        # 旧的混计键必须消失: 它还在就说明有人加了新项却没想清楚归哪一层,
        # 而门禁会照旧拿一个容差同时判两个方向相反的量。
        assert "needs_review" not in TOLERANCE


class TestDiffBySection:
    """章节级判定: 与总数**独立**, 不是总数超了才看章节。"""

    def _dist(self, extract=None, assess=None):
        return {"extract": dict(extract or {}), "assess": dict(assess or {})}

    def test_concentrated_change_reported_even_when_total_is_small(self):
        """总数 +4 在容差内, 但全落在一章 -> 必须报。

        这条是本机制存在的全部理由: 分散的 +4 无害, 集中的 +4 要查那一章的规则。
        两者在总数上无法区分, 所以不能只看总数。
        """
        base = self._dist(assess={"4.3.1": 1, "4.3.2": 1})
        now = self._dist(assess={"4.3.1": 1, "4.3.2": 5})  # 总数 +4, 单章 +4
        assert abs(sum(now["assess"].values()) - sum(base["assess"].values())) <= 5
        out = diff_by_section(now, base, tolerance=3)
        assert len(out) == 1 and "4.3.2" in out[0]

    def test_spread_change_stays_quiet(self):
        """同样的 +4 摊在四章, 每章 +1 -> 不报。"""
        base = self._dist(assess={"4.3.%d" % i: 1 for i in range(1, 5)})
        now = self._dist(assess={"4.3.%d" % i: 2 for i in range(1, 5)})
        assert diff_by_section(now, base, tolerance=3) == []

    def test_layers_are_judged_separately(self):
        """同章节号在两层里各判各的 —— 合并会把 extract 与 assess 的变化搅在一起。"""
        base = self._dist(extract={"4.3.1": 10}, assess={"4.3.1": 1})
        now = self._dist(extract={"4.3.1": 10}, assess={"4.3.1": 9})
        out = diff_by_section(now, base, tolerance=3)
        assert len(out) == 1 and out[0].startswith("assess/4.3.1")

    def test_new_section_is_reported(self):
        """基线里没有的章节 = 新增待审, 不能因为不在交集里就当没变。"""
        out = diff_by_section(
            self._dist(assess={"4.3.9": 4}), self._dist(assess={"4.3.1": 1}), tolerance=3
        )
        assert len(out) == 1 and "4.3.9" in out[0]

    def test_disappeared_section_is_reported(self):
        """待审项消失也要报 —— 可能是抽取器不再识别了, 那比多出来更危险。"""
        out = diff_by_section(
            self._dist(assess={"4.3.1": 1}), self._dist(assess={"4.3.1": 9}), tolerance=3
        )
        assert len(out) == 1 and "减少" in out[0]

    def test_identical_distribution_is_silent(self):
        d = self._dist(extract={"4.3.1": 2}, assess={"4.3.2": 20})
        assert diff_by_section(d, d, tolerance=3) == []


@pytest.fixture(scope="module")
def measured():
    """跑一次真实抽取拿到当前状态 (measure() 的返回就是「当前」本身)。"""
    from template_drift import measure

    return measure("PA601-D54A")


def _baseline_from(measured: dict, **overrides) -> dict:
    """用当前状态造一条基线, 只改指定字段 —— 免得每条用例重抄一遍键。

    **必须带上** ``needs_review_by_section``: 章节分布是与总数**独立**判定的
    (见 template_drift.diff_by_section), 少了它 check() 会把当前每一章都当成
    「新增待审」, 于是「基线与当前完全一致」也会判成 drift —— 那条用例会假装
    门禁坏了, 而真实原因是 fixture 少造了一个字段。
    """
    entry = {
        "fingerprint": measured["fingerprint"],
        "template_version": measured["template_version"],
        "metrics": dict(measured["metrics"]),
        "needs_review_by_section": {
            layer: dict(dist)
            for layer, dist in (measured.get("needs_review_by_section") or {}).items()
        },
        "governed_sections": list(measured["governed_sections"]),
    }
    entry.update(overrides)
    return {"entries": {"PA601-D54A": entry}}


class TestCheck:
    def test_no_baseline_is_its_own_status(self, measured):
        """新增型号不是漂移, 是「还没有基线」—— 两者混在一起会养成忽略的习惯。"""
        res = check("PA601-D54A", {"entries": {}})
        assert res["status"] == "no_baseline"
        assert res["fingerprint_changed"] is None
        assert "--update" in res["message"]

    def test_matching_baseline_is_ok(self, measured):
        res = check("PA601-D54A", _baseline_from(measured))
        assert res["status"] == "ok", res["message"]
        assert not res["fingerprint_changed"]
        assert not res["metric_diffs"]

    def test_fingerprint_change_reports_affected_sections(self, measured):
        """只报「变了」没用 —— 人得知道改了什么章节号。"""
        res = check(
            "PA601-D54A",
            _baseline_from(measured, fingerprint="deadbeefdeadbeef", governed_sections=["9.9.9"]),
        )
        assert res["status"] == "drift"
        assert res["fingerprint_changed"]
        assert "9.9.9" in res["governed_sections_changed"]
        assert any(s.startswith("4.3") for s in res["governed_sections_changed"])

    def test_metric_regression_alone_is_drift(self, measured):
        res = check(
            "PA601-D54A",
            _baseline_from(
                measured,
                metrics={
                    **measured["metrics"],
                    "conditions_total": measured["metrics"]["conditions_total"] + 25,
                },
            ),
        )
        assert res["status"] == "drift"
        assert res["metric_diffs"]
        assert not res["fingerprint_changed"], "这次不该是指纹变了"

    def test_check_stores_current_under_current_key(self, measured):
        """check() 的返回结构固定: 渲染与测试都依赖 result['current']。"""
        res = check("PA601-D54A", _baseline_from(measured))
        assert res["current"]["model_id"] == "PA601-D54A"
        assert res["current"]["fingerprint"] == measured["fingerprint"]

    def test_render_says_something_on_every_status(self, capsys, measured):
        """三种状态都得有输出 —— 没输出的状态会被当成「没问题」。"""
        drift = _baseline_from(measured, fingerprint="deadbeefdeadbeef")
        for baseline in ({"entries": {}}, _baseline_from(measured), drift):
            render(check("PA601-D54A", baseline))
            assert capsys.readouterr().out.strip(), "该状态没有输出"


class TestUpdateOnlyTouchesMeasured:
    def test_update_does_not_clobber_unmeasured_entries(self):
        """--update 只能覆盖**本次测过的**型号。

        顺手把没测的也刷成当前值, 等于清空它的基线 —— 而空基线永远不会红,
        于是那个型号从此静默。
        """
        baseline = {
            "version": 1,
            "entries": {
                "PN2000-24A": {"fingerprint": "keepme", "metrics": {"conditions_total": 7}}
            },
        }
        results = [
            {
                "current": {
                    "model_id": "PA601-D54A",
                    "profile": "p",
                    "template_id": "t",
                    "template_version": "1.0",
                    "fingerprint": "new",
                    "metrics": {"conditions_total": 95},
                    "governed_sections": ["4.3.1"],
                }
            }
        ]
        out = update(baseline, results)
        assert out["entries"]["PN2000-24A"]["fingerprint"] == "keepme", "未测量的条目被覆盖了"
        assert out["entries"]["PA601-D54A"]["fingerprint"] == "new"

    def test_update_records_the_note(self):
        """变更说明要跟着基线走 —— 基线是一串数字, 没人记得为什么改的。"""
        out = update({"entries": {}}, [], note="章节号 4.3 -> 5.2")
        assert out["update_note"] == "章节号 4.3 -> 5.2"

    def test_updated_baseline_is_valid_yaml(self):
        out = update(
            {"entries": {}},
            [
                {
                    "current": {
                        "model_id": "X",
                        "profile": "p",
                        "template_id": "t",
                        "template_version": "1.0",
                        "fingerprint": "f",
                        "metrics": {"conditions_total": 1},
                        "governed_sections": [],
                    }
                }
            ],
        )
        assert yaml.safe_load(yaml.safe_dump(out, allow_unicode=True))["entries"]["X"]


class TestUnmeasurableIsNotClean:
    def test_missing_blocks_raises_rather_than_reports_clean(self, tmp_path):
        """型号没入库 -> 报错, 而不是「无漂移」。

        这条最要紧: 报 PASS 等于告诉人「查过了没问题」, 而实际上什么都没查。
        """
        from aterag.extract.models import ModelNotIngested

        with pytest.raises(ModelNotIngested):
            check("NO-SUCH-MODEL-XYZ", load_baseline(Path(BASELINE)))

    def test_main_returns_error_code_when_a_model_cannot_be_measured(self, monkeypatch, capsys):
        """有一个型号测不了 -> 退出码 2 (ERROR), 不是 0。"""
        import template_drift as td

        monkeypatch.setattr(
            td,
            "registered_models",
            lambda: ["PA601-D54A", "GHOST-MODEL"],
        )
        rc = td.main([])
        out = capsys.readouterr().out
        assert rc == 2
        assert "ERROR" in out
        assert "GHOST-MODEL" in out
        assert "PASS" not in out, "有型号测不了时不得出现 PASS"

    def test_main_passes_when_nothing_drifted(self, monkeypatch, capsys):
        import template_drift as td

        monkeypatch.setattr(td, "registered_models", lambda: ["PA601-D54A"])
        rc = td.main([])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "TEMPLATE_DRIFT PASS" in out
