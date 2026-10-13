"""Generate development-only dashboard/DB secrets; never reads production secrets."""
import secrets,json,os
from pathlib import Path
p=Path('secrets');p.mkdir(exist_ok=True);p.chmod(0o700)
for name,payload in [('v2-db.txt',secrets.token_urlsafe(32)),('v2-auth.json',json.dumps({'username':'v2admin','password':secrets.token_urlsafe(24),'csrf':secrets.token_urlsafe(32)}))]:
 file=p/name
 if not file.exists():
  fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'w') as f:f.write(payload)
print('Development secrets exist in ignored owner-only secrets/; values are not logged.')
