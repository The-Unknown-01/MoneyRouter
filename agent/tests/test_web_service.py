"""Contract, isolation, explicit confirmation and durable recovery tests."""
import base64
import json
import threading
import time
import pytest
from moneyrouter_agent.api.service import Service, Command, public
from moneyrouter_agent.config import Settings

@pytest.fixture
def service(tmp_path, monkeypatch, business_clock):
 business_clock("2026-09-10")
 monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls,*a,**k:Settings(api_key=None,key_file=None)))
 from moneyrouter_agent.plan_agent import PlanAgent
 from moneyrouter_agent.domain.plan_turn import PlanTurnDecision
 from moneyrouter_agent.domain.wallet import Wallet, WalletProposal
 class ScriptedPlanner(PlanAgent):
  def __init__(self, **kwargs):
   def decision(messages):
    facts=self.context.inputs
    wallets=[Wallet(id=c.category,name=c.category,kind="expense",category=c.category,amount_cents=c.amount_cents,
       reason="用户确认的本月费用",execution="核对实际") for c in (facts.snapshot.categories if facts.planning_mode == "adjustment" else facts.snapshot.obligations)]
    from moneyrouter_agent.domain.month import planning_income_cents
    surplus=planning_income_cents(facts.snapshot)-sum(w.amount_cents for w in wallets)
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
 from moneyrouter_agent.history import MonthRecord
 from moneyrouter_agent.domain.month import MonthSnapshot, IncomeFact, CategorySpend, recompute
 history=s.stores(user)[0]
 history.save(MonthRecord(period='2026-08',snapshot=recompute(MonthSnapshot(period='2026-08',coverage_complete=True,
     income=IncomeFact(amount_cents=800000),categories=[CategorySpend(category='居住',amount_cents=200000)]))))
 s.put(user,'month_semantics_version',2,'2026-08')
 s.execute(command('summary',user,period='2026-08'))
 s.execute(command('summary',user,'confirm',period='2026-08'))
 s.execute(command('forecast',user,'finish',{'expected_income_cents':800000,'obligations_reviewed':True,
     'obligations':[{'id':'rent','category':'居住','label':'房租','amount_cents':200000,'evidence':'用户明确的下月房租'}]}))
 s.execute(command('forecast',user,'confirm'))
 doc={'period':'2026-09','cashflow':[{'date':'2026-09-01','direction':'income','amount':'8000.00','category':'工资'},{'date':'2026-09-02','direction':'expense','amount':'2000.00','category':'居住'},{'date':'2026-09-03','direction':'transfer','amount':'1000.00'}]}
 s.execute(command('month',user,payload={'bill':json.dumps(doc)}))
 s.execute(command('month',user,'finish',{'non_invested_cents':600000,'invested_cents':0,'has_investments':False,'obligations_reviewed':True}))
 s.execute(command('month',user,'confirm'))

def test_profile_null_and_zero(service):
 r=service.execute(command('manual',action='confirm',payload={'max_loss_pct':0,'debt_cents':0}))
 assert r['final_profile']['income_cents'] is None
 assert r['final_profile']['max_loss_pct']==0
 assert service.state('u2','2026-09')['profile'] is None


def test_legacy_plan_conversation_does_not_expose_tool_loop(service):
 service.put('u1', 'plan_result', {'reply': '请核对收入。', 'messages': [
  {'role': 'user', 'content': '生成方案'},
  {'role': 'assistant', 'content': "I'll start by reading facts."},
  {'role': 'assistant', 'content': 'Let me test the stress scenario.'},
  {'role': 'assistant', 'content': '请核对收入。'}]}, '2026-09')
 result = service.state('u1', '2026-09')['plan_result']
 assert result['messages'] == [{'role': 'user', 'content': '生成方案'}, {'role': 'assistant', 'content': '请核对收入。'}]


def test_plan_service_only_persists_public_replies(service):
 seed(service)
 result = service.execute(command('plan'))
 assert result['conversation_version'] == 1
 assert result['messages'][-1]['content'] == result['reply']
 assert service.state('u1', '2026-09')['plan_result']['messages'] == result['messages']

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

def test_full_loop_and_stale(service, business_clock):
 seed(service)
 state=service.state('u1','2026-09')
 assert state['month_current']
 assert state['month_result']['snapshot']['spend_total_cents']==200000
 service.execute(command('plan'))
 service.execute(command('plan',action='confirm'))
 assert not service.state('u1','2026-09')['stale']
 business_clock("2026-10-08")
 history=service.stores('u1')[0]
 record=history.load('2026-09')
 record.snapshot.coverage_complete=True
 history.save(record)
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


def test_next_month_forecast_is_separate_from_actual_history(service):
 seed(service)
 before=service.stores('u1')[0].load('2026-09').model_dump()
 result=service.execute(command('plan'))
 facts=result['plan']['input_facts']
 assert facts['planning_mode']=='next_month' and facts['source_period']=='2026-08'
 assert facts['month']['income'] is None and facts['month']['categories']==[]
 assert facts['month']['expected_income']['amount_cents']==800000
 assert any(s['period']=='2026-08' for s in facts['history'])
 assert service.stores('u1')[0].load('2026-09').model_dump()==before
 service.execute(command('plan',action='confirm'))
 adjustment=command('plan',payload={'planning_mode':'adjustment'})
 adjusted=service.execute(adjustment)
 assert adjusted['plan']['input_facts']['month']['spend_total_cents']==200000
 assert service.thread(adjustment)!=service.thread(command('plan'))


def test_forecast_requires_adjacent_final_review_and_fresh_inputs(service):
 seed(service)
 with pytest.raises(ValueError,match='上一月'):
  service.execute(command('forecast',action='finish',period='2026-10',payload={'expected_income_cents':0,'obligations_reviewed':True}))
 service.execute(command('forecast',action='finish',payload={'expected_income_cents':0,'obligations_reviewed':True}))
 result=service.execute(command('forecast',action='confirm'))
 assert result['confirmed'] and result['snapshot']['expected_income']['amount_cents']==0
 assert service.state('u1','2026-09')['month_current']
 assert not service.state('u1','2026-09')['month_draft_stale']
 service.invalidate('u1')
 with pytest.raises(ValueError,match='预计资料'):
  service.execute(command('plan'))
 assert not service.state('u1','2026-09')['forecast_current']


def test_forecast_rejects_stale_review_after_source_bill_changes(service):
 seed(service)
 history=service.stores('u1')[0]
 record=history.load('2026-08')
 record.snapshot.income.amount_cents+=1
 history.save(record)
 with pytest.raises(ValueError,match='账单已更新'):
  service.execute(command('forecast',action='finish',payload={'expected_income_cents':800000,'obligations_reviewed':True}))
