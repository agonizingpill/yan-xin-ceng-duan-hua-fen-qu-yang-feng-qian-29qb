import hashlib,json
import numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.mixture import GaussianMixture
from sklearn.model_selection import train_test_split
from sklearn.metrics import adjusted_rand_score
SEED=42
FEATURES=['density','susceptibility','velocity']
LABELS={'density':'密度','susceptibility':'磁化率','velocity':'声速'}
# 候选接触面按相邻测点成分变化的中点给出，测点间距0.5m；容差0.75m用于判定“移动”。
CONTACT_TOL=0.75


def _fit(rows,features):
    if len(rows)<12:raise ValueError('至少12个真实测点')
    raw=np.array([[r[k]for k in features]for r in rows],dtype=float)
    if not np.isfinite(raw).all()or np.any(raw.std(axis=0)<1e-6):raise ValueError('物性缺少变化或包含非有限值')
    train_idx,held_idx=train_test_split(np.arange(len(rows)),test_size=.2,random_state=SEED)
    scaler=StandardScaler().fit(raw[train_idx]);train=scaler.transform(raw[train_idx]);held=scaler.transform(raw[held_idx]);whole=scaler.transform(raw)
    models=[GaussianMixture(n_components=k,random_state=SEED,n_init=5,reg_covar=.01).fit(train)for k in range(1,min(5,len(train)//3)+1)]
    best=min(models,key=lambda m:m.bic(train));prob=best.predict_proba(whole);labels=best.predict(whole)
    sort_col=features.index('density')if'density'in features else 0
    ordered=np.argsort(scaler.inverse_transform(best.means_)[:,sort_col]);rename={int(old):int(new)+1 for new,old in enumerate(ordered)}
    # Holdout readings never enter scaler fitting/BIC choice. Stability compares two train-only fits on the same holdout.
    rng=np.random.default_rng(SEED);boot_idx=rng.choice(len(train),len(train),replace=True);boot=GaussianMixture(n_components=best.n_components,random_state=43,n_init=5,reg_covar=.01).fit(train[boot_idx]);ari=adjusted_rand_score(best.predict(held),boot.predict(held))
    classified=[{'reading_id':r['id'],'depth_m':r['depth_m'],'cluster':rename[int(l)],'posterior':round(float(max(p)),6),'density':r['density'],'susceptibility':r['susceptibility'],'velocity':r['velocity']}for r,l,p in zip(rows,labels,prob)]
    # Holdout labels returned in row order so ablation variants can be compared point-by-point.
    held_ids={rows[i]['id']for i in held_idx}
    held_labels=np.array([l for r,l in zip(rows,labels)if r['id']in held_ids])
    contacts=[round((a['depth_m']+b['depth_m'])/2,4)for a,b in zip(classified,classified[1:])if a['cluster']!=b['cluster']]
    checksum=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    evidence={'algorithm':'StandardScaler + GaussianMixture, train-only BIC','random_state':SEED,'features':features,'source_sha256':checksum,'source_count':len(rows),'train_count':len(train_idx),'holdout_count':len(held_idx),'train_reading_ids':[rows[i]['id']for i in train_idx],'holdout_reading_ids':[rows[i]['id']for i in held_idx],'bic_candidates':[{'k':m.n_components,'bic':round(float(m.bic(train)),6)}for m in models],'chosen_k':best.n_components,'holdout_mean_log_likelihood':round(float(best.score(held)),6),'holdout_stability_ari':round(float(ari),6),'cluster_centers':[{'cluster':rename[int(k)],**{f:round(float(v),5)for f,v in zip(features,c)}}for k,c in enumerate(scaler.inverse_transform(best.means_))],'classified':classified,'suggested_contacts_m':contacts,'note':'成分相似性不是岩性真值；接触面须编录员人工解释并主管确认'}
    return evidence,held_labels


def _compare(full,other,tol=CONTACT_TOL):
    used=set();rows=[]
    for i,d in enumerate(full):
        best=None
        for j,e in enumerate(other):
            if j in used:continue
            dist=abs(e-d)
            if dist<=tol and(best is None or dist<best[0]):best=(dist,j,e)
        if best:
            used.add(best[1]);rows.append({'contact':i,'depth_m':d,'status':'stable'if best[0]<1e-6 else'moved','to_m':best[2],'shift_m':round(best[2]-d,4)})
        else:rows.append({'contact':i,'depth_m':d,'status':'disappeared','to_m':None,'shift_m':None})
    added=[{'depth_m':e}for j,e in enumerate(other)if j not in used]
    return rows,added


def ablation(rows,full_evidence,full_labels):
    """Full three-property fit vs. leave-one-property-out. Same seed, same split,
    same ordering; a contact counts as moved only beyond CONTACT_TOL."""
    full=full_evidence['suggested_contacts_m'];full_held=full_labels
    variants=[];impacts=[{'contact':i,'depth_m':d,'by':{}}for i,d in enumerate(full)]
    for f in FEATURES:
        rest=[k for k in FEATURES if k!=f]
        try:
            ev,held_labels=_fit(rows,rest)
        except ValueError as e:
            variants.append({'feature':f,'label':LABELS[f],'status':'failed','error':str(e),'contacts':[],'changes':[],'extra_contacts':[]})
            for x in impacts:x['by'][f]={'status':'not_available'}
            continue
        changes,added=_compare(full,ev['suggested_contacts_m'])
        # 留出点分类是否被缺失该物性改变：1.0=完全一致，仅作参考，不是岩性准确率。
        agree=round(float(adjusted_rand_score(full_held,held_labels)),6)
        variants.append({'feature':f,'label':LABELS[f],'status':'ok','error':'','chosen_k':ev['chosen_k'],'contacts':ev['suggested_contacts_m'],'holdout_agreement_ari':agree,'changes':changes,'extra_contacts':added})
        for ch in changes:
            impacts[ch['contact']]['by'][f]={'status':ch['status'],'to_m':ch['to_m'],'shift_m':ch['shift_m']}
    for x in impacts:
        moved=[k for k,v in x['by'].items()if v['status']=='moved']
        gone=[k for k,v in x['by'].items()if v['status']=='disappeared']
        x['status']='affected'if moved or gone else('unverified'if any(v['status']=='not_available'for v in x['by'].values())else'stable')
        x['moved_by']=[LABELS[k]for k in moved];x['disappeared_by']=[LABELS[k]for k in gone]
    return {'tolerance_m':CONTACT_TOL,'full_contacts_m':full,'features':variants,'contacts':impacts,'note':'逐项剔除后界面移动或消失的物性；界面结论仍须编录员逐处勘验并主管签认'}


def fit_layers(rows):
    ev,held_labels=_fit(rows,FEATURES)
    return {**ev,'ablation':ablation(rows,ev,held_labels)}
