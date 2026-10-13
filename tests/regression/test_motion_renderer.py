"""Local synthetic renderer acceptance; no API/DB/service calls."""
import asyncio
import copy
import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = os.environ.get('MOTION_TEST_SCRATCH', tempfile.gettempdir())
FFMPEG = os.environ.get('MOTION_TEST_FFMPEG', shutil.which('ffmpeg') or '/usr/bin/ffmpeg')
FFPROBE = os.environ.get('MOTION_TEST_FFPROBE', shutil.which('ffprobe') or '/usr/bin/ffprobe')
DEMO = {'scenes': [
    {'title':'Событие — не просто сообщение', 'text':'Событие сохраняется в журнале. Читатели получают его независимо.', 'icon':'process', 'steps':['Событие','Журнал','Читатель']},
    {'title':'Kafka: журнал событий', 'text':'Упрощённая схема одной партиции. Два читателя идут своим темпом.', 'icon':'network', 'steps':['Продюсер','Партиция','Читатель A','Читатель B']},
    {'title':'Пример: заказ создан', 'text':'Один факт запускает разные реакции. Это не общая транзакция сервисов.', 'icon':'diagram', 'steps':['Заказ создан','Оплата','Склад','Уведомление']},
    {'title':'Храним факт, читаем независимо', 'text':'Порядок показан внутри одной партиции. Пример учебный, не готовая архитектура.', 'icon':'code', 'steps':['Запись','Хранение','Чтение']},
]}


def renderer():
    spec = importlib.util.find_spec('motion_renderer')
    if spec is None:
        raise AssertionError('Motion renderer feature is missing')
    import motion_renderer
    return motion_renderer


class MotionSchemaTests(unittest.TestCase):
    def test_valid_storyboard_is_normalized_without_mutating_input(self):
        m = renderer()
        original = copy.deepcopy(DEMO)
        result = m.validate_storyboard(DEMO)
        self.assertEqual(result, original)
        result['scenes'][0]['steps'][0] = 'changed'
        self.assertEqual(DEMO, original)

    def test_schema_rejects_unbounded_or_executable_inputs(self):
        m = renderer()
        invalid = [None, [], {'scenes': []}, {'scenes': DEMO['scenes'] * 2},
                   {'scenes': DEMO['scenes'], 'code': 'print(1)'}]
        for key, value in [('title', 'x'*61), ('text', 'x'*181), ('title', '  '),
                           ('title', 123), ('icon','unknown'), ('steps',['one']),
                           ('steps', ['a']*5), ('steps',['a'*33,'b']),
                           ('text','https://example.com'), ('text','```python\npass\n```'),
                           ('text','hello\x00world'), ('text','hello\u202eworld'),
                           ('text','file:///etc/passwd')]:
            bad = copy.deepcopy(DEMO)
            bad['scenes'][0][key] = value
            invalid.append(bad)
        for value in invalid:
            with self.subTest(value=repr(value)[:100]):
                with self.assertRaises(ValueError):
                    m.validate_storyboard(value)


class MotionPathTests(unittest.TestCase):
    def test_executable_override_is_absolute_trusted_local_file_not_symlink_or_path_lookup(self):
        m = renderer()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            root = Path(temp)
            linked = root/'ffmpeg-link'
            linked.symlink_to(FFMPEG)
            with self.assertRaises(RuntimeError):
                m._executable(linked,'ffmpeg')
            for path in ('ffmpeg','https://example.com/ffmpeg',root/'missing'):
                with self.assertRaises(RuntimeError):
                    m._executable(path,'ffmpeg')
            self.assertEqual(m._executable(FFMPEG,'ffmpeg'),FFMPEG)

    def test_output_scope_rejects_symlinks_traversal_public_dirs_and_nonfiles(self):
        m = renderer()
        self.assertTrue(hasattr(m, '_open_output_parent'), 'Anchored output-scope feature missing')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            root = Path(temp)
            outside = root/'outside'
            outside.mkdir(mode=0o700)
            (root/'linked').symlink_to(outside, target_is_directory=True)
            (root/'target.mp4').symlink_to(outside/'keep.mp4')
            (root/'folder.mp4').mkdir()
            public = root/'public'
            public.mkdir(mode=0o755)
            invalid = [root/'linked'/'a.mp4',root/'target.mp4',root/'folder.mp4',
                       root/'..'/'escape.mp4',public/'a.mp4',root/'a.png']
            for path in invalid:
                with self.subTest(path=str(path)):
                    with self.assertRaises(ValueError):
                        m._open_output_parent(path)
            fd,name = m._open_output_parent(root/'workspace-1'/'item-1'/'ok.mp4')
            try:
                self.assertEqual(name, 'ok.mp4')
                self.assertEqual(os.fstat(fd).st_mode & 0o777,0o700)
            finally:
                os.close(fd)
            self.assertFalse(list(outside.iterdir()))


class MotionProbeTests(unittest.TestCase):
    def test_probe_process_is_bounded_sanitized_and_killed(self):
        m = renderer()
        self.assertTrue(hasattr(m, '_probe'), 'Bounded probe reader missing')
        from unittest.mock import patch
        import sys
        import threading
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            root = Path(temp)
            candidate = root/'candidate.mp4'
            candidate.write_bytes(b'fixture')
            pidfile = root/'probe.pid'
            executable = root/'fake-probe'
            scripts = [
                ('import os,time\nopen('+repr(str(pidfile))+',"w").write(str(os.getpid()))\ntime.sleep(300)\n','timeout'),
                ('print("x"*200000)\n','limit'),
                ('print("private-token-not-for-errors")\n','invalid'),
            ]
            fd = os.open(candidate, os.O_RDONLY)
            try:
                for script, error in scripts:
                    executable.write_text('#!'+sys.executable+'\n'+script)
                    executable.chmod(0o700)
                    with patch.object(m,'PROBE_TIMEOUT',.2):
                        with self.assertRaisesRegex(RuntimeError,error) as caught:
                            m._probe(str(executable),fd,threading.Event())
                    self.assertNotIn('private-token',str(caught.exception))
                    self.assertNotIn(str(root),str(caught.exception))
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pidfile.read_text()),0)
            finally:
                os.close(fd)

    def test_probe_metadata_rejects_nan_wrong_frames_audio_and_resource_overflow(self):
        m = renderer()
        self.assertTrue(hasattr(m, '_validate_probe'), 'Strict artifact validation missing')
        valid = {'streams':[{'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p',
                             'width':720,'height':1280,'r_frame_rate':'24/1','avg_frame_rate':'24/1',
                             'nb_frames':'576','duration':'24.0'}],
                 'format':{'duration':'24.0','format_name':'mov,mp4,m4a,3gp,3g2,mj2'}}
        self.assertEqual(m._validate_probe(valid,24,50000)['duration'],24.0)
        invalid = []
        for key,value in [('nb_frames','1'),('width',1920),('pix_fmt','rgb24'),('avg_frame_rate','30/1'),
                          ('codec_type','audio'),('duration','nan')]:
            bad = copy.deepcopy(valid)
            bad['streams'][0][key] = value
            invalid.append(bad)
        for value in ('nan','inf','0','999'):
            bad = copy.deepcopy(valid)
            bad['format']['duration'] = value
            invalid.append(bad)
        bad = copy.deepcopy(valid)
        bad['streams'].append({'codec_type':'audio'})
        invalid.extend([bad,{},None])
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    m._validate_probe(value,24,50000)
        for size in (0,100,33*1024*1024):
            with self.assertRaises(RuntimeError):
                m._validate_probe(valid,24,size)


class MotionRenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_encoder_times_out_kills_process_and_preserves_previous_artifact(self):
        m = renderer()
        self.assertTrue(hasattr(m, 'ENCODE_TIMEOUT'), 'Bounded encoder deadline missing')
        from unittest.mock import patch
        import sys
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            root = Path(temp)
            output = root/'motion.mp4'
            output.write_bytes(b'previous-good-artifact')
            pidfile = root/'encoder.pid'
            executable = root/'blocked-encoder'
            executable.write_text('#!'+sys.executable+'\nimport os,time\nopen('+repr(str(pidfile))+',"w").write(str(os.getpid()))\ntime.sleep(300)\n')
            executable.chmod(0o700)
            with patch.object(m, 'ENCODE_TIMEOUT', .3):
                with self.assertRaisesRegex(RuntimeError,'timeout'):
                    await m.render_motion(DEMO, output, ffmpeg_path=executable,ffprobe_path=FFPROBE)
            self.assertEqual(output.read_bytes(), b'previous-good-artifact')
            self.assertFalse(list(root.glob('.motion-*')))
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()),0)

    async def test_async_cancellation_kills_worker_before_return_and_keeps_output(self):
        m = renderer()
        from unittest.mock import patch
        import sys
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            root = Path(temp)
            pidfile = root/'cancel.pid'
            executable = root/'blocked-encoder'
            executable.write_text('#!'+sys.executable+'\nimport os,time\nopen('+repr(str(pidfile))+',"w").write(str(os.getpid()))\ntime.sleep(300)\n')
            executable.chmod(0o700)
            output = root/'motion.mp4'
            output.write_bytes(b'previous')
            with patch.object(m, 'ENCODE_TIMEOUT', .6):
                task = asyncio.create_task(m.render_motion(DEMO,output,ffmpeg_path=executable,ffprobe_path=FFPROBE))
                for _ in range(100):
                    if pidfile.exists():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(pidfile.exists())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pidfile.read_text()),0)
                self.assertEqual(output.read_bytes(),b'previous')
                self.assertFalse(list(root.glob('.motion-*')))

    async def test_parallel_render_is_rejected_without_extra_worker_or_output(self):
        m = renderer()
        self.assertTrue(hasattr(m, '_RENDER_SLOT'), 'Global renderer capacity bound missing')
        import sys
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            root = Path(temp)
            pidfile = root/'busy.pid'
            executable = root/'blocked-encoder'
            executable.write_text('#!'+sys.executable+'\nimport os,time\nopen('+repr(str(pidfile))+',"w").write(str(os.getpid()))\ntime.sleep(300)\n')
            executable.chmod(0o700)
            task = asyncio.create_task(m.render_motion(DEMO,root/'first.mp4',ffmpeg_path=executable,ffprobe_path=FFPROBE))
            try:
                for _ in range(100):
                    if pidfile.exists():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(pidfile.exists())
                with self.assertRaisesRegex(RuntimeError,'busy'):
                    await m.render_motion(DEMO,root/'second.mp4',ffmpeg_path=executable,ffprobe_path=FFPROBE)
                self.assertFalse((root/'second.mp4').exists())
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(list(root.glob('.motion-*')))

    async def test_three_and_five_scene_bounds_render_readable_maximum_strings(self):
        m = renderer()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            for count,expected in ((3,21),(5,30)):
                scene = {'title':'Ж'*60,'text':'Ш'*180,'icon':'process','steps':['Ж'*32,'Ш'*32]}
                board = {'scenes':[copy.deepcopy(scene) for _ in range(count)]}
                for i,entry in enumerate(board['scenes']):
                    entry['icon'] = ['network','diagram','code','science','health'][i]
                    entry['steps'] = ['Ж'*32] * (2 + i%3)
                meta = await m.render_motion(board,Path(temp)/f'bound-{count}.mp4',ffmpeg_path=FFMPEG,ffprobe_path=FFPROBE)
                self.assertEqual(meta['duration'],expected)
                self.assertEqual(meta['frames'],expected*24)
            for icon in m.ICONS:
                board = copy.deepcopy(DEMO)
                board['scenes'][0]['icon'] = icon
                assets = m._assets(board['scenes'][0],0,4)
                self.assertEqual(m._frame(board['scenes'][0],assets,4,6).size,(720,1280))

    async def test_invalid_probe_does_not_replace_existing_artifact(self):
        m = renderer()
        import sys
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            root = Path(temp)
            output = root/'motion.mp4'
            output.write_bytes(b'previous-good-artifact')
            probe = root/'invalid-probe'
            probe.write_text('#!'+sys.executable+'\nprint("private-bad-probe-response")\n')
            probe.chmod(0o700)
            with self.assertRaisesRegex(RuntimeError,'invalid response') as caught:
                await m.render_motion(DEMO,output,ffmpeg_path=FFMPEG,ffprobe_path=probe)
            self.assertNotIn('private-bad',str(caught.exception))
            self.assertEqual(output.read_bytes(),b'previous-good-artifact')
            self.assertFalse(list(root.glob('.motion-*')))

    async def test_real_mp4_has_contract_metadata_and_continuous_diagram_motion(self):
        m = renderer()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            output = Path(temp)/'workspace-7'/'item-8'/'motion.mp4'
            meta = await m.render_motion(DEMO, output, ffmpeg_path=FFMPEG, ffprobe_path=FFPROBE)
            self.assertEqual((meta['width'],meta['height'],meta['fps']), (720,1280,24))
            self.assertEqual(meta['codec'], 'h264')
            self.assertEqual(meta['pixel_format'], 'yuv420p')
            self.assertEqual(meta['duration'], 24.0)
            self.assertEqual(meta['frames'], 576)
            self.assertEqual(meta['bytes'], output.stat().st_size)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)
            self.assertGreater(meta['bytes'], 20000)
            probe = subprocess.run([FFPROBE,'-v','error','-show_streams','-show_format','-of','json',str(output)], capture_output=True, check=True, timeout=30)
            stream = json.loads(probe.stdout)['streams'][0]
            self.assertEqual(stream['codec_name'], 'h264')
            self.assertEqual(stream['pix_fmt'], 'yuv420p')
            self.assertEqual(stream['r_frame_rate'], '24/1')
            from PIL import Image, ImageChops
            import io
            crops = []
            for t in (4.0,4.5):
                frame = subprocess.run([FFMPEG,'-v','error','-ss',str(t),'-i',str(output),'-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],capture_output=True,check=True,timeout=30)
                crops.append(Image.open(io.BytesIO(frame.stdout)).convert('RGB').crop((48,470,672,1020)))
            diff = ImageChops.difference(*crops)
            self.assertIsNotNone(diff.getbbox(), 'Diagram must move during its hold, not just fade static slides')
            self.assertGreater(sum(diff.convert('L').get_flattened_data()), 10000)
            self.assertFalse(list(output.parent.glob('.motion-*')), 'Private temporary work must be removed')


if __name__ == '__main__':
    unittest.main()
