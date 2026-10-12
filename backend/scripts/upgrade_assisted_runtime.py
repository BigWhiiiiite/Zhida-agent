"""Explicit, backed-up native upgrade. Never touches an ATS or calls an LLM."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from native_backend_v2 import BACKEND,ROOT,read_environment,private_write
from workbench_v2_app import status,port_available


def counts_and_hashes(engine):
    # Only hashes/counts go in the manifest, not names, contact details or IDs.
    with engine.connect() as cx:
        tables=list(cx.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='workbench' "
            "AND tablename<>'projections' ORDER BY tablename")).scalars())
        result={}
        for table in tables:
            rows=list(cx.execute(text(f'SELECT to_jsonb(t)::text FROM workbench."{table}" t ORDER BY to_jsonb(t)::text')).scalars())
            result[table]={'count':len(rows),'hash':hashlib.sha256(json.dumps(rows).encode()).hexdigest()}
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--confirm-app-stopped',action='store_true')
    args=parser.parse_args()
    values=read_environment(ROOT,bootstrap=True)
    url=make_url(values['ZHIDA_V2_DATABASE_URL'])
    if url.host!='127.0.0.1' or url.database!='zhida_v2' or url.port!=15432:
        raise ValueError('Only this project native zhida_v2 database is supported')
    if not args.apply:
        print('Plan: stop app → private pg_dump backup → Alembic head → scoped memory/rule projection → verify unchanged workbench data.');return
    if not args.confirm_app_stopped or any(x=='running' for x in status().get('processes',{}).values()) or not port_available(18000):
        raise ValueError('Stop all managed application processes before applying; nothing changed')
    # Superuser is used only for the consistent full backup/counts. All domain
    # projections below use the limited runtime role with explicit tenant scope.
    admin=url.set(username='zhida_native_admin',password=values['ZHIDA_V2_NATIVE_ADMIN_PASSWORD'])
    engine=create_engine(admin,hide_parameters=True)
    root=BACKEND/'.v2-runtime'/'upgrades';root.mkdir(mode=0o700,exist_ok=True)
    directory=Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S-'),dir=root))
    env=dict(os.environ);env.update(PGPASSWORD=admin.password,PYTHON_DOTENV_DISABLED='1')
    backup=directory/'before.dump'
    subprocess.run([str(ROOT/'bin/postgres/bin/pg_dump'),'-h','127.0.0.1','-p','15432','-U',admin.username,
        '-d','zhida_v2','-Fc','-f',str(backup)],env=env,check=True,capture_output=True,timeout=60)
    backup.chmod(0o600)
    before=counts_and_hashes(engine)
    env['ZHIDA_V2_DATABASE_URL']=values['ZHIDA_V2_MIGRATION_DATABASE_URL']
    subprocess.run([sys.executable,str(BACKEND/'scripts/migrate_backend_v2.py')],cwd=BACKEND,env=env,check=True,timeout=60)
    os.environ['ZHIDA_V2_DATABASE_URL']=values['ZHIDA_V2_DATABASE_URL']
    sys.path.insert(0,str(BACKEND))
    from app.backend_v2.db import Database
    from app.backend_v2.workbench_projection import sync_user
    with engine.connect() as cx:
        users=list(cx.execute(text("SELECT DISTINCT user_id FROM workbench.candidate_profiles UNION SELECT DISTINCT user_id FROM workbench.application_knowledge")).scalars())
    db=Database(values['ZHIDA_V2_DATABASE_URL'])
    try:
        for user in users:
            with db.transaction(user) as cx:sync_user(cx,user,{'candidate_profiles','application_knowledge'})
    finally:db.close()
    after=counts_and_hashes(engine)
    with engine.connect() as cx:revision=cx.execute(text('SELECT version_num FROM public.alembic_version')).scalar_one()
    engine.dispose()
    equal=before==after
    private_write(directory/'manifest.json',json.dumps({'schema_revision':revision,'workbench_unchanged':equal,
        'tables':after,'backup_sha256':hashlib.sha256(backup.read_bytes()).hexdigest()},indent=2))
    if not equal:raise RuntimeError('Workbench changed during upgrade; app remains stopped. Inspect private backup/manifest.')
    print(json.dumps({'schema_revision':revision,'workbench_unchanged':equal,'backup':str(backup),
        'records':sum(item['count'] for item in after.values())}))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Upgrade stopped: '+type(exc).__name__+'. No credentials printed; app not started.',file=sys.stderr)
        raise SystemExit(1)
