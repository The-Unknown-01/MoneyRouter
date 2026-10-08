"""Loopback-only API for the Go gateway."""
from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path
import re
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from .service import Service, Command, VERSION
from .contract import JobResponse

@asynccontextmanager
async def lifespan(app):
    if not os.getenv('AGENT_SERVICE_TOKEN'):
        raise RuntimeError('AGENT_SERVICE_TOKEN must be configured')
    app.state.service = Service(Path(os.getenv('MONEYROUTER_SERVICE_DIR', '.data/web')))
    yield
    app.state.service.close()

app = FastAPI(title='MoneyRouter private API', version=VERSION, lifespan=lifespan)

@app.middleware('http')
async def private(request: Request, call_next):
    expected = os.getenv('AGENT_SERVICE_TOKEN', '')
    if not expected or not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + expected):
        return JSONResponse({'detail': 'Unauthorized'}, status_code=401)
    try:
        length = int(request.headers.get('content-length', '0'))
    except ValueError:
        return JSONResponse({'detail': 'Invalid content length'}, status_code=400)
    if length > 4 * 1024 * 1024:
        return JSONResponse({'detail': 'Request too large'}, status_code=413)
    return await call_next(request)

def user(value):
    if not re.fullmatch(r'u[1-9][0-9]*', value):
        raise HTTPException(422, 'Invalid user')
    return value

@app.get('/healthz')
@app.get('/readyz')
def health(request: Request):
    return {'status': 'ok', 'contract_version': VERSION}

@app.post('/v1/jobs', status_code=202, response_model=JobResponse)
def submit(command: Command, request: Request):
    try:
        return request.app.state.service.submit(command)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

@app.get('/v1/users/{user_id}/jobs/{job_id}', response_model=JobResponse)
def job(user_id: str, job_id: str, request: Request):
    try:
        return request.app.state.service.job(user(user_id), job_id)
    except KeyError as exc:
        raise HTTPException(404, 'Job not found') from exc

@app.get('/v1/schema')
def schema():
    from .contract import TurnResult, MonthTurnResult, PlanResult, SummaryResult, FinanceBriefingResponse
    from ..domain.profile import Profile
    from ..domain.plan import PlanInputs
    return {'contract_version': VERSION, 'schemas': {model.__name__: model.model_json_schema()
        for model in (Command, JobResponse, Profile, PlanInputs, TurnResult, MonthTurnResult, PlanResult, SummaryResult, FinanceBriefingResponse)}}

@app.get('/v1/users/{user_id}/state')
def state(user_id: str, request: Request, period: str = ''):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
        raise HTTPException(422, 'Invalid period')
    return request.app.state.service.state(user(user_id), period)

@app.post('/v1/users/{user_id}/invalidate')
def invalidate(user_id: str, request: Request):
    service = request.app.state.service
    with service.user_lock(user(user_id)):
        service.invalidate(user_id)
    return {'revision': service.revision(user_id)}

@app.post('/v1/users/{user_id}/clear')
def clear(user_id: str, request: Request):
    import shutil
    service = request.app.state.service
    uid = user(user_id)
    with service.user_lock(uid), service.lock:
        if service.db.execute("SELECT count(*) FROM jobs WHERE user=? AND status IN ('queued','running')", (uid,)).fetchone()[0]:
            raise HTTPException(409, '请等待当前任务完成后清空')
        for key in list(service.agents):
            if key[0] == uid:
                service.agents.pop(key).graph.checkpointer.conn.close()
        target = (service.root / 'users' / uid).resolve()
        if target.parent != (service.root / 'users').resolve():
            raise HTTPException(422, 'Invalid path')
        if target.exists():
            shutil.rmtree(target)
        with service.db:
            for table in ('artifacts', 'revisions', 'jobs'):
                service.db.execute('DELETE FROM ' + table + ' WHERE user=?', (uid,))
    return {'status': 'ok'}
