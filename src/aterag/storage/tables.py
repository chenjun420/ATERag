"""型号 schema 的表清单 —— DDL 生成与 RLS 策略的**唯一**真相源。

为什么单列一个模块
------------------
型号 schema 内有 31 张表, 而它们被两条独立的生成路径消费:

* :mod:`aterag.storage.model_schema` 建表
* :mod:`aterag.storage.rls` 为每张表生成 ENABLE/FORCE + CREATE POLICY

两者此前各维护一份手写清单 (``MODEL_TABLES`` 31 项 / ``RLS_TABLES``
15 项)。§5.8.3 纪律 2 要求「无 RLS 的表不允许上线」, 所以这两份清单**必须
相等** —— 但它们是手写的, 于是必然漂移。

板卡 192.168.5.25 上首次建库就撞上了: ``MODEL_TABLES`` 早已补齐到 31 张,
``RLS_TABLES`` 还停在最初那 15 张, 结果 16 张表建出来但没有策略, 门禁
``verify`` 报 FAIL。那是好的失败 (门禁抓到了), 但代价是一次部署往返。

把清单收到这里之后, ``RLS_TABLES`` 直接等于 ``MODEL_TABLES`` —— 漂移在
结构上不可能发生。加表只需改一处, 且新表自动带策略, 正是纪律 2 想要的
「默认安全」: 白名单的问题在于新加的表默认不在名单里, 于是新表静默裸奔,
而新表恰恰是最需要保护的。

public schema 的两张附件表 (:mod:`aterag.storage.model_schema` 的
``public_ddl``) **不在**本清单里 —— 它们按 §3.9 是跨型号的受控产物,
型号隔离对它们不适用, 其访问控制由 §3.9.4 的写序协议与巡检承担。
"""

from __future__ import annotations

__all__ = ["MODEL_TABLES"]

#: 型号 schema 内的全部表名。字母序 (便于 diff); 建表顺序不在这里表达 ——
#: 那由 ``model_schema._TABLE_BATCHES`` 的批次顺序决定 (批间有依赖)。
#:
#: 本清单与 ``_TABLE_BATCHES`` 的一致性由
#: ``tests/unit/storage/test_model_schema.py`` 的
#: ``TestTableCoverage`` 双向断言守住 (登记的必须建出来, 建出来的必须登记)。
MODEL_TABLES: tuple[str, ...] = (
    # 知识与事实 (§18.5「基础」)
    "clause",
    "doc",
    "doc_chunk",
    "fact",
    "provenance",
    "conflict",
    "trace",
    "test_requirement",
    "test_case",
    # 四遥 (§18.5「四遥」)
    "yx_point",
    "yx_soe",
    "yc_point",
    "yc_trend",
    "yk_command",
    "yk_audit",
    "yt_parameter",
    "yt_change_log",
    # 保护 (§18.5「四遥」续)
    "protection_setting",
    "protection_setting_log",
    "protection_coordination",
    "protection_action",
    "comm_protocol",
    # 工装与工位 (§18.5「工装」)
    "fixture",
    "test_station",
    "fixture_channel_map",
    "fixture_checkpoint",
    "poka_yoke_event",
    # 仪器台账与调度 (§18.5「仪器」「调度」+ §3.5.5)
    "instrument_ledger",
    "fixture_tp_probe",
    "sched_result",
    "jev_gate_log",
)
