"""Preview the additive private schema on a temporary copy. Never opens provider APIs."""
import argparse,json,sqlite3,tempfile
from pathlib import Path
from app.appointments.registry import Registry


def preview(path=None):
    with tempfile.TemporaryDirectory(prefix='phone-private-schema-') as folder:
        target=Path(folder)/'preview.sqlite3'
        if path and path.exists():
            source=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
            destination=sqlite3.connect(target)
            try:source.backup(destination)
            finally:source.close();destination.close()
        registry=Registry(target)
        with registry.connect() as db:
            tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            version=db.execute('SELECT max(version) FROM appointment_schema').fetchone()[0]
            counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in
                ('booking_operations','private_appointments','booking_outbox')}
        return {'dry_run':True,'schema_version':version,'tables':tables,'counts':counts,
            'source_modified':False,'provider_calls':0,'rollback':'Separate additive DB; preserve journal and reconcile sent writes before disabling.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,help='Optional existing private registry; read-only backup to temporary preview')
    parser.add_argument('--dry-run',action='store_true',required=True)
    args=parser.parse_args();print(json.dumps(preview(args.database),indent=2))
