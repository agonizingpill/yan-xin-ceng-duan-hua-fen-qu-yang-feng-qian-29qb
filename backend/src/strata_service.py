import hashlib,json
from sqlalchemy import select
from fastapi import HTTPException
from .models import Reading,Interpretation,Stratum,SampleInterval,ContactSign
from .common import hole,get,pack,audit,now


def _signature(depth,upper,lower,evidence):
    """界面签名只取深度、上下岩性、勘验依据：同位置内容相同则签名相同，
    因此“未改处签认继续有效”，而任何一处改动（含挪回原位）都会得到新的签名。"""
    payload=json.dumps({'depth_m':round(float(depth),4),'upper':str(upper),'lower':str(lower),'evidence':(evidence or '').strip()},ensure_ascii=False,sort_keys=True,separators=(',',':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def _rows(s,interpretation_id):
    return s.scalars(select(Stratum).where(Stratum.interpretation_id==interpretation_id).order_by(Stratum.start_m)).all()


def _contacts(rows):
    """内部接触面按“下伏层”计：样段上下界分别是所在层起点与下层层起点。"""
    out=[]
    for seq in range(len(rows)-1):
        a,b=rows[seq],rows[seq+1]
        out.append({'key':str(b.id) if b.id is not None else'new-%d'%seq,'seq':seq,'depth_m':round(b.start_m,4),'upper_lithology':a.lithology,'lower_lithology':b.lithology,'evidence':b.contact_note,'evidence_by':b.evidence_by,'evidence_at':b.evidence_at,'epoch':b.contact_epoch or 0,'signature':b.contact_signature or ''})
    return out


def _sign_state(s,interpretation_id,rows):
    signs={x.key:x for x in s.scalars(select(ContactSign).where(ContactSign.interpretation_id==interpretation_id))}
    out={}
    for c in _contacts(rows):
        sg=signs.get(c['key'])
        if not c['signature'] or len((c['evidence'] or '').strip())<8:
            c['sign_state']='needs_evidence'
        elif sg and sg.state=='valid' and sg.epoch==c['epoch'] and sg.signature==c['signature']:
            c['sign_state']='signed';c['signed_by']=sg.signed_by;c['signed_at']=sg.signed_at
        elif sg:
            c['sign_state']='stale'
        else:
            c['sign_state']='unsigned'
        out[c['seq']]=c
    return out


def contact_view(s,interpretation_id):
    rows=_rows(s,interpretation_id)
    states=_sign_state(s,interpretation_id,rows)
    return [states[k] for k in sorted(states)]


def _mark_stale(s,interpretation_id,rows):
    """按内容核验：签认只对当时那一处界面（层 id + 该版深度/上下岩性/依据签名）有效。"""
    stale=[]
    for sg in s.scalars(select(ContactSign).where(ContactSign.interpretation_id==interpretation_id)):
        cur=next((b for b in rows if str(b.id)==sg.key),None)
        bad=cur is None or sg.epoch!=cur.contact_epoch or sg.signature!=cur.contact_signature
        if bad:
            if sg.state!='stale':sg.state='stale'
            stale.append(sg.seq)
    return stale


def queue(s,p,u):
    h=hole(s,p.borehole_id)
    if s.scalar(select(Interpretation.id).where(Interpretation.borehole_id==h.id,Interpretation.state.in_(['queued','confirmed']))):raise HTTPException(409,'本孔已有排队或已确认解释')
    n=len(s.scalars(select(Reading).where(Reading.borehole_id==h.id)).all())
    if n<12:raise HTTPException(409,'至少12个真实物性测点才可选层数')
    x=Interpretation(borehole_id=h.id,state='queued');s.add(x);s.flush();audit(s,u,'申请GMM层段解释');return pack(x)


def _active_samples(s,interpretation_id):
    ids=select(Stratum.id).where(Stratum.interpretation_id==interpretation_id)
    return s.scalars(select(SampleInterval).where(SampleInterval.stratum_id.in_(ids),SampleInterval.state!='cancelled')).all()


def _remap(s,id,new_rows):
    """样段随界面重挂到新层；有效的破坏性样段跨层则拒绝本次改线。
    已撤销样段只是留档，按其起点所在层归档，避免删除旧层时失去外键。"""
    for smp in s.scalars(select(SampleInterval).where(SampleInterval.stratum_id.in_(select(Stratum.id).where(Stratum.interpretation_id==id)))):
        hits=[r for r in new_rows if r.start_m<=smp.start_m+1e-9 and smp.end_m<=r.end_m+1e-9]
        if len(hits)==1:smp.stratum_id=hits[0].id
        elif smp.state!='cancelled':raise HTTPException(409,'改线后样段 %s 会跨层或失去归属，须先撤销该取样'%smp.code)
        else:
            home=next((r for r in new_rows if r.start_m<=smp.start_m<r.end_m),new_rows[-1])
            smp.stratum_id=home.id


def layers(s,id,p,u):
    ref=get(s,Interpretation,id);h=hole(s,ref.borehole_id);x=get(s,Interpretation,id,True)
    if x.state not in ['ready','confirmed']:raise HTTPException(409,'须算法已就绪或层面已确认才能修订接触面')
    rows=sorted(p.layers,key=lambda z:z.start_m);edge=0
    for z in rows:
        if abs(z.start_m-edge)>1e-6 or z.end_m<=z.start_m:raise HTTPException(409,'人工层段须从0连续衔接，不得缺段或重叠')
        edge=z.end_m
    if abs(edge-h.total_depth)>1e-6:raise HTTPException(409,'人工层段必须完整覆盖孔深')
    # 先按签名匹配同一处界面：内容未变的界面复用原层 id（即签认 key），签认继续有效；
    # 新插入/改动过的界面落到新层或旧层新 epoch，必须重新签认。同一签名只认领一次。
    old=_rows(s,id)
    for i,v in enumerate(old):v.start_m=1e6+i;v.end_m=1e6+i+.5
    s.flush()
    sigs=['']+[_signature(rows[i].start_m,rows[i-1].lithology,rows[i].lithology,rows[i].contact_note)for i in range(1,len(rows))]
    oldsigs=[v.contact_signature or''for v in old];oldepochs=[v.contact_epoch or 0 for v in old]
    used=set();slot=[None]*len(rows)
    for i in range(1,len(rows)):
        for j in range(1,len(old)):
            if j not in used and oldsigs[j]==sigs[i]:slot[i]=j;used.add(j);break
    for i in range(len(rows)):
        if slot[i]is None and i<len(old)and i not in used:slot[i]=i;used.add(i)
    for i,z in enumerate(rows):
        j=slot[i]
        if j is None:
            s.add(Stratum(interpretation_id=id,start_m=z.start_m,end_m=z.end_m,lithology=z.lithology,contact_note=z.contact_note,contact_epoch=0 if i==0 else 1,contact_signature=sigs[i]));continue
        v=old[j];same=i==0 or oldsigs[j]==sigs[i]
        keep=(v.contact_note or'')==z.contact_note and v.lithology==z.lithology
        v.start_m=z.start_m;v.end_m=z.end_m;v.lithology=z.lithology;v.contact_note=z.contact_note
        v.contact_signature=sigs[i];v.contact_epoch=0 if i==0 else(oldepochs[j]if same else oldepochs[j]+1)
        if not keep:v.evidence_by='';v.evidence_at=''
    s.flush()
    # 先安置取样，再删除被合并/移除的旧层，避免外键悬空。
    _remap(s,id,_rows(s,id))
    for j,v in enumerate(old):
        if j not in used:s.delete(v)
    s.flush()
    newrows=_rows(s,id);stale=_mark_stale(s,id,newrows);x.contact_version=(x.contact_version or 0)+1
    audit(s,u,'人工接触面已编录'+(('，%d 处签认失效'%len(stale))if stale else''))
    return {**pack(x),'contacts':contact_view(s,id),'invalidated':stale}


def evidence(s,id,seq,p,u):
    ref=get(s,Interpretation,id);h=hole(s,ref.borehole_id);x=get(s,Interpretation,id,True)
    if x.state not in ['ready','confirmed']:raise HTTPException(409,'须算法已就绪或层面已确认才能补写勘验依据')
    rows=_rows(s,id)
    if not 0<=seq<len(rows)-1:raise HTTPException(404,'该层段没有内部接触面')
    st=rows[seq+1];signature=_signature(st.start_m,rows[seq].lithology,st.lithology,p.reason)
    if signature!=st.contact_signature:st.contact_epoch=(st.contact_epoch or 0)+1
    st.contact_note=p.reason;st.evidence_by=u['email'];st.evidence_at=now();st.contact_signature=signature
    stale=_mark_stale(s,id,rows);x.contact_version=(x.contact_version or 0)+1
    audit(s,u,'补记接触面 %.2fm 勘验依据，原签认失效'%st.start_m)
    return {'stratum':pack(st),'contacts':contact_view(s,id),'invalidated':stale}


def sign(s,id,seq,p,u):
    ref=get(s,Interpretation,id);h=hole(s,ref.borehole_id);x=get(s,Interpretation,id,True)
    if x.state not in ['ready','confirmed']:raise HTTPException(409,'仅候选就绪或已确认的层段可签认接触面')
    rows=_rows(s,id)
    if not 0<=seq<len(rows)-1:raise HTTPException(404,'该层段没有内部接触面')
    st=rows[seq+1]
    if len((st.contact_note or '').strip())<8:raise HTTPException(409,'编录员须先逐处写不少于8字勘验依据')
    if st.contact_epoch!=p.epoch or st.contact_signature!=p.signature:raise HTTPException(409,'接触面已改动，该签认已过期；请按当前深度与依据重新签认')
    key=str(st.id)
    sg=s.scalar(select(ContactSign).where(ContactSign.interpretation_id==id,ContactSign.key==key))
    if not sg:
        sg=ContactSign(interpretation_id=id,seq=seq,key=key);s.add(sg)
    sg.seq=seq;sg.depth_m=st.start_m;sg.epoch=st.contact_epoch;sg.signature=st.contact_signature;sg.evidence=st.contact_note
    sg.evidence_by=st.evidence_by;sg.evidence_at=st.evidence_at;sg.state='valid';sg.signed_by=u['email'];sg.signed_at=now();s.flush()
    audit(s,u,'签认接触面 %.2fm（第%d版）'%(st.start_m,st.contact_epoch))
    return {'contact':_sign_state(s,id,rows)[seq],'contacts':contact_view(s,id)}


def gate(s,st):
    """放行切样所需的签认：样段所在的上下两个内部界面都须有当前有效签认。"""
    if not st:raise HTTPException(409,'取样须引用已确认的层段')
    i=get(s,Interpretation,st.interpretation_id)
    if i.state!='confirmed':raise HTTPException(409,'取样须位于本孔同一已确认层段内')
    rows=_rows(s,i.id)
    idx=next((k for k,r in enumerate(rows)if r.id==st.id),None)
    if idx is None:raise HTTPException(409,'取样层段不属于当前已确认解释')
    need=[k for k in (idx-1,idx)if 0<=k<len(rows)-1]
    out={}
    for seq in need:
        target=rows[seq+1];sg=s.scalar(select(ContactSign).where(ContactSign.interpretation_id==i.id,ContactSign.key==str(target.id)))
        if not sg or sg.state!='valid' or sg.epoch!=target.contact_epoch or sg.signature!=target.contact_signature:
            raise HTTPException(409,'接触面 %.2fm 尚无有效主管签认或签认已失效，不能放行切样'%target.start_m)
        out[str(target.id)]=target.contact_signature
    return i,out


def gate_ok(s,sample):
    st=s.get(Stratum,sample.stratum_id)
    if not st:return False,'样段层段已不存在'
    i=s.get(Interpretation,st.interpretation_id)
    if not i or i.state=='historical':return True,'' # 旧纸本来源无 GMM 层面，保留原历史链条
    if i.state!='confirmed':return False,'所引解释已不是已确认状态'
    if not sample.gate_signatures:return False,'样段没有界面放行记录，须撤销后按签认重新申请'
    rows=_rows(s,i.id)
    for key,signature in sample.gate_signatures.items():
        target=next((b for b in rows if str(b.id)==key),None)
        sg=s.scalar(select(ContactSign).where(ContactSign.interpretation_id==i.id,ContactSign.key==key))
        if not target or target.contact_signature!=signature or not sg or sg.state!='valid' or sg.epoch!=target.contact_epoch or sg.signature!=target.contact_signature:
            return False,'接触面 %.2fm 的签认在放行后已失效，须重新勘验签认并复核样段'%(target.start_m if target else 0)
    return True,''


def confirm(s,id,u):
    ref=get(s,Interpretation,id);hole(s,ref.borehole_id);x=get(s,Interpretation,id,True)
    if s.scalar(select(Interpretation.id).where(Interpretation.borehole_id==x.borehole_id,Interpretation.state=='confirmed',Interpretation.id!=id)):raise HTTPException(409,'本孔已有主管确认的解释，须先撤销未被取样引用的旧解释')
    if x.state!='ready' or not s.scalar(select(Stratum.id).where(Stratum.interpretation_id==id)):raise HTTPException(409,'须算法就绪且人工完整编录层段，不能重复确认')
    x.state='confirmed';audit(s,u,'主管确认地层接触面');return pack(x)


def withdraw(s,id,u):
    ref=get(s,Interpretation,id);hole(s,ref.borehole_id);x=get(s,Interpretation,id,True)
    if x.state not in ['ready','failed','confirmed']:raise HTTPException(409,'仅已完成/失败解释可撤销重编')
    ids=s.scalars(select(Stratum.id).where(Stratum.interpretation_id==id)).all()
    if ids and s.scalar(select(SampleInterval.id).where(SampleInterval.stratum_id.in_(ids))):raise HTTPException(409,'已有取样引用层面，禁止撤销解释')
    x.state='withdrawn';audit(s,u,'撤销解释，允许重测重编');return pack(x)
