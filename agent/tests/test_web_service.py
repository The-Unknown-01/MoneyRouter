"""Contract, isolation, explicit confirmation and durable recovery tests."""
import base64
import json
import threading
import time
import pytest
from moneyrouter_agent.api.service import Service, Command, public
from moneyrouter_agent.config import Settings

@pytest.fixture
def service(tmp_path, monkeypatch):
 monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls,*a,**k:Settings(api_key=None,key_file=None)))
 from moneyrouter_agent.plan_agent import PlanAgent
 from moneyrouter_agent.domain.plan_turn import PlanTurnDecision
 from moneyrouter_agent.domain.wallet import Wallet, WalletProposal
 class ScriptedPlanner(PlanAgent):
  def __init__(self, **kwargs):
   def decision(messages):
    facts=self.context.inputs
    wallets=[Wallet(id=c.category,name=c.category,kind="expense",category=c.category,amount_cents=c.amount_cents,
       reason="用户确认的本月费用",execution="核对实际") for c in facts.snapshot.categories]
    surplus=facts.snapshot.income.amount_cents-sum(w.amount_cents for w in wallets)
    wallets.append(Wallet(id="goal",name="目标储蓄",kind="goal",amount_cents=surplus,reason="测试用户目标",execution="单独留存"))
    return PlanTurnDecision(reply="请核对",status="finalize",proposal=WalletProposal(headline="测试方案",wallets=wallets))
   super().__init__(decide_runner=decision,**kwargs)
 monkeypatch.setattr('moneyrouter_agent.api.service.PlanAgent',ScriptedPlanner)
 s=Service(tmp_path/'service')
 yield s
 s.close()

def command(kind,user='u1',action='',payload=None,request='request001',period='2026-09'):
 return Command(kind=kind,user_id=user,action=action,payload=payload or {},request_id=request,period=period)

def seed(s,user='u1'):
 s.execute(command('manual',user,'confirm',{'income_cents':800000,'income_stable':True,'debt_cents':0,'reserve_cents':100000,'horizon_months':36,'max_loss_pct':0}))
 doc={'period':'2026-09','cashflow':[{'date':'2026-09-01','direction':'income','amount':'8000.00','category':'工资'},{'date':'2026-09-02','direction':'expense','amount':'2000.00','category':'居住'},{'date':'2026-09-03','direction':'transfer','amount':'1000.00'}]}
 s.execute(command('month',user,payload={'bill':json.dumps(doc)}))
 s.execute(command('month',user,'finish',{'non_invested_cents':600000,'invested_cents':0,'has_investments':False,'obligations_reviewed':True}))
 s.execute(command('month',user,'confirm'))

def test_profile_null_and_zero(service):
 r=service.execute(command('manual',action='confirm',payload={'max_loss_pct':0,'debt_cents':0}))
 assert r['final_profile']['income_cents'] is None
 assert r['final_profile']['max_loss_pct']==0
 assert service.state('u2','2026-09')['profile'] is None

def test_manual_rejects_invalid_values(service):
 with pytest.raises(ValueError):service.execute(command('manual',action='confirm',payload={'horizon_months':601}))

def test_edit_regenerates_valid_wallet_plan(service):
 seed(service)
 original=service.execute(command('plan'))['plan']
 result=service.execute(command('plan',action='edit').model_copy(update={'user_message':'重新核对目标储蓄'}))
 assert result['awaiting_confirmation'] and result['plan']['schema_version']==2
 assert result['plan']['wallets']==original['wallets']
 assert service.execute(command('plan',action='confirm'))['confirmed']


def test_draft_becomes_stale_after_input_changes(service):
 seed(service)
 service.execute(command('plan'))
 assert not service.state('u1','2026-09')['stale']
 service.invalidate('u1')
 state=service.state('u1','2026-09')
 assert state['stale'] and state['month_draft_stale']

def test_confirmation_is_explicit(service):
 seed(service)
 r=service.execute(command('plan'))
 assert r['awaiting_confirmation'] and not r['confirmed']
 with pytest.raises(ValueError):service.execute(command('plan'))
 assert not service.get('u1','plan_result','2026-09')['confirmed']

def test_full_loop_and_stale(service):
 seed(service)
 state=service.state('u1','2026-09')
 assert state['month_current']
 assert state['month_result']['snapshot']['spend_total_cents']==200000
 service.execute(command('plan'))
 service.execute(command('plan',action='confirm'))
 assert not service.state('u1','2026-09')['stale']
 r=service.execute(command('summary'))
 assert r['awaiting_confirmation']
 service.execute(command('summary',action='confirm'))
 assert service.state('u1','2026-09')['stale']
 assert service.stores('u1')[1].load() is not None
 assert service.state('u2','2026-09')['history_periods']==[]

def test_restart_pending_confirmation(service):
 seed(service)
 service.execute(command('plan'))
 # Close only instances and recreate, preserving on-disk state.
 for agent in service.agents.values():agent.graph.checkpointer.conn.close()
 service.agents.clear()
 r=service.execute(command('plan',action='confirm'))
 assert r['confirmed']
 assert len(service.get('u1','versions','2026-09'))==1

def test_reconcile_confirmed_checkpoint_without_projection(service):
 seed(service)
 service.execute(command('plan'))
 service.execute(command('plan',action='confirm'))
 service.put('u1','plan_result',{},'2026-09')
 service.put('u1','confirmed_plan',None,'2026-09')
 result=service.execute(command('plan',action='confirm'))
 assert result['confirmed'] and service.get('u1','confirmed_plan','2026-09')
 assert len(service.get('u1','versions','2026-09'))==1
 assert service.execute(command('month',action='confirm'))['confirmed']
 service.execute(command('plan',action='confirm'))
 assert len(service.get('u1','versions','2026-09'))==1

def test_clean_exact_amount_and_transfer(service):
 doc={'period':'2026-09','cashflow':[{'date':'2026-09-02','direction':'expense','amount':'0.29','category':'餐饮'},{'date':'2026-09-03','direction':'transfer','amount':'100.01'}]}
 r=service.execute(command('clean',payload={'source':'json','content':base64.b64encode(json.dumps(doc).encode()).decode()}))
 assert [row['amount_cents'] for row in r['rows']]==[29,10001]
 assert r['rows'][1]['direction']=='transfer'
 assert r['rows'][0]['fingerprint']

def test_public_observations_removed():
 assert public({'reasoning':'secret','nested':{'write_location':'private','error':'traceback'},'reply':'safe'})=={'nested':{},'reply':'safe'}

def test_jobs_idempotent_and_isolated(service):
 c=command('manual',action='confirm',payload={'income_cents':0})
 job=service.submit(c)
 assert service.submit(c)['job_id']==job['job_id']
 with pytest.raises(KeyError):service.job('u2',job['job_id'])
 with pytest.raises(ValueError):service.submit(c.model_copy(update={'payload':{'income_cents':1}}))
 for _ in range(100):
  result=service.job('u1',job['job_id'])
  if result['status'] not in {'queued','running'}:break
  time.sleep(.01)
 assert result['status']=='succeeded'

def test_state_does_not_wait_for_model(service):
 entered=threading.Event();release=threading.Event()
 original=service.execute
 def slow(c):entered.set();release.wait(3);return original(c)
 service.execute=slow
 job=service.submit(command('manual',action='confirm'))
 assert entered.wait(2)
 started=time.monotonic()
 assert service.state('u1','2026-09')['pending']['job_id']==job['job_id']
 assert time.monotonic()-started<1
 release.set()

def test_api_auth_and_schema(tmp_path,monkeypatch):
 from fastapi.testclient import TestClient
 from moneyrouter_agent.api.server import app
 monkeypatch.setenv('AGENT_SERVICE_TOKEN','test-api-token')
 monkeypatch.setenv('MONEYROUTER_SERVICE_DIR',str(tmp_path/'api'))
 with TestClient(app) as client:
  assert client.get('/healthz').status_code==401
  headers={'authorization':'Bearer test-api-token'}
  assert client.get('/readyz',headers=headers).json()['contract_version']=='1'
  assert client.get('/v1/users/u2/jobs/missing',headers=headers).status_code==404
  assert client.get('/v1/users/../state',headers=headers).status_code!=200
  schema=client.get('/openapi.json',headers=headers).json()
  assert schema['components']['schemas']['Command']['properties']['action']
