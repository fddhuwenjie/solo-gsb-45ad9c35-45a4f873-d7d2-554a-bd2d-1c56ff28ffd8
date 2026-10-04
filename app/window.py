"""按日期窗口送审后提交：整批修订临时应用 → 分段审查 → 一次性提交或回滚。

流程（全程在单个事务内）：
1. 复用 /applications 的版本链校验与自动封闭，把整批修订**临时**写入事务；
2. 按窗口 [start_date, end_date) 内所有权利要求版本的生效/失效边界划段，
   段起日 = start_date 以及落在 (start_date, end_date) 内的每个边界日；
3. 用现有 /audit 规则审查每个段起日：该日必须**存在有效权利要求**且审查通过；
4. 全部通过才提交（修订与 request_id 一次落库）；
   任一段为空或不通过 → 抛 WindowAuditRejected（最早失败日期 + 原因），
   事务回滚，整批修订及 request_id 均不写入，旧数据与旧证据包不受影响。
"""
from __future__ import annotations

import datetime as dt

from .audit import run_audit
from .db import Database
from .errors import WindowAuditRejected
from .models import WindowSubmissionIn
from .store import apply_revisions, idempotent_transaction

ENDPOINT = "POST /applications/window-audit"


def _segment_dates(
    db: Database,
    application_number: str,
    start: dt.date,
    end: dt.date,
) -> list[dt.date]:
    """收集窗口内的分段起日：start_date 及落在 (start, end) 的所有版本边界。

    边界来自该申请在临时状态下全部版本的 effective_from / effective_to；
    有效集合只可能在这些日期发生变化，故逐段审查等价于对窗口内每一天审查。
    """
    with db._lock:
        rows = db.conn.execute(
            "SELECT effective_from, effective_to FROM revisions"
            " WHERE application_number = ?",
            (application_number,),
        ).fetchall()

    boundaries: set[dt.date] = set()
    for r in rows:
        for raw in (r["effective_from"], r["effective_to"]):
            if not raw:
                continue
            d = dt.date.fromisoformat(raw)
            if start < d < end:
                boundaries.add(d)
    return [start] + sorted(boundaries)


def submit_with_window_audit(
    db: Database, payload: WindowSubmissionIn
) -> tuple[dict, int, bool]:
    """按窗口逐段送审通过后一次性提交修订；否则整批回滚。"""
    body = payload.model_dump(mode="json")

    def perform(db: Database) -> dict:
        # 1) 临时应用整批修订（含版本链校验、开放版本自动封闭）
        applied = apply_revisions(db, payload)

        # 2) 按临时状态下的生效/失效边界划段
        segment_dates = _segment_dates(
            db, payload.application_number, payload.start_date, payload.end_date
        )

        # 3) 用现有 /audit 规则审查每个段起日；首个失败即中止并回滚
        segments: list[dict] = []
        for audit_date in segment_dates:
            result = run_audit(db, payload.application_number, audit_date)
            if not result["effective_claims"]:
                raise WindowAuditRejected(
                    f"审查窗口段 {audit_date.isoformat()} 内没有任何有效权利要求",
                    details={
                        "failed_date": audit_date.isoformat(),
                        "reason": "EMPTY_SEGMENT",
                        "issues": [],
                    },
                )
            if result["status"] != "pass":
                raise WindowAuditRejected(
                    f"审查窗口段 {audit_date.isoformat()} 审查不通过",
                    details={
                        "failed_date": audit_date.isoformat(),
                        "reason": "AUDIT_FAILED",
                        "issues": result["issues"],
                    },
                )
            segments.append({
                "audit_date": audit_date.isoformat(),
                "effective_claims": len(result["effective_claims"]),
                "status": "pass",
            })

        # 4) 全部通过：返回成功响应，由 idempotent_transaction 与 request_id 一起提交
        return {
            "request_id": payload.request_id,
            "application_number": payload.application_number,
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
            "stored_revisions": applied["stored_revisions"],
            "auto_closed": applied["auto_closed"],
            "segments": segments,
        }

    return idempotent_transaction(
        db,
        request_id=payload.request_id,
        endpoint=ENDPOINT,
        body=body,
        perform=perform,
    )
