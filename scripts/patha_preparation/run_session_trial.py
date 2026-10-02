"""Single authorized real session-v2 trial. No restart, recovery or algorithm changes."""
import asyncio, json, pathlib, time, hashlib, os, traceback
from datetime import datetime, timezone
from uuid import UUID, uuid4
from copy import deepcopy
from collections import Counter
import asyncpg, httpx
from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import PgRepository
from contexthub.services.execution_ledger import ExecutionLedger
from contexthub.services.session_models import SessionConfiguration
from contexthub.models.knowledge import canonical_hash, ContractError
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.propagation.candidate import ModelConfig
from integrations.memebench.patha.session_factory import create_session_components,SessionExecutionGrant,run_session_arm
from integrations.memebench.patha.session_evaluation import judge_session_result,session_costs
from integrations.memebench.patha.official_adapter import OfficialJudgeRunner,load_official_class
from integrations.memebench.patha.answer_prompt import OfficialAnswerConfig
from integrations.memebench.patha.run_gates import FileIdentity
from integrations.memebench.patha.database_snapshot import DatabaseIdentity,check_instance
from integrations.memebench.patha.schemas import MaintenanceInput,ScoringSidecar
from integrations.memebench.patha.chronology import EpisodeBoundary
ROOT=pathlib.Path(__file__).resolve().parents[2]
R=None  # Set explicitly by the CLI; importing never opens credentials or starts a run.
def read(n):return json.loads((R/n).read_text())
def save(n,x):
 p=R/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str))
def append(n,x):
 with (R/n).open('a') as f:f.write(json.dumps(x,ensure_ascii=False,default=str)+'\n');f.flush();os.fsync(f.fileno())
def now():return datetime.now(timezone.utc).isoformat()
def event(kind,**kw):
 row={'at':now(),'event':kind,**kw};append('events.jsonl',row);print(json.dumps(row,ensure_ascii=False),flush=True)
class BudgetStop(asyncio.CancelledError):pass
class Budget:
 def __init__(self):self.start=time.monotonic();self.tokens=0;self.reason=None;self.active={};self.records=[];self.episode=None;self.current=None
 def check(self):
  if self.reason:raise BudgetStop(self.reason)
  if time.monotonic()-self.start>=1800:self.reason='wall_limit_1800_seconds'
  elif self.tokens>=1000000:self.reason='known_token_limit_1000000'
  if self.reason:event('budget_stop',reason=self.reason,known_tokens=self.tokens,inflight=list(self.active));raise BudgetStop(self.reason)
 def snapshot(self):return {'elapsed_seconds':time.monotonic()-self.start,'known_tokens':self.tokens,'stop_reason':self.reason,'inflight':list(self.active),'unknown_usage_attempts':sum(r.phase=='finished' and r.usage.completeness!='complete' for r in self.records),'provider_cancellation_confirmed':False,'limits_may_be_exceeded_by_inflight_or_unknown_usage':True}
class Ledger(ExecutionLedger):
 def __init__(self,repo,budget):super().__init__(repo);self.budget=budget
 async def append(self,record):
  b=self.budget;key=str(record.identity.call_id)+'-'+str(record.attempt_no)
  if record.phase=='started':b.check()
  await super().append(record)
  append('ledger.jsonl',record.model_dump(mode='json'));b.records.append(record)
  if record.phase=='started':
   b.active[key]=record;b.current=record
  else:
   b.active.pop(key,None)
   u=record.usage;b.tokens+=max(u.total_tokens or 0,(u.input_tokens or 0)+(u.output_tokens or 0))
   event('attempt_finished',episode=b.episode,role=record.identity.model_role,operation=record.identity.operation,outcome=record.outcome,tokens=u.total_tokens,elapsed=record.wall_seconds,known_total=b.tokens,error=record.error.code if record.error else None)
  save('budget-progress.json',b.snapshot())
class Transport(httpx.AsyncHTTPTransport):
 def __init__(self,budget):super().__init__(retries=0);self.budget=budget
 async def handle_async_request(self,request):
  b=self.budget;b.check();r=b.current
  assert r is not None
  key=str(r.identity.call_id)+'-'+str(r.attempt_no)
  body=await request.aread()
  # Body only, never headers or credentials. Actual response archives scrub keys.
  save('requests/'+key+'.json',{'identity':r.identity.model_dump(mode='json'),'attempt_no':r.attempt_no,'request':json.loads(body)})
  return await super().handle_async_request(request)
class SessionJudge:
 """Explicit session grant, same unmodified official judgment method and prompt.
 The legacy constructor's fixed-graph manifest is inapplicable to this protocol.
 This adapter requires the frozen trial authorization and exact config/source.
 """
 judge=OfficialJudgeRunner.judge
 close=OfficialJudgeRunner.close
 def __init__(self,config,source,account,key,ledger,budget,authorization):
  assert authorization==read('authorization.json') and authorization['protocol']=='session-evidence-v2'
  if config != ModelConfig.model_validate(read('judge-configuration.json')):
   raise ContractError('session_judge_configuration_mismatch')
  if source != FileIdentity.model_validate(read('official-source.json')):
   raise ContractError('session_judge_source_mismatch')
  source.verify()
  assert config.prompt_template=='{input_json}' and config.request_parameters.response_format=='json_object'
  config.request_parameters.require_resolved()
  self.judge_class=load_official_class(source);self.source=source;self.config=config;self.account_id=account;self.ledger=ledger
  self.client=OpenAIChatClient(key,model=config.model,base_url=config.base_url,temperature=config.temperature,request_parameters=config.request_parameters,timeout=config.retry.timeout_seconds,transport=Transport(budget))
async def main():
 assert not (R/'run-start.json').exists(),'Single execution only'
 frozen=read('freeze.json')
 assert str(pathlib.Path(__file__).resolve()) in frozen,'Runner must be included in frozen identity'
 for path,sha in frozen.items():assert hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()==sha,'Frozen file changed'
 assert __import__('subprocess').check_output(['git','branch','--show-current'],text=True).strip()=='codex/s0-active-only'
 cfg=SessionConfiguration.model_validate(read('configuration.json'));answer=OfficialAnswerConfig.model_validate(read('answer-configuration.json'));jc=ModelConfig.model_validate(read('judge-configuration.json'));auth=read('authorization.json')
 # Authorized credential loading. Only key and endpoint equality are inspected.
 providers=json.loads((ROOT/'model_providers.local.json').read_text())
 chat=next(t for t in providers['targets'] if t['label']=='openlux');emb=next(t for t in providers['targets'] if t['label']=='aliyun')
 assert chat['base_url']==cfg.models['base'].base_url and emb['base_url']==cfg.embedding.base_url
 keys={role:chat['api_key'] for role in (*cfg.models,'answer')};keys['embedding']=emb['api_key'];judge_key=chat['api_key']
 assert all(isinstance(k,str) and k for k in keys.values());del providers,chat,emb
 db=read('database.json');identity=DatabaseIdentity.model_validate(db['identity'])
 admin=await asyncpg.connect(db['admin_dsn'])
 try:await check_instance(admin,identity)
 finally:await admin.close()
 pool=await asyncpg.create_pool(f'postgresql://{identity.ordinary_role}@127.0.0.1:{identity.port}/{identity.database}',min_size=1,max_size=3,init=init_pg_connection)
 async with pool.acquire() as conn:await check_instance(conn,identity,ordinary=True)
 repo=PgRepository(pool);scopes=read('scopes.json');mapping=read('episode-map.json');runtimes={r['episode_id']:MaintenanceInput.model_validate(r) for r in read('maintenance-inputs.json')};boundaries={k:EpisodeBoundary.model_validate(v) for k,v in read('boundaries.json').items()}
 # Check all three accounts for both episodes before starting budget/model calls.
 for scope in scopes.values():
  for account in [scope['maintenance'],scope['answer'],scope['judge']]:
   async with repo.session(account) as conn:
    for table in ['knowledge_artifacts','contexts','execution_attempts']:
     assert not await conn.fetchval(f'SELECT EXISTS(SELECT 1 FROM {table} WHERE account_id=$1)',account)
 b=Budget();ledger=Ledger(repo,b);save('run-start.json',{'started_at':now(),'configuration_sha256':cfg.fingerprint(),'authorization_sha256':canonical_hash(auth),'database_verified':True,'empty_accounts_verified':True,'budget':b.snapshot()})
 labels=None;results={};runid=uuid4()
 async def watcher(task):
  while not task.done():
   await asyncio.sleep(1)
   if time.monotonic()-b.start>=1800:
    b.reason='wall_limit_1800_seconds';event('deadline_cancel',inflight=list(b.active));task.cancel();return
 timer=asyncio.create_task(watcher(asyncio.current_task()))
 try:
  for rawid in ['pl_001','sw_004']:
   if b.reason:break
   b.check();b.episode=rawid;scope=scopes[rawid];rt=runtimes[mapping[rawid]];bound=boundaries[rt.episode_id];stream=UUID(scope['stream_id'])
   grant=SessionExecutionGrant('session-evidence-v2',cfg.fingerprint(),canonical_hash(answer),scope['maintenance'],scope['answer'],stream,canonical_hash(auth))
   comp=create_session_components(repository=repo,config=cfg,answer_config=answer,account_id=scope['maintenance'],answer_account_id=scope['answer'],stream_id=stream,keys=keys,transport=Transport(b),grant=grant,ledger=ledger)
   # Pure archive wrapper: no changes to the checkpoint or model behavior.
   original_save=comp.service.store.save
   async def archived_save(checkpoint,expected,_save=original_save,_rawid=rawid):
    await _save(checkpoint,expected)
    save(_rawid+'/checkpoints/'+str(checkpoint.revision)+'.json',checkpoint.model_dump(mode='json'))
    event('checkpoint',episode=_rawid,revision=checkpoint.revision,ordinal=len(checkpoint.sources)-1,status=checkpoint.status,states=dict(Counter(n.status for n in checkpoint.state.nodes)))
   comp.service.store.save=archived_save
   started=time.monotonic();event('episode_start',episode=rawid)
   try:result=await run_session_arm(comp,rt,bound,question_directory=R/'questions')
   except asyncio.CancelledError:
    result=deepcopy(getattr(comp.runner,'progress',{'protocol':'session-evidence-v2','sessions':[],'states':{},'answers':{},'judge_executed':False}))
    result.update(episode_id=rt.episode_id,input_sha256=rt.input_sha256,arm_status='stopped',failure=b.reason or 'cancelled')
   result['wall_seconds']=time.monotonic()-started;results[rawid]=result;save(rawid+'/result.json',result)
   # Privileged labels are opened only by the independent post-trajectory scorer.
   if labels is None:labels=[ScoringSidecar.model_validate(r) for r in read('judge-labels.json')]
   judge=None
   try:
    if not b.reason:
     judge=SessionJudge(jc,FileIdentity.model_validate(read('official-source.json')),scope['judge'],judge_key,ledger,b,auth)
     graded=await judge_session_result(result,boundary=bound,question_directory=R/'questions',labels=labels,judge=judge,run_id=runid,replicate_id=0,profile='adaptive')
     save(rawid+'/judgments.json',graded)
   except asyncio.CancelledError:event('judge_stopped',episode=rawid,reason=b.reason)
   finally:
    if judge:await judge.close()
   event('episode_end',episode=rawid,status=result['arm_status'],wall_seconds=result['wall_seconds'])
   if result['arm_status']!='complete':
    b.reason=b.reason or 'episode_failed_cannot_continue';break
 except BaseException as exc:
  b.reason=b.reason or type(exc).__name__
  save('failure.json',{'type':type(exc).__name__,'frames':[{'file':f.filename,'line':f.lineno,'name':f.name} for f in traceback.extract_tb(exc.__traceback__)]})
 finally:
  timer.cancel()
  for rawid in ['pl_001','sw_004']:
   if rawid not in results:
    rt=runtimes[mapping[rawid]];results[rawid]={'protocol':'session-evidence-v2','episode_id':rt.episode_id,'input_sha256':rt.input_sha256,'sessions':[],'states':{},'answers':{},'judge_executed':False,'arm_status':'not_executed','failure':b.reason};save(rawid+'/result.json',results[rawid])
   scope=scopes[rawid];records=[r for r in b.records if r.identity.account_id in [scope['maintenance'],scope['answer'],scope['judge']]]
   save(rawid+'/costs.json',session_costs(records,wall_seconds=results[rawid].get('wall_seconds')))
  save('run-end.json',{'ended_at':now(),'budget':b.snapshot(),'episodes':{k:v['arm_status'] for k,v in results.items()},'human_review':'not performed'})
  await pool.close();event('run_end',**b.snapshot())
if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser(description='Run one separately authorized, frozen two-episode session trial.')
 parser.add_argument('artifact_directory',type=pathlib.Path)
 R=parser.parse_args().artifact_directory.resolve()
 asyncio.run(main())
