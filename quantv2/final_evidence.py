"""Original-final export specification: read-only SQL and pure SQLite event flattening."""
LAB_EXPORT_SQL="""BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT json_build_object('kind','lab','state',(SELECT payload FROM lab_state WHERE id=1),
 'trades',coalesce((SELECT json_agg(jsonb_build_object('strategy',strategy)||payload ORDER BY id) FROM trades),'[]'::json),
 'points',coalesce((SELECT json_agg(jsonb_build_object('time',time)||payload ORDER BY id) FROM snapshots),'[]'::json));
COMMIT;"""

def flatten_original(state,events):
    import json
    trades=[]
    for stamp,kind,payload in events:
        if kind=='trades':
            e=json.loads(payload);trades.extend({'time':stamp,'strategy':e['strategy'],**t} for t in e['trades'])
    return {'kind':'original','state':state,'trades':trades,'points':state['samples']}
