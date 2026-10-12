"""Every real model request reserves shared quota and records sanitized usage.

Only enabled inside a D3 local round. Unknown usage stays conservatively charged
in Redis; no request body/key/screenshot is placed in a queue or usage table.
"""
from contextvars import ContextVar
from functools import lru_cache
from uuid import uuid4
from sqlalchemy import text

from .redis_control import RedisControl,ProviderLimits
from .settings import Settings
from .workbench_storage import database
from .repository import digest,encode

MODEL_SCOPE=ContextVar('zhida_model_scope',default=None)


@lru_cache(maxsize=1)
def control():
    from redis import Redis
    cfg=Settings.from_env()
    if not cfg.redis_control_url:raise ValueError('协调服务未配置，暂停新的模型调用和浏览器写入')
    return RedisControl(Redis.from_url(cfg.redis_control_url,socket_timeout=2,socket_connect_timeout=2),
                        prefix=f'zhida:v2:{cfg.environment}')


def require_write_control(user):
    decision=control().rate_limit(user_id=user,route_group='runner_write_guard',capacity=4000,
                                 refill_per_second=100)
    if not decision.allowed:raise ValueError('执行器频率预算已用完，暂停后续写入')


class ModelReservation:
    def __init__(self,scope,model,request_material):
        self.scope,self.model,self.id=scope,model,str(uuid4())
        self.estimated=min(200000, max(1000,len(str(request_material))+12000))
        self.account='configured-model-account'

    def start(self):
        decision=control().reserve_provider(account=self.account,call_id=self.id,
            limits=ProviderLimits(20,1000000,2),estimated_tokens=self.estimated,timeout_ms=600000)
        if not decision.allowed:raise ValueError('配置模型的共享调用预算已用完，保留确定项并等待')
        try:
            with database().transaction(self.scope['user_id']) as cx:
                cx.execute(text("""INSERT INTO zhida.model_calls(id,user_id,run_id,provider,model,request_hash,reserved_tokens)
                    VALUES(:id,:u,:r,'configured-provider',:model,:hash,:tokens)"""),
                    {'id':self.id,'u':self.scope['user_id'],'r':self.scope['run_id'],'model':self.model,
                     'hash':digest([self.model,self.id]),'tokens':self.estimated})
            if not control().mark_provider_sent(account=self.account,call_id=self.id):
                raise ValueError('模型配额状态变化，未派发请求')
        except BaseException:
            control().settle_provider(account=self.account,call_id=self.id,state='cancelled_before_send')
            raise

    def finish(self,usage=None):
        tokens=getattr(usage,'total_tokens',None) if usage else None
        known=isinstance(tokens,int) and tokens>=0
        actual={'total_tokens':tokens} if known else None
        with database().transaction(self.scope['user_id']) as cx:
            cx.execute(text('UPDATE zhida.model_calls SET status=:state,actual_usage=CAST(:usage AS jsonb),finished_at=now() '
                'WHERE user_id=:u AND id=:id'),{'id':self.id,'u':self.scope['user_id'],
                'state':'completed' if known else 'unknown','usage':encode(actual)})
        control().settle_provider(account=self.account,call_id=self.id,
            actual_tokens=tokens if known else None,state='completed' if known else 'unknown')
