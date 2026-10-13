import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from db import DB
from modules.media import MediaModule

class MediaWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(pathlib.Path(self.tmp.name)/'news.sqlite'))
        wid=self.db.create_workspace('Sport')
        self.a = MediaModule(SimpleNamespace(db=self.db,user=None))
        self.b = MediaModule(SimpleNamespace(db=self.db.workspace(wid),user=None))
    async def asyncTearDown(self):
        self.db.conn.close();self.tmp.cleanup()
    async def test_missing_workspace_cache_state_is_read_only_zero(self):
        root=pathlib.Path(self.tmp.name)/'media'
        self.assertFalse(root.exists())
        self.assertEqual(self.a.panel_info(), {'cached':0})
        self.assertEqual(self.b.cache_files(), [])
        self.assertFalse(root.exists(), 'GET state must not create media directories')
        root.mkdir(mode=0o700)
        self.assertEqual(self.b.panel_info(), {'cached':0})
        self.assertFalse((root/'workspace-2').exists())

    def test_distinct_workspace_directories(self):
        self.assertEqual(pathlib.Path(self.a.media_dir), pathlib.Path(self.tmp.name)/'media/workspace-1')
        self.assertEqual(pathlib.Path(self.b.media_dir), pathlib.Path(self.tmp.name)/'media/workspace-2')
        self.assertNotEqual(self.a.media_dir,self.b.media_dir)

    async def test_same_source_download_is_cached_independently(self):
        class Reader:
            def __init__(self, data):self.data=data;self.downloads=0
            async def get_messages(self,*args,**kwargs):return SimpleNamespace(photo=True)
            async def download_media(self,*args,**kwargs):self.downloads+=1;return self.data
        self.a.app.user=Reader(b'politics-photo');self.b.app.user=Reader(b'sport-photo')
        draft={'media':('same_source',123)}
        self.assertEqual(await self.a.fetch(draft),b'politics-photo')
        self.assertEqual(await self.b.fetch(draft),b'sport-photo')
        self.assertEqual(await self.a.fetch(draft),b'politics-photo')
        self.assertEqual(self.a.app.user.downloads,1)
        self.assertEqual(self.b.app.user.downloads,1)
        self.assertEqual(self.a.panel_info(),{'cached':1})
        self.assertEqual(self.b.panel_info(),{'cached':1})

    async def test_cleanup_only_removes_expired_files_in_selected_workspace(self):
        import os,time
        pa=pathlib.Path(self.a.media_dir)/'old.jpg';pb=pathlib.Path(self.b.media_dir)/'old.jpg'
        pa.write_bytes(b'A');pb.write_bytes(b'B')
        old=time.time()-15*86400
        os.utime(pa,(old,old));os.utime(pb,(old,old))
        await self.a.on_tick()
        self.assertFalse(pa.exists());self.assertEqual(pb.read_bytes(),b'B')
        self.assertEqual(self.a.panel_info(),{'cached':0});self.assertEqual(self.b.panel_info(),{'cached':1})
        self.assertNotEqual(self.a.get('last_cleanup'),'')
        self.assertEqual(self.b.get('last_cleanup'),'')

    async def test_counter_and_cleanup_ignore_directories_and_symlinks(self):
        import os,time
        root=pathlib.Path(self.a.media_dir)
        (root/'photo.jpg').write_bytes(b'photo')
        nested=root/'subdirectory';nested.mkdir();(nested/'keep.jpg').write_bytes(b'keep')
        external=pathlib.Path(self.tmp.name)/'keep-outside.jpg';external.write_bytes(b'outside')
        (root/'link.jpg').symlink_to(external)
        old=time.time()-15*86400;os.utime(nested,(old,old));os.utime(external,(old,old))
        self.assertEqual(self.a.panel_info(),{'cached':1})
        await self.a.on_tick()
        self.assertEqual(external.read_bytes(),b'outside')
        self.assertEqual((nested/'keep.jpg').read_bytes(),b'keep')
