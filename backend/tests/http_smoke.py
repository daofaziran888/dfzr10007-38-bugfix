"""HTTP 端到端冒烟：启动 uvicorn 后用标准库 urllib 打真实接口。

运行：.venv/bin/python -I tests/http_smoke.py
（脚本会自行拉起子进程服务，也可先启动后端再设 SAMPLE_BASE 环境变量。）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("SAMPLE_BASE", "http://127.0.0.1:8000")
PASS = 0
FAIL = 0


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name: str, condition: bool, detail: object = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def wait_for_server(proc: subprocess.Popen) -> None:
    for _ in range(50):
        if proc.poll() is not None:
            raise RuntimeError("后端进程提前退出")
        try:
            status, body = call("GET", "/api/health")
            if status == 200 and body.get("ok"):
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("后端未在超时内就绪")


def main() -> int:
    proc = None
    if "SAMPLE_BASE" not in os.environ:
        proc = subprocess.Popen(
            [".venv/bin/python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=Path(__file__).resolve().parent.parent,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    try:
        if proc is not None:
            wait_for_server(proc)

        print("[A] 配置清单：补齐后的 SAMP-0001~0003 字段齐全、种子为整数")
        status, body = call("GET", "/api/sample?size=10")
        check("列表 200", status == 200, body)
        rows = {row["配置编号"]: row for row in body["items"]}
        check("SAMP-0001 种子=41（初始旧配置）", int(rows["SAMP-0001"]["随机种子"]) == 41, rows["SAMP-0001"])
        check("SAMP-0003 种子=129 且暂停", int(rows["SAMP-0003"]["随机种子"]) == 129 and rows["SAMP-0003"]["status"] == "已暂停")
        check("列表随机种子不丢失（无空值）", all(row.get("随机种子") not in (None, "") for row in body["items"]))

        print("[B] 登记：可选字段随配置落库")
        status, body = call("POST", "/api/sample", {"values": {
            "配置编号": "SAMP-HTTP-1", "数据源名称": "src_http", "采样算法": "分层随机采样",
            "样本比例": "25%", "随机种子": 7, "分层字段": "region", "输出表名": "sample_out_http",
        }})
        check("登记成功", status == 200 and body["ok"] is True, body)
        new_id = body["entry"]["id"]
        check("比例归一化/种子整数化", body["entry"]["样本比例"] == 0.25 and body["entry"]["随机种子"] == 7, body["entry"])
        status, body = call("GET", f"/api/sample/{new_id}")
        check("重新进入采样页快照一致", body["随机种子"] == 7 and body["分层字段"] == "region", body)

        print("[C] 同种子重复生成只保留一个结果且可复现")
        status, r1 = call("POST", f"/api/sample/{new_id}/generate", {"values": {"seed": 7}})
        check("首次生成成功", r1["ok"], r1)
        status, r2 = call("POST", f"/api/sample/{new_id}/generate", {"values": {"seed": 7}})
        check("重复生成幂等复用", r2["ok"] and "复用" in r2["message"], r2)
        check("结果 id 相同、样本一致", r2["entry"]["id"] == r1["entry"]["id"]
              and r2["entry"]["样本行号"] == r1["entry"]["样本行号"])
        status, body = call("GET", f"/api/sample/results?entry_id={new_id}")
        check("输出表清单不重复（一条）", body["total"] == 1, body)

        print("[D] 写入报错回滚：回读 seed=41 旧配置")
        status, body = call("PUT", "/api/sample/1", {"values": {"随机种子": 42, "force_fail": True}})
        check("故障写入返回失败", body["ok"] is False and "回滚" in body["message"], body)
        status, body = call("GET", "/api/sample/1")
        check("回滚后回读旧 seed=41", int(body["随机种子"]) == 41, body)

        print("[E] 正常保存 SAMP-0001 seed=42 并生成；SAMP-0003 保持暂停无新输出")
        status, body = call("PUT", "/api/sample/1", {"values": {"随机种子": 42}})
        check("保存 seed=42 成功", body["ok"] and body["entry"]["随机种子"] == 42, body)
        status, body = call("POST", "/api/sample/1/generate", {"values": {"seed": 42}})
        check("SAMP-0001 生成成功", body["ok"] and body["entry"]["随机种子"] == 42, body)
        status, body = call("POST", "/api/sample/3/generate", {})
        check("SAMP-0003 暂停被拦截、无新输出", body["ok"] is False and "不生成" in body["message"], body)
        status, body = call("GET", "/api/sample/results")
        check("输出表中不存在 SAMP-0003 的新结果",
              all(row["配置编号"] != "SAMP-0003" for row in body["items"]), body)
        status, body = call("GET", "/api/sample/1")
        check("SAMP-0001 持久快照仍是 seed=42，不受 SAMP-0003 影响", int(body["随机种子"]) == 42, body)

        print("[F] 数据准备流水线输入详情：分层输入非空、种子不丢失、结果不重复")
        status, body = call("GET", "/api/etl/inputs/1")
        check("详情 200", status == 200, body)
        check("输入源=SAMP-0001 输出表", body["输入源"] == "sample_out_samp-0001", body)
        linked = next(s for s in body["关联采样配置"] if s["配置编号"] == "SAMP-0001")
        check("种子=42 不丢失", linked["随机种子"] == 42, linked)
        check("采样结果仅一条不重复", len(linked["采样结果"]) == 1, linked)
        check("分层输入非空", body["分层输入"].get("city_level", 0) > 0, body["分层输入"])

        print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
        return 1 if FAIL else 0
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
