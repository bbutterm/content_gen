"""Bounded, local CPU animated-infographic renderer for Linux.

No Manim/LaTeX, GPU, network, shell, arbitrary code or remote assets. Production
binary locations are /opt/newsbot/bin/{ffmpeg,ffprobe}; explicit absolute paths
are trusted deployment/test overrides, never model input. Output path is chosen
by backend ownership checks, not storyboard content. Directories must be owned
and private (0700); completed MP4 is 0600 and atomically renamed after ffprobe.
"""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import threading
import time
import unicodedata
import uuid

from PIL import Image, ImageDraw, ImageFont

ICONS = frozenset(('process', 'diagram', 'network', 'code', 'science', 'health', 'idea'))


def _text(value, limit):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ValueError('Invalid motion text length or type')
    if any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value):
        raise ValueError('Control characters are not allowed')
    if re.search(r'\w+://|www\.|(?:javascript|data|mailto):|```', value, re.I):
        raise ValueError('URLs and code blocks are not allowed')
    return value


def validate_storyboard(storyboard):
    """Strict JSON-shaped schema only; preserve literal text and detach input."""
    if type(storyboard) is not dict or set(storyboard) != {'scenes'}:
        raise ValueError('Expected storyboard with scenes only')
    scenes = storyboard['scenes']
    if type(scenes) is not list or not 3 <= len(scenes) <= 5:
        raise ValueError('Expected 3 to 5 scenes')
    result = []
    for scene in scenes:
        if type(scene) is not dict or set(scene) != {'title', 'text', 'icon', 'steps'}:
            raise ValueError('Invalid motion scene fields')
        if type(scene['icon']) is not str or scene['icon'] not in ICONS:
            raise ValueError('Unsupported motion icon')
        steps = scene['steps']
        if type(steps) is not list or not 2 <= len(steps) <= 4:
            raise ValueError('Expected 2 to 4 steps')
        result.append({'title': _text(scene['title'], 60), 'text': _text(scene['text'], 180),
                       'icon': scene['icon'], 'steps': [_text(s, 32) for s in steps]})
    return {'scenes': result}


WIDTH, HEIGHT, FPS = 720, 1280, 24
FONT = '/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf'
BG = (10, 17, 31)
FG = (238, 245, 255)
MUTED = (146, 165, 188)
ACCENTS = ((68, 214, 230), (255, 197, 93), (113, 225, 169), (180, 151, 249))


def _wrap(text, font, width):
    # Character wrap also bounds unbroken strings; nothing is silently truncated.
    lines, current = [], ''
    for word in text.split(' '):
        trial = (current + ' ' + word) if current else word
        if font.getlength(trial) <= width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = ''
        for char in word:
            if current and font.getlength(current + char) > width:
                lines.append(current)
                current = ''
            current += char
    if current:
        lines.append(current)
    return lines


def _label(draw, text, box, size, minimum=22, color=FG, centered=False):
    x, y, w, h = box
    for s in range(size, minimum - 1, -1):
        font = ImageFont.truetype(FONT, s)
        lines = _wrap(text, font, w)
        line_h = s + 8
        if len(lines) * line_h <= h:
            break
    else:
        raise ValueError('Motion text cannot fit readable layout')
    for i, line in enumerate(lines):
        px = x + (w - font.getlength(line))/2 if centered else x
        py = y + (h - len(lines)*line_h)/2 + i*line_h if centered else y + i*line_h
        draw.text((px, py), line, font=font, fill=color)


def _icon(draw, kind, color):
    x, y = 610, 90
    if kind == 'code':
        draw.line([(x-10,y-20),(x-28,y),(x-10,y+20)], fill=color, width=4)
        draw.line([(x+10,y-20),(x+28,y),(x+10,y+20)], fill=color, width=4)
    elif kind == 'health':
        draw.line((x-25,y,x+25,y),fill=color,width=9)
        draw.line((x,y-25,x,y+25),fill=color,width=9)
    elif kind == 'science':
        draw.ellipse((x-31,y-14,x+31,y+14),outline=color,width=3)
        draw.ellipse((x-14,y-31,x+14,y+31),outline=color,width=3)
        draw.ellipse((x-4,y-4,x+4,y+4),fill=color)
    elif kind == 'idea':
        draw.ellipse((x-21,y-28,x+21,y+14),outline=color,width=3)
        draw.line((x-12,y+24,x+12,y+24),fill=color,width=4)
        draw.line((x-10,y+15,x+10,y+15),fill=color,width=3)
    else:
        pts = [(x-26,y-17),(x+26,y-17),(x,y+25)]
        draw.line([pts[0],pts[1],pts[2],pts[0]],fill=color,width=2)
        for px,py in pts:
            draw.ellipse((px-7,py-7,px+7,py+7),fill=color)


def _assets(scene, index, count):
    color = ACCENTS[index % len(ACCENTS)]
    base = Image.new('RGB', (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(base)
    for x in range(48, 700, 52):
        for y in range(470, 1000, 52):
            d.ellipse((x,y,x+2,y+2),fill=(24,37,54))
    _label(d, f'{index+1:02d} / {count:02d}   •   СХЕМА', (48,75,500,36), 22, color=color)
    _icon(d, scene['icon'], color)
    _label(d, scene['title'], (48,150,624,130), 42, minimum=28)
    _label(d, scene['text'], (48,294,624,155), 28)
    d.line((48,1040,672,1040),fill=(37,52,73),width=2)
    _label(d, 'ФАКТ → СВЯЗЬ → РЕЗУЛЬТАТ', (48,1155,624,36), 22, color=MUTED)
    n = len(scene['steps'])
    if scene['icon'] == 'network' and n == 4:
        positions = [(360,526),(360,704),(195,924),(525,924)]
        edges = [(0,1),(1,2),(1,3)]
        w,h = 260,112
    elif scene['icon'] == 'code':
        positions = [(120 + i*480/(n-1),740) for i in range(n)]
        edges = [(i,i+1) for i in range(n-1)]
        w,h = min(180, 440/(n-1)),160
    elif scene['icon'] == 'diagram':
        positions = [(215 if i % 2 == 0 else 505, 520+i*400/(n-1)) for i in range(n)]
        edges = [(i,i+1) for i in range(n-1)]
        w,h = 270,104
    else:
        positions = [(360, 520+i*400/(n-1)) for i in range(n)]
        edges = [(i,i+1) for i in range(n-1)]
        w,h = 480,98
    cards = []
    for i, label in enumerate(scene['steps']):
        card = Image.new('RGBA',(int(w),int(h)),(0,0,0,0))
        cd = ImageDraw.Draw(card)
        cd.rounded_rectangle((0,0,w-1,h-1),radius=18,fill=(21,35,53),outline=color,width=2)
        _label(cd, label, (12,10,w-24,h-20), 28, centered=True)
        cards.append(card)
    lines = []
    for a,b in edges:
        p,q = positions[a],positions[b]
        dx,dy = q[0]-p[0],q[1]-p[1]
        ratio = min((w/2+8)/abs(dx) if dx else math.inf, (h/2+8)/abs(dy) if dy else math.inf)
        lines.append(((p[0]+dx*ratio,p[1]+dy*ratio),(q[0]-dx*ratio,q[1]-dy*ratio),b))
    return base, cards, positions, lines, color


def _ease(value):
    v = min(1.0,max(0.0,value))
    return v*v*(3-2*v)


def _frame(scene, assets, local_time, scene_seconds):
    base, cards, positions, lines, color = assets
    image = base.copy()
    d = ImageDraw.Draw(image)
    for p,q,b in lines:
        progress = _ease((local_time-(0.85+b*0.62))/0.65)
        tip = (p[0]+(q[0]-p[0])*progress,p[1]+(q[1]-p[1])*progress)
        if progress:
            d.line((p,tip), fill=color, width=3)
            angle = math.atan2(q[1]-p[1],q[0]-p[0])
            d.polygon([tip,(tip[0]-12*math.cos(angle-.45),tip[1]-12*math.sin(angle-.45)),
                       (tip[0]-12*math.cos(angle+.45),tip[1]-12*math.sin(angle+.45))],fill=color)
        if progress == 1:
            travel = ((local_time-(1.5+b*0.62))/1.65) % 1
            x,y = p[0]+(q[0]-p[0])*travel,p[1]+(q[1]-p[1])*travel
            d.ellipse((x-11,y-11,x+11,y+11),fill=(35,56,73))
            d.ellipse((x-6,y-6,x+6,y+6),fill=FG)
    for i,(card,(x,y)) in enumerate(zip(cards,positions)):
        progress = _ease((local_time-(.45+i*.62))/.65)
        if progress:
            visible = card.copy()
            visible.putalpha(card.getchannel('A').point(lambda a: int(a*progress)))
            image.paste(visible,(int(x-card.width/2),int(y-card.height/2+18*(1-progress))),visible)
    active = min(len(cards)-1,max(0,int((local_time-.5)/1.3)))
    _label(ImageDraw.Draw(image), scene['steps'][active], (48,1070,624,72),28,centered=True,color=color)
    opacity = min(_ease(local_time/.3),_ease((scene_seconds-local_time)/.3))
    if opacity < 1:
        image = Image.blend(Image.new('RGB',(WIDTH,HEIGHT),BG), image, opacity)
    return image


def _executable(explicit, name):
    path = Path(explicit) if explicit is not None else Path('/opt/newsbot/bin')/name
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError('Motion executable unavailable')
    return str(path)


ENCODE_TIMEOUT = 120.0
PROBE_TIMEOUT = 15.0
MAX_PROBE_BYTES = 65536
MAX_VIDEO_BYTES = 32 * 1024 * 1024
_RENDER_SLOT = threading.BoundedSemaphore(1)


def _stop_process(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def _encode(command, candidate_fd, scenes, assets, seconds, stop_event):
    deadline = time.monotonic() + ENCODE_TIMEOUT
    process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,pass_fds=(candidate_fd,),
                               start_new_session=True,bufsize=0)
    pipe_fd = process.stdin.fileno()
    os.set_blocking(pipe_fd,False)
    try:
        for scene,prepared in zip(scenes,assets):
            for frame in range(seconds*FPS):
                data = memoryview(_frame(scene,prepared,frame/FPS,seconds).tobytes())
                while data:
                    if stop_event.is_set():
                        raise RuntimeError('Motion render cancelled')
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError('Motion encoder timeout')
                    if process.poll() is not None:
                        raise RuntimeError('Motion encoder failed')
                    if not select.select([], [pipe_fd], [], min(.1,remaining))[1]:
                        continue
                    try:
                        written = os.write(pipe_fd,data)
                    except BlockingIOError:
                        continue
                    data = data[written:]
        process.stdin.close()
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Motion encoder timeout')
        while process.poll() is None:
            if stop_event.is_set():
                raise RuntimeError('Motion render cancelled')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Motion encoder timeout')
            try:
                process.wait(timeout=min(.1, remaining))
            except subprocess.TimeoutExpired:
                pass
        if process.returncode != 0:
            raise RuntimeError('Motion encoder failed')
    except BrokenPipeError:
        raise RuntimeError('Motion encoder failed') from None
    finally:
        _stop_process(process)
        process.stdin.close()


def _check_target(parent_fd, name):
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError('Output must be an owned regular file, never a symlink')


def _open_output_parent(output_path):
    """Return an anchored directory FD; never resolve or follow symlinks."""
    raw = os.fspath(output_path)
    if not isinstance(raw, str) or not raw or '\x00' in raw:
        raise ValueError('Invalid motion output path')
    output = Path(raw)
    if '..' in output.parts:
        raise ValueError('Motion output traversal rejected')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.mp4', output.name):
        raise ValueError('Expected a local MP4 filename')
    output = output.absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in output.parent.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('Motion output directory must be private and owned')
        _check_target(fd, output.name)
        return fd, output.name
    except Exception as exc:
        os.close(fd)
        if isinstance(exc, ValueError):
            raise
        raise ValueError('Unsafe motion output directory') from None


def _probe(executable, candidate_fd, stop_event):
    process = subprocess.Popen([executable,'-v','error','-show_streams','-show_format','-of','json',
                                f'/proc/self/fd/{candidate_fd}'], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                               pass_fds=(candidate_fd,),start_new_session=True,bufsize=0)
    data = bytearray()
    deadline = time.monotonic()+PROBE_TIMEOUT
    fd = process.stdout.fileno()
    os.set_blocking(fd,False)
    try:
        while True:
            if stop_event.is_set():
                raise RuntimeError('Motion render cancelled')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Motion probe timeout')
            if not select.select([fd],[],[],min(.1,remaining))[0]:
                continue
            try:
                chunk = os.read(fd,8192)
            except BlockingIOError:
                continue
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_PROBE_BYTES:
                raise RuntimeError('Motion probe output limit')
        while process.poll() is None:
            if stop_event.is_set():
                raise RuntimeError('Motion render cancelled')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Motion probe timeout')
            try:
                process.wait(timeout=min(.1,remaining))
            except subprocess.TimeoutExpired:
                pass
        if process.returncode != 0:
            raise RuntimeError('Motion probe failed')
        try:
            return json.loads(data)
        except (ValueError,UnicodeError):
            raise RuntimeError('Motion probe invalid response') from None
    finally:
        _stop_process(process)
        process.stdout.close()


def _validate_probe(probe, expected_duration, size):
    try:
        if not 1000 <= size <= MAX_VIDEO_BYTES or len(probe['streams']) != 1:
            raise ValueError()
        stream = probe['streams'][0]
        duration = float(probe['format']['duration'])
        stream_duration = float(stream['duration'])
        if not math.isfinite(duration) or not math.isfinite(stream_duration):
            raise ValueError()
        if (stream['codec_type'],stream['codec_name'],stream['pix_fmt'],stream['width'],stream['height'],
                stream['r_frame_rate'],stream['avg_frame_rate']) != ('video','h264','yuv420p',720,1280,'24/1','24/1'):
            raise ValueError()
        frames = int(stream['nb_frames'])
        if frames != expected_duration*FPS or abs(duration-expected_duration) > 1/FPS or abs(stream_duration-duration) > 1/FPS:
            raise ValueError()
        if 'mp4' not in probe['format']['format_name'].split(','):
            raise ValueError()
        return {'duration':duration,'width':WIDTH,'height':HEIGHT,'fps':FPS,'bytes':size,
                'codec':'h264','pixel_format':'yuv420p','frames':frames,'renderer':'pillow-ffmpeg'}
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,OverflowError):
        raise RuntimeError('Motion artifact validation failed') from None


def _render_sync(storyboard, output_path, ffmpeg, ffprobe, stop_event):
    parent_fd, output_name = _open_output_parent(output_path)
    work_name = '.motion-' + uuid.uuid4().hex
    work_fd = candidate_fd = None
    try:
        scenes = storyboard['scenes']
        seconds = 7 if len(scenes) == 3 else 6
        assets = [_assets(scene,i,len(scenes)) for i,scene in enumerate(scenes)]
        os.mkdir(work_name, mode=0o700, dir_fd=parent_fd)
        work_fd = os.open(work_name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent_fd)
        candidate_fd = os.open('render.mp4',os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=work_fd)
        candidate = f'/proc/self/fd/{candidate_fd}'
        command = [ffmpeg,'-v','error','-nostdin','-y','-f','rawvideo','-pixel_format','rgb24',
                   '-video_size','720x1280','-framerate','24','-i','pipe:0','-an','-c:v','libx264',
                   '-threads','2','-preset','veryfast','-crf','21','-pix_fmt','yuv420p',
                   '-movflags','+faststart','-map_metadata','-1','-fs',str(MAX_VIDEO_BYTES),'-f','mp4',candidate]
        _encode(command, candidate_fd, scenes, assets, seconds, stop_event)
        probe = _probe(ffprobe,candidate_fd,stop_event)
        size = os.fstat(candidate_fd).st_size
        meta = _validate_probe(probe,seconds*len(scenes),size)
        os.lseek(candidate_fd,0,os.SEEK_SET)
        with os.fdopen(os.dup(candidate_fd),'rb') as video:
            digest = hashlib.file_digest(video, 'sha256').hexdigest()
        os.fsync(candidate_fd)
        if stop_event.is_set():
            raise RuntimeError('Motion render cancelled')
        _check_target(parent_fd,output_name)
        os.rename('render.mp4',output_name,src_dir_fd=work_fd,dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
        meta.update(sha256=digest,scene_count=len(scenes))
        return meta
    finally:
        if candidate_fd is not None:
            os.close(candidate_fd)
        if work_fd is not None:
            try:
                os.unlink('render.mp4',dir_fd=work_fd)
            except FileNotFoundError:
                pass
            os.close(work_fd)
            os.rmdir(work_name,dir_fd=parent_fd)
        os.close(parent_fd)


async def render_motion(storyboard, output_path, *, ffmpeg_path=None, ffprobe_path=None, theme='tech'):
    """Asynchronously render {'scenes': [3..5 strict scene dictionaries]}.

    Each scene has title (1..60 chars), text (1..180 chars), icon (ICONS enum),
    steps (2..4 labels of 1..32 chars); no unknown fields, URL/code blocks or
    control characters. Bare scene lists and narration are not accepted.
    Durations: 3 scenes=21s, 4=24s, 5=30s; always 720x1280/24fps/H264/yuv420p.
    Output is backend-chosen str/Path; final directory must be private/owned.
    Metadata: duration, width, height, fps, bytes, codec, pixel_format, frames,
    sha256, scene_count, renderer. No filesystem paths appear in metadata.
    ValueError: invalid schema/theme/output scope. RuntimeError: unavailable
    binaries, busy slot, timeout, encoding/probe/validation failure. At most
    one worker per process; caller should serialize jobs rather than poll busy.
    Cancellation waits for the worker/process cleanup before propagating.
    Linux /proc/self/fd and O_NOFOLLOW/dir_fd support are required.
    """
    if theme != 'tech':
        raise ValueError('Unsupported motion theme')
    clean = validate_storyboard(storyboard)
    ffmpeg,ffprobe = _executable(ffmpeg_path,'ffmpeg'),_executable(ffprobe_path,'ffprobe')
    if not _RENDER_SLOT.acquire(blocking=False):
        raise RuntimeError('Motion renderer busy')
    try:
        stop_event = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(_render_sync, clean, output_path,ffmpeg,ffprobe,stop_event))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            stop_event.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    stop_event.set()
                except Exception:
                    break
            if worker.done() and not worker.cancelled():
                worker.exception()
            raise
    finally:
        _RENDER_SLOT.release()
