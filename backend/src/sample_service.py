from sqlalchemy import select
from fastapi import HTTPException
from .models import Stratum,Interpretation,SampleInterval,MissingInterval
from .common import hole,get,pack,audit,interval,overlaps
from . import strata_service as strata


def validate(s,h,p,exclude=None):
    interval(p.start_m,p.end_m,h.total_depth);st=get(s,Stratum,p.stratum_id);i=get(s,Interpretation,st.interpretation_id)
    if i.borehole_id!=h.id or i.state!='confirmed' or p.start_m<st.start_m or p.end_m>st.end_m:raise HTTPException(409,'取样须位于本孔同一已确认层段内')
    for m in s.scalars(select(MissingInterval).where(MissingInterval.borehole_id==h.id)):
        if overlaps(p.start_m,p.end_m,m.start_m,m.end_m):raise HTTPException(409,'缺芯区间不可取样')
    active=[x for x in s.scalars(select(SampleInterval).where(SampleInterval.borehole_id==h.id,SampleInterval.state!='cancelled'))if x.id!=exclude]
    if p.destructive:
        if any(x.destructive and overlaps(x.start_m,x.end_m,p.start_m,p.end_m)for x in active):raise HTTPException(409,'破坏性切样区间与已有取样重叠')
        if sum(x.end_m-x.start_m for x in active if x.destructive)+p.end_m-p.start_m>h.cut_quota+1e-8:raise HTTPException(409,'切样累计长度超过本孔破坏性配额')


def _gate(s,st,p):
    i,signatures=strata.gate(s,st)
    p.gate_epoch=i.contact_version or 0;p.gate_signatures=signatures


def create(s,p,u):
    h=hole(s,p.borehole_id);validate(s,h,p);st=get(s,Stratum,p.stratum_id)
    gate=SampleInterval(**p.model_dump());_gate(s,st,gate)
    s.add(gate);s.flush();audit(s,u,'申请切样 '+gate.code+'（界面签认已核）');return pack(gate)


def revise(s,id,p,u):
    ref=get(s,SampleInterval,id);h=hole(s,ref.borehole_id);x=get(s,SampleInterval,id,True)
    if x.state!='planned' or p.borehole_id!=h.id or p.code!=x.code:raise HTTPException(409,'仅未封签计划可修订，孔号和样号固定')
    validate(s,h,p,id)
    for k,v in p.model_dump().items():setattr(x,k,v)
    _gate(s,get(s,Stratum,p.stratum_id),x)
    audit(s,u,'修订切样区间 '+x.code+'（界面签认已复核）');return pack(x)


def cancel(s,id,u):
    ref=get(s,SampleInterval,id);hole(s,ref.borehole_id);x=get(s,SampleInterval,id,True)
    if x.state!='planned':raise HTTPException(409,'只有未封签计划可撤销释放配额')
    x.state='cancelled';audit(s,u,'撤销取样 '+x.code);return pack(x)


def assert_released(s,sample):
    """封签是切样的最后一道放行：界面若在放行后被挪动或重签已失效，拒绝封签。"""
    ok,msg=strata.gate_ok(s,sample)
    if not ok:raise HTTPException(409,msg)
