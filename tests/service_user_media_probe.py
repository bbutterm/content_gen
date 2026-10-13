"""Offline service-UID probe; all files are synthetic and stage-local."""
import os
from pathlib import Path
import pwd
import sys
import tempfile
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from db import DB
from modules.media import MediaModule

stage=Path(__file__).resolve().parents[1]
account=pwd.getpwnam('newsbot')
with tempfile.TemporaryDirectory(prefix='uid-media-',dir=stage/'evidence') as folder:
    path=Path(folder); os.chown(path,account.pw_uid,account.pw_gid); os.chmod(path,0o700)
    media=path/'media'; media.mkdir(mode=0o755)
    def child(create):
        pid=os.fork()
        if pid==0:
            try:
                os.chdir(path)
                os.setgroups([]); os.setgid(account.pw_gid); os.setuid(account.pw_uid)
                db=DB(':memory:'); db.path='synthetic.db'
                wid=db.create_workspace('Sport')
                module=MediaModule(SimpleNamespace(db=db.workspace(wid),user=None))
                assert module.panel_info()=={'cached':0}
                assert not Path('media/workspace-2').exists()
                if create:
                    directory=Path(module.media_dir)
                    assert directory.stat().st_uid==account.pw_uid
                    assert directory.stat().st_mode & 0o777 == 0o700
                db.conn.close()
                os._exit(0)
            except BaseException as e:
                print(type(e).__name__,str(e),flush=True);os._exit(1)
        _,status=os.waitpid(pid,0)
        assert os.waitstatus_to_exitcode(status)==0
    child(False)
    print('PASS newsbot UID: missing workspace panel_info is read-only zero under root-owned non-writable media parent')
    os.chown(media,account.pw_uid,account.pw_gid);os.chmod(media,0o700)
    child(True)
    print('PASS newsbot UID: writable service-owned 0700 parent enables lazy workspace directory creation with service owner and mode0700')
