"""采样链路修复的回归验证：可直接 `python tests/test_sample_flow.py` 运行。

覆盖任务要求的全部场景：
1. 可选采样字段随配置落库（登记断点）
2. SAMP-0001～SAMP-0003 存量配置按清单补齐
3. 同一随机种子重复生成只保留一个结果且可复现
4. 两个任务并发写同一输出表只成功一个
5. 写入报错回滚，回读旧 seed=41；成功后 SAMP-0001 持久化为 seed=42
6. SAMP-0003 暂停不生成新输出表，且不影响 SAMP-0001
7. 数据准备流水线沿输出表读到非空分层输入
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import store  # noqa: E402
from app.services.sample import BLOCK_GENERATE_STATUS, service as sample_service  # noqa: E402
from app.services.etl import service as etl_service  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def test_backfill_catalog() -> None:
    print("[1] 存量配置补齐：SAMP-0001～SAMP-0003 清单核对")
    expected = {
        "SAMP-0001": ("dwd_order_detail", "分层随机采样", 0.2, 41, "city_level"),
        "SAMP-0002": ("dwd_user_profile", "简单随机采样", 0.1, 73, ""),
        "SAMP-0003": ("dwd_payment_flow", "分层随机采样", 0.05, 129, "pay_channel"),
    }
    rows = {row["配置编号"]: row for row in store.rows("sample")}
    for code, (source, algo, ratio, seed, strata) in expected.items():
        row = rows[code]
        check(
            f"{code} 数据源/算法/比例/种子/分层字段",
            (
                row["数据源名称"] == source
                and row["采样算法"] == algo
                and abs(float(row["样本比例"]) - ratio) < 1e-9
                and int(row["随机种子"]) == seed
                and row["分层字段"] == strata
            ),
            str(row),
        )
        check(f"{code} 输出表名已补齐", bool(str(row["输出表名"]).strip()), str(row))
    check("补齐后随机种子为整数类型", isinstance(rows["SAMP-0001"]["随机种子"], int))


def test_create_persists_optional_fields() -> None:
    print("[2] 登记断点：可选采样字段必须与配置一起落库")
    entry, missing = sample_service.create_entry({
        "配置编号": "SAMP-0100",
        "数据源名称": "dwd_new_source",
        "采样算法": "简单随机采样",
        "样本比例": "30%",
        "随机种子": "55",
        "分层字段": "province",
        "输出表名": "sample_out_samp_0100",
    })
    check("登记成功且无缺失字段", entry is not None and missing == [])
    check("样本比例落库并归一化", entry is not None and abs(entry["样本比例"] - 0.3) < 1e-9, str(entry))
    check("随机种子落库为整数", entry is not None and entry["随机种子"] == 55, str(entry))
    check("分层字段落库", entry is not None and entry["分层字段"] == "province")
    check("输出表名落库", entry is not None and entry["输出表名"] == "sample_out_samp_0100")

    # 缺省可选字段也要显式落库，不能出现字段丢失
    entry2, _ = sample_service.create_entry({
        "配置编号": "SAMP-0101",
        "数据源名称": "dwd_other",
        "采样算法": "简单随机采样",
    })
    check("未填可选字段时仍显式落库", entry2 is not None and all(
        key in entry2 for key in ("样本比例", "随机种子", "分层字段", "输出表名")
    ), str(entry2))


def test_idempotent_and_reproducible() -> None:
    print("[3] 同种子幂等与可复现：重复生成只保留一个结果")
    entry, _ = sample_service.create_entry({
        "配置编号": "SAMP-0200",
        "数据源名称": "dwd_repro",
        "采样算法": "简单随机采样",
        "样本比例": 0.15,
        "随机种子": 99,
        "输出表名": "sample_out_samp_0200",
    })
    r1, msg1 = sample_service.generate_result(entry["id"], seed=99)
    r2, msg2 = sample_service.generate_result(entry["id"], seed=99)
    check("两次生成都返回结果", r1 is not None and r2 is not None)
    check("第二次为复用而非新建", "复用" in msg2, msg2)
    check("两次返回同一条结果（id 相同）", r1 is not None and r2 is not None and r1["id"] == r2["id"])
    check("样本行号完全一致（可复现）", r1 is not None and r2 is not None and r1["样本行号"] == r2["样本行号"])
    check("校验码一致", r1 is not None and r2 is not None and r1["校验码"] == r2["校验码"])

    results = sample_service.list_results(entry["id"])
    check("结果清单按（配置,种子）去重，只有一条", len(results) == 1, str(len(results)))

    r3, _ = sample_service.generate_result(entry["id"], seed=100)
    check("不同种子产生新结果", r3 is not None and r3["id"] != r1["id"])
    check("不同种子样本不同", r3 is not None and r3["样本行号"] != r1["样本行号"])
    check("结果清单变为两条", len(sample_service.list_results(entry["id"])) == 2)


def test_concurrent_same_output_table() -> None:
    print("[4] 并发写同一输出表：只能一个成功")
    e1, _ = sample_service.create_entry({
        "配置编号": "SAMP-0300", "数据源名称": "src_a", "采样算法": "简单随机采样",
        "随机种子": 1, "输出表名": "sample_out_contended",
    })
    e2, _ = sample_service.create_entry({
        "配置编号": "SAMP-0301", "数据源名称": "src_b", "采样算法": "简单随机采样",
        "随机种子": 2, "输出表名": "sample_out_contended",
    })
    table = "sample_out_contended"

    # 确定性模拟：任务 A 正在写该输出表（持有生产代码使用的同一把表锁），
    # 任务 B 并发抢锁必须立刻失败，而不是排队等待。
    lock = sample_service._table_lock(table)
    lock.acquire()
    try:
        blocked_result, blocked_msg = sample_service.generate_result(e2["id"])
        check("并发方被拦截", blocked_result is None and "正被其他任务写入" in blocked_msg, blocked_msg)
        check("被拦截期间输出表没有任何写入",
              all(r["输出表名"] != table for r in store.rows("sample_result")))
    finally:
        lock.release()

    # 锁释放后任务 A 串行写入成功，同一张表最终只有一个采样结果
    ok_result, ok_msg = sample_service.generate_result(e1["id"])
    check("持锁方写入成功", ok_result is not None and "已写入" in ok_msg, ok_msg)
    table_rows = [r for r in store.rows("sample_result") if r["输出表名"] == table]
    check("输出表内只有一个采样结果", len(table_rows) == 1, str(len(table_rows)))

    # 同配置同种子再来一次：幂等复用，不产生第二条
    again, msg = sample_service.generate_result(e1["id"])
    check("同种子重复请求复用既有结果", again is not None and again["id"] == ok_result["id"] and "复用" in msg, msg)
    check("输出表仍然只有一个采样结果",
          len([r for r in store.rows("sample_result") if r["输出表名"] == table]) == 1)

    # 真实双线程竞争：拉长写入临界区，保证两个线程时间窗重叠
    e3, _ = sample_service.create_entry({
        "配置编号": "SAMP-0302", "数据源名称": "src_c", "采样算法": "简单随机采样",
        "随机种子": 3, "输出表名": "sample_out_race",
    })
    e4, _ = sample_service.create_entry({
        "配置编号": "SAMP-0303", "数据源名称": "src_d", "采样算法": "简单随机采样",
        "随机种子": 4, "输出表名": "sample_out_race",
    })
    original_build = sample_service._build_result

    def slow_build(entry, seed, ratio):  # noqa: ANN001
        time.sleep(0.15)
        return original_build(entry, seed, ratio)

    sample_service._build_result = slow_build
    messages: list[str] = []
    barrier = threading.Barrier(2)
    try:
        def worker(entry_id: int) -> None:
            barrier.wait()
            _, message = sample_service.generate_result(entry_id)
            messages.append(message)

        t1 = threading.Thread(target=worker, args=(e3["id"],))
        t2 = threading.Thread(target=worker, args=(e4["id"],))
        t1.start(); t2.start(); t1.join(); t2.join()
    finally:
        sample_service._build_result = original_build
    check("双线程竞争恰好一个成功", sum("已写入" in m for m in messages) == 1, str(messages))
    check("双线程竞争另一个被拦截", sum("正被其他任务写入" in m for m in messages) == 1, str(messages))
    race_rows = [r for r in store.rows("sample_result") if r["输出表名"] == "sample_out_race"]
    check("竞争输出表最终只有一个结果", len(race_rows) == 1, str(len(race_rows)))


def test_rollback_on_write_error() -> None:
    print("[5] 写入报错回滚：回读 seed=41 旧配置，快照保持一致")
    samp1 = store.find("sample", 1)
    check("初始 SAMP-0001 随机种子为 41", int(samp1["随机种子"]) == 41, str(samp1["随机种子"]))

    rolled, message = sample_service.update_entry(1, {"随机种子": 42}, force_fail=True)
    check("写入失败返回错误", rolled is None and "回滚" in message, message)
    reread = sample_service.get_entry(1)
    check("回滚后重新读到旧 seed=41", int(reread["随机种子"]) == 41, str(reread["随机种子"]))
    check("其余字段快照不变", reread["数据源名称"] == "dwd_order_detail" and reread["分层字段"] == "city_level")

    # 生成路径的失败同样回滚
    before_seed = int(samp1["随机种子"])
    r, msg = sample_service.generate_result(1, seed=42, force_fail=True)
    check("生成写入失败返回错误", r is None and "回滚" in msg, msg)
    check("生成失败后种子仍是旧值", int(sample_service.get_entry(1)["随机种子"]) == before_seed)

    # 成功保存 seed=42
    ok, message = sample_service.update_entry(1, {"随机种子": 42})
    check("正常修改成功", ok is not None and int(ok["随机种子"]) == 42, message)
    check("重新进入采样页（重新读取）持久快照一致", int(sample_service.get_entry(1)["随机种子"]) == 42)


def test_paused_config_no_new_output() -> None:
    print("[6] SAMP-0003 保持暂停：不生成新输出表，且不影响 SAMP-0001")
    samp3 = store.find("sample", 3)
    check("SAMP-0003 处于暂停状态", samp3["status"] in BLOCK_GENERATE_STATUS, samp3["status"])
    before_results = len(store.rows("sample_result"))
    r, msg = sample_service.generate_result(3)
    check("暂停配置生成被拦截", r is None and "不生成" in msg, msg)
    check("没有新增任何采样结果", len(store.rows("sample_result")) == before_results)

    # SAMP-0001 的 seed=42 不受影响，并可正常生成
    check("SAMP-0001 仍是 seed=42", int(sample_service.get_entry(1)["随机种子"]) == 42)
    r1, msg = sample_service.generate_result(1, seed=42)
    check("SAMP-0001 seed=42 生成成功", r1 is not None and int(r1["随机种子"]) == 42, msg)
    check("结果分层输入随表落库", r1 is not None and r1["分层输入"] == {"city_level": r1["样本行数"]}, str(r1))
    again, _ = sample_service.generate_result(1, seed=42)
    check("SAMP-0001 重复生成仍只保留一个结果", again is not None and again["id"] == r1["id"])
    check("SAMP-0003 依旧暂停且无新输出表", sample_service.get_entry(3)["status"] == "已暂停")


def test_etl_reads_stratified_input() -> None:
    print("[7] 数据准备流水线：沿样本输出表读到非空分层输入")
    etl1 = etl_service.get_entry(1)
    samp1 = sample_service.get_entry(1)
    check("ETL-0001 输入源已关联 SAMP-0001 输出表", etl1["输入源"] == samp1["输出表名"], str(etl1))
    detail = etl_service.get_pipeline_input(1)
    check("流水线输入详情存在", detail is not None)
    check("关联到 SAMP-0001 配置", any(s["配置编号"] == "SAMP-0001" for s in detail["关联采样配置"]))
    linked = next(s for s in detail["关联采样配置"] if s["配置编号"] == "SAMP-0001")
    check("详情页随机种子不丢失（42）", int(linked["随机种子"]) == 42, str(linked))
    check("分层输入非空", bool(detail["分层输入"]) and detail["分层输入"].get("city_level", 0) > 0, str(detail["分层输入"]))
    check("采样结果不重复显示", len(linked["采样结果"]) == 1, str(len(linked["采样结果"])))
    check("输出表与配置清单对得上", detail["输入源"] == samp1["输出表名"])

    # 未生成结果的任务：分层字段占位保证不空（SAMP-0002 是简单随机采样、无分层字段，
    # 因此为空是正确的；改用带分层字段、未生成结果的 ETL-0003/SAMP-0003）
    detail3 = etl_service.get_pipeline_input(3)
    check("无结果时分层输入按分层字段占位非空",
          detail3["分层输入"] == {"pay_channel": 0}, str(detail3["分层输入"]))
    detail2 = etl_service.get_pipeline_input(2)
    check("无分层字段的简单随机采样分层输入为空（语义正确）", detail2["分层输入"] == {})


def main() -> int:
    test_backfill_catalog()
    test_create_persists_optional_fields()
    test_idempotent_and_reproducible()
    test_concurrent_same_output_table()
    test_rollback_on_write_error()
    test_paused_config_no_new_output()
    test_etl_reads_stratified_input()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
