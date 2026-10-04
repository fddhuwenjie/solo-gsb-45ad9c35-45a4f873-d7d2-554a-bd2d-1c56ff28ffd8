"""FastAPI 入口。"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .audit import run_audit
from .db import Database
from .errors import DomainError, NotFoundError
from .evidence import create_evidence, read_evidence
from .models import (
    ApplicationIn,
    ApplicationResponse,
    AuditIn,
    AuditResponse,
    EvidenceCreatedResponse,
    EvidenceResponse,
    WindowedApplicationIn,
    WindowedApplicationResponse,
)
from .store import get_application, submit_revisions, submit_revisions_windowed

DATABASE_PATH = os.environ.get("DATABASE_PATH", "./data/app.db")


def create_app(db_path: str = DATABASE_PATH) -> FastAPI:
    app = FastAPI(
        title="专利权利要求依附审查 API",
        version="1.0.0",
        description="按版本链记录权利要求修订，按目标日期重建有效版本并审查依附关系。",
    )
    app.state.db = Database(db_path)

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/applications", status_code=201, response_model=ApplicationResponse)
    def post_applications(payload: ApplicationIn, request: Request):
        """提交申请号及一批权利要求修订（幂等，事务性）。"""
        response, status_code, replayed = submit_revisions(request.app.state.db, payload)
        return JSONResponse(
            status_code=status_code,
            content=response,
            headers={"X-Idempotent-Replay": "true" if replayed else "false"},
        )

    @app.post(
        "/applications/audited-submissions",
        status_code=201,
        response_model=WindowedApplicationResponse,
    )
    def post_applications_windowed(payload: WindowedApplicationIn, request: Request):
        """按日期窗口送审通过后一次性提交修订（幂等，全有或全无）。

        整批修订（含开放版本自动封闭）先暂时应用，按半开区间
        [start_date, end_date) 内的生效/失效边界划段，逐段起日执行
        与 /audit 相同的审查；每段都有有效权利要求且全部通过才落库，
        否则整批回滚并返回最早失败日期与原因。
        """
        response, status_code, replayed = submit_revisions_windowed(
            request.app.state.db, payload
        )
        return JSONResponse(
            status_code=status_code,
            content=response,
            headers={"X-Idempotent-Replay": "true" if replayed else "false"},
        )

    @app.get("/applications/{application_number}")
    def get_application_state(application_number: str, request: Request):
        """查看某申请的全部修订版本链（调试用）。"""
        result = get_application(request.app.state.db, application_number)
        if result is None:
            raise NotFoundError(f"申请号 '{application_number}' 不存在")
        return result

    @app.post("/audit", response_model=AuditResponse)
    def post_audit(payload: AuditIn, request: Request):
        """按申请号与目标日期重建有效版本并审查依附关系。"""
        return run_audit(request.app.state.db, payload.application_number, payload.target_date)

    @app.post("/audit/evidence", status_code=201, response_model=EvidenceCreatedResponse)
    def post_audit_evidence(payload: AuditIn, request: Request):
        """按现有 /audit 规则审查并冻结为可复验证据包，返回 evidence_id 与 SHA-256。"""
        return create_evidence(request.app.state.db, payload.application_number, payload.target_date)

    @app.get("/audit/evidence/{evidence_id}", response_model=EvidenceResponse)
    def get_audit_evidence(evidence_id: str, request: Request):
        """只读复验冻结快照：重新计算摘要并标记 verified，不回查修订数据。"""
        return read_evidence(request.app.state.db, evidence_id)

    return app


app = create_app()
