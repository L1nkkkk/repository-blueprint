"""Reproducible skeleton acceptance timing on an explicit local repository."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from codemap.core import canonical
from codemap.project import initialize_project
from codemap import skeleton


def benchmark(root, excludes=()):
    with tempfile.TemporaryDirectory(prefix='blueprint-benchmark-') as folder:
        started=perf_counter()
        store=initialize_project(root,Path(folder)/'map',excludes=excludes)
        elapsed=perf_counter()-started
        def fingerprint():
            digest=sha256()
            with skeleton.connection(store) as db:
                for table in ('files','nodes','edges','search'):
                    digest.update(table.encode())
                    for row in db.execute('SELECT * FROM '+table+' ORDER BY 1,2'):
                        digest.update(canonical(tuple(row)).encode('utf-8'))
                        digest.update(b'\n')
            return digest.hexdigest()
        before=fingerprint()
        started=perf_counter()
        again=skeleton.sync(store)
        repeat=perf_counter()-started
        with skeleton.connection(store) as db:
            nodes=[dict(r) for r in db.execute("SELECT * FROM nodes WHERE kind IN ('function','method') ORDER BY id LIMIT 80")]
            counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('files','nodes','edges')}
            lines=sum(len(json.loads(r[0]).get('text','').splitlines()) for r in db.execute('SELECT document FROM files'))
            states={r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM files GROUP BY state')}
        timings={k:[] for k in ('find_symbol','callers','callees','search','repo_map')}
        for n in nodes:
            actions={'find_symbol':lambda:skeleton.find_symbol(store,n['id']),
                     'callers':lambda:skeleton.calls(store,n['id'],depth=3),
                     'callees':lambda:skeleton.calls(store,n['id'],direction='callees',depth=3),
                     'search':lambda:skeleton.search(store,n['name']),
                     'repo_map':lambda:skeleton.repo_map(store,budget_tokens=2000)}
            for name,action in actions.items():
                started=perf_counter()
                action()
                timings[name].append((perf_counter()-started)*1000)
        return {'repository':Path(root).resolve().name,'source_lines':lines,**counts,'states':states,
                'init_seconds':round(elapsed,4),'repeat_seconds':round(repeat,4),
                'repeat_parsed_files':again['parsed_files'],'identical':before==fingerprint(),
                'query_samples_each':len(nodes),
                'p95_ms':{k:round(sorted(v)[max(0,int(len(v)*.95)-1)],4) if v else None for k,v in timings.items()},
                'doctor':skeleton.doctor(store)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path)
    parser.add_argument('--exclude',action='append',default=[])
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    report=benchmark(args.root,args.exclude)
    output=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        args.output.write_text(output+'\n',encoding='utf-8')
    print(output)
    return 0 if report['identical'] and report['doctor']['ok'] and all(v is None or v<100 for v in report['p95_ms'].values()) else 1


if __name__=='__main__':
    raise SystemExit(main())
