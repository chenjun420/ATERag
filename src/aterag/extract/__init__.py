"""产测条件抽取: 从 RAG 已解析的规格书数据中抽取"输入条件 -> 输出条件"对.

四缝架构 (认知全部外置, 代码不含具体文档词汇):
  缝⓪ table_schemas.yaml    表怎么读        (ingest 侧, aterag.ingest.table_schema)
  缝① doc_profiles.yaml     抽哪章节/剔哪些  (selector + sieve)
  缝④ condition_patterns.yaml  一句话怎么切成条件子句 (assembler)
  缝③ MCP / CLI / JSON       结果出口

典型用法:
    from aterag.extract import extract_test_conditions, load_annotations
    r = extract_test_conditions("PA601-D54A", doc_version="B",
                                annotations=load_annotations("PA601-D54A"))
    for c in r.conditions:
        ...
"""

from aterag.extract.api import (
    DocProfile,
    ProfileBook,
    SectionPrior,
    extract_test_conditions,
    load_annotations,
    load_blocks,
    rows_from_blocks,
    rows_from_postgres,
)
from aterag.extract.assembler import AnnotationBook, PatternBook, row_fingerprint
from aterag.extract.assess import (
    AssessmentItem,
    AssessRule,
    RuleBook,
    assess_conditions,
)
from aterag.extract.models import (
    ConditionClause,
    ExcludedItem,
    ExtractionResult,
    ModelNotIngested,
    ReviewItem,
    SectionKeywordNotFound,
    Selection,
    TestCondition,
)
from aterag.extract.selector import section_matches, select_sections
from aterag.extract.sieve import apply_sieve, is_placeholder, no_data_dimensions
from aterag.extract.supplement import (
    DescriptionTemplate,
    MethodBook,
    render_descriptions,
    supplement_conditions,
)

__all__ = [
    "AnnotationBook",
    "AssessRule",
    "AssessmentItem",
    "ConditionClause",
    "DescriptionTemplate",
    "DocProfile",
    "ExcludedItem",
    "ExtractionResult",
    "MethodBook",
    "ModelNotIngested",
    "PatternBook",
    "ProfileBook",
    "ReviewItem",
    "RuleBook",
    "SectionKeywordNotFound",
    "SectionPrior",
    "Selection",
    "TestCondition",
    "apply_sieve",
    "assess_conditions",
    "extract_test_conditions",
    "is_placeholder",
    "load_annotations",
    "load_blocks",
    "no_data_dimensions",
    "render_descriptions",
    "row_fingerprint",
    "rows_from_blocks",
    "rows_from_postgres",
    "select_sections",
    "section_matches",
    "supplement_conditions",
]
