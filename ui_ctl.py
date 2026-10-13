"""ADB UI 操控。坐标为当前方向的整屏像素；选择器动作重新识别，歧义时拒绝注入。"""
import math
import os
import re
import shlex
import struct
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

# ============ 配置区 ============
SERIAL = "your-device-serial"  # 改成 adb devices 列出的序列号
ADB = "adb"
SU_INPUT = True                # 经 su -c input 注入；部分 ROM 用户态 shell 无 INJECT_EVENTS 权限
DUMP_PATH = "/sdcard/_ui_dump.xml"  # 每次加随机后缀，避免读取旧文件
DUMP_TIMEOUT = 12
ACTION_WAIT = 0.6
TEXT_CHAR_DELAY = 0.05       # MIUI 输入法会吞字或乱序，默认逐字注入
MIN_VISIBLE_SIZE = 8        # WebView 会把屏幕外节点压成 2 至 3 像素细条
MIN_DRAG_MS = 1200          # Android 10 的 draganddrop 约 0.75 秒后才开始移动
OUTPUT_DIR = Path(__file__).resolve().parent / "out" / "ui"
# ================================


class AdbError(RuntimeError):
    pass


class UiDumpError(RuntimeError):
    pass


class AmbiguousElementError(LookupError):
    pass


def _run(args, timeout=30, binary=False):
    if timeout <= 0:
        raise TimeoutError("ADB 调用已超时")
    try:
        result = subprocess.run([ADB, "-s", SERIAL, *map(str, args)],
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError("ADB 调用超时；输入动作不得自动重试") from None
    except OSError as exc:
        raise AdbError(f"无法运行 ADB: {exc}") from exc
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.returncode:
        raise AdbError(f"ADB 返回 {result.returncode}: {stderr.strip()[:500]}")
    if binary:
        return result.stdout
    output = result.stdout.decode("utf-8", errors="replace")
    if re.search(r"SecurityException|Permission denied|^Error:|^error:",
                 output + "\n" + stderr, re.MULTILINE):
        raise AdbError(f"设备命令失败: {(output + stderr).strip()[:500]}")
    return output


def _shell(*args, timeout=30):
    return _run(["shell", shlex.join(map(str, args))], timeout=timeout)


def _input(*args):
    command = shlex.join(["input", *map(str, args)])
    if SU_INPUT:
        command = shlex.join(["su", "-c", command])
    return _run(["shell", command])


def _remaining(deadline, cap=30):
    value = min(cap, deadline - time.monotonic())
    if value <= 0:
        raise TimeoutError("已到达等待期限")
    return value


def _pause(seconds):
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("等待时间必须为有限非负数")
    time.sleep(seconds)


def _save(path, data):
    path = Path(path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return str(path)


def _parse_bounds(value):
    match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", value or "")
    return tuple(map(int, match.groups())) if match else None


def _intersect(a, b):
    if not a or not b:
        return None
    rect = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return rect if rect[0] < rect[2] and rect[1] < rect[3] else None


def _parse_ui(data):
    root = ET.fromstring(data)
    if root.tag != "hierarchy":
        raise ValueError("根节点不是 hierarchy")
    elements = []

    def visit(node, parent=None, clip=None):
        a = node.attrib
        bounds = _parse_bounds(a.get("bounds"))
        visible = _intersect(bounds, clip) if clip else bounds
        if not visible or visible[0] >= visible[2] or visible[1] >= visible[3]:
            visible = None
        if parent is not None and not elements[parent]["visible"]:
            visible = None
        if a.get("visible-to-user") == "false":
            visible = None
        index = len(elements)
        elements.append(dict(
            text=a.get("text", ""), rid=a.get("resource-id", ""),
            cls=a.get("class", ""), desc=a.get("content-desc", ""), pkg=a.get("package", ""),
            bounds=bounds, visible_bounds=visible, visible=bool(visible),
            center=((visible[0]+visible[2])//2, (visible[1]+visible[3])//2) if visible else None,
            parent=parent, node_index=index,
            **{key: a.get(key) == "true" for key in
               ("clickable", "enabled", "scrollable", "long-clickable", "checkable", "checked", "selected", "focused")}
        ))
        for child in node.findall("node"):
            visit(child, index, visible)

    for node in root.findall("node"):
        visit(node)
    return elements


def dump_ui(save_raw=None, retries=3, timeout=None):
    """取新元素树；timeout 为所有重试的总预算，包含 dump 与 pull。"""
    if retries < 1:
        raise ValueError("retries 必须 >= 1")
    deadline = time.monotonic() + (DUMP_TIMEOUT if timeout is None else timeout)
    last_error = None
    for attempt in range(retries):
        remote = DUMP_PATH.removesuffix(".xml") + "_" + uuid.uuid4().hex + ".xml"
        try:
            _shell("uiautomator", "dump", remote, timeout=_remaining(deadline))
            with tempfile.TemporaryDirectory(prefix="adb_ui_") as directory:
                local = Path(directory) / "ui.xml"
                _run(["pull", remote, str(local)], timeout=_remaining(deadline))
                data = local.read_bytes()
            elements = _parse_ui(data)
            if not elements:
                raise ValueError("元素树为空，当前界面可能不提供可访问节点")
            if save_raw:
                _save(save_raw, data)
            return elements
        except (AdbError, OSError, ET.ParseError, ValueError) as exc:
            if isinstance(exc, TimeoutError):
                raise
            last_error = exc
        finally:
            # 超时后不额外等待清理，随机路径不会被下次调用误用。
            if deadline - time.monotonic() > 0.15:
                try:
                    _shell("rm", "-f", remote, timeout=_remaining(deadline, 1))
                except (AdbError, TimeoutError):
                    pass
        if attempt + 1 < retries:
            _pause(min(0.15, _remaining(deadline)))
    raise UiDumpError(f"元素树获取失败: {last_error}") from last_error


def _match(el, text=None, rid=None, cls=None, desc=None, exact=False,
           clickable_only=False, pkg=None, enabled_only=False, visible_only=True, within=None):
    if clickable_only and not el.get("clickable"):
        return False
    if enabled_only and not el.get("enabled"):
        return False
    bounds = el.get("visible_bounds", el.get("bounds"))
    if visible_only and (el.get("visible") is False or not bounds or
                         bounds[2]-bounds[0] < MIN_VISIBLE_SIZE or bounds[3]-bounds[1] < MIN_VISIBLE_SIZE):
        return False
    if within and (not bounds or _intersect(bounds, within) != bounds):
        return False
    for key, value in (("text", text), ("rid", rid), ("cls", cls), ("desc", desc), ("pkg", pkg)):
        if value is not None:
            field = el.get(key, "")
            if (exact and field != value) or (not exact and value not in field):
                return False
    return True


def find_all(ui=None, text=None, rid=None, cls=None, desc=None, exact=False,
             clickable_only=False, pkg=None, enabled_only=False, visible_only=True, within=None):
    """默认仅匹配可见节点；未知参数直接报错。"""
    if ui is None:
        ui = dump_ui()
    return [el for el in ui if _match(el, text, rid, cls, desc, exact, clickable_only,
                                     pkg, enabled_only, visible_only, within)]


def find(ui=None, text=None, rid=None, cls=None, desc=None, exact=False,
         clickable_only=False, index=0, **filters):
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("index 必须为非负整数")
    hits = find_all(ui, text, rid, cls, desc, exact, clickable_only, **filters)
    return hits[index] if index < len(hits) else None


def display_size():
    """当前方向的像素宽高，包含状态栏和导航栏。"""
    info = _shell("dumpsys", "display")
    match = re.search(r"mOverrideDisplayInfo=DisplayInfo\{[^\n]*?real (\d+) x (\d+)", info)
    if not match:
        match = re.search(r"mBaseDisplayInfo=DisplayInfo\{[^\n]*?real (\d+) x (\d+)", info)
    if not match:
        raise RuntimeError("无法确定当前屏幕尺寸，拒绝猜测坐标")
    return tuple(map(int, match.groups()))


def map_point(x, y, image_size, viewport=None):
    """把缩放后的整屏截图坐标换算为设备坐标；不适用于裁剪图。"""
    width, height = viewport or display_size()
    iw, ih = image_size
    if iw <= 0 or ih <= 0 or not (0 <= x < iw and 0 <= y < ih):
        raise ValueError("截图尺寸或坐标无效")
    return min(width-1, int(x*width/iw)), min(height-1, int(y*height/ih))


def _point(x, y, size):
    if x is None or y is None or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in (x, y)):
        raise ValueError("必须提供两个有限坐标")
    if not (0 <= x < size[0] and 0 <= y < size[1]):
        raise ValueError(f"坐标 {(x, y)} 超出屏幕 {size}")
    return int(x), int(y)


def _selector_required(selector):
    if not any(selector.get(key) for key in ("text", "rid", "cls", "desc", "pkg")):
        raise ValueError("必须指定非空选择器或完整坐标")


def _resolve(x, y, ui, index, retries, selector):
    if x is not None or y is not None:
        if any(selector.get(k) is not None for k in ("text", "rid", "cls", "desc", "pkg")):
            raise ValueError("坐标和元素选择器不可混用")
        return _point(x, y, display_size())
    _selector_required(selector)
    if index is not None and (not isinstance(index, int) or isinstance(index, bool) or index < 0):
        raise ValueError("index 必须为非负整数")
    if retries < 0:
        raise ValueError("retries 必须 >= 0")
    # ui 仅为兼容旧调用保留。动作始终重取，避免旧树坐标误点。
    for attempt in range(retries + 1):
        hits = find_all(dump_ui(), enabled_only=True, **selector)
        if index is None and len(hits) > 1:
            raise AmbiguousElementError(f"匹配到 {len(hits)} 个元素，请收紧条件或显式指定 index")
        selected = 0 if index is None else index
        if selected < len(hits):
            return _point(*hits[selected]["center"], display_size())
        if attempt < retries:
            _pause(0.2)
    raise LookupError(f"未找到可见且启用的元素: {selector}")


def tap(x=None, y=None, ui=None, text=None, rid=None, cls=None, desc=None,
        exact=False, index=None, wait=ACTION_WAIT, retries=2, pkg=None,
        within=None, clickable_only=False):
    """返回注入坐标，不代表业务完成；应 wait_for 验证后置状态。"""
    if not math.isfinite(wait) or wait < 0:
        raise ValueError("wait 必须非负")
    selector = dict(text=text, rid=rid, cls=cls, desc=desc, exact=exact,
                    pkg=pkg, within=within, clickable_only=clickable_only)
    point = _resolve(x, y, ui, index, retries, selector)
    _input("tap", *point)
    _pause(wait)
    return point


def _gesture(command, x1, y1, x2, y2, ms, wait):
    if not isinstance(ms, int) or ms <= 0 or not math.isfinite(wait) or wait < 0:
        raise ValueError("ms 必须为正整数，wait 必须非负")
    size = display_size()
    start, end = _point(x1, y1, size), _point(x2, y2, size)
    _input(command, *start, *end, ms)
    _pause(wait)


def swipe(x1, y1, x2, y2, ms=300, wait=ACTION_WAIT):
    _gesture("swipe", x1, y1, x2, y2, ms, wait)


def drag(x1, y1, x2, y2, ms=1600, wait=ACTION_WAIT):
    """系统长按拖拽；ms 是含长按的总时长。首个 MOVE 有跳跃，不保证匀速轨迹。"""
    if not isinstance(ms, int) or ms < MIN_DRAG_MS:
        raise ValueError(f'系统拖拽总时长至少 {MIN_DRAG_MS}ms；短手势请用 swipe')
    _gesture("draganddrop", x1, y1, x2, y2, ms, wait)


def keyevent(code, wait=ACTION_WAIT):
    value = str(code).upper()
    if not re.fullmatch(r"[A-Z0-9_]+", value) or wait < 0 or not math.isfinite(wait):
        raise ValueError("无效按键或等待时间")
    _input("keyevent", value)
    _pause(wait)


def long_press(x=None, y=None, text=None, ms=1200, wait=ACTION_WAIT, **selector):
    index = selector.pop("index", None)
    retries = selector.pop("retries", 0)
    point = _resolve(x, y, None, index, retries, dict(text=text, **selector))
    _gesture("swipe", *point, *point, ms, wait)
    return point


def _hide_keyboard():
    state = _shell('dumpsys', 'input_method')
    match = re.search(r'\bmInputShown=(true|false)', state)
    if not match:
        raise RuntimeError('无法判断软键盘是否展开，未注入文本')
    if match.group(1) == 'true':
        keyevent('BACK', wait=0.2)
        state = _shell('dumpsys', 'input_method')
        if not re.search(r'\bmInputShown=false', state):
            raise RuntimeError('软键盘未收起，未注入文本')


def input_text(s, wait=ACTION_WAIT, verify=None, char_delay=TEXT_CHAR_DELAY, hide_keyboard=True):
    """收起软键盘后逐字输入可打印 ASCII；verify 指定输入框选择器并核对全文。

    不自动提交或切换输入法。char_delay=None 为批量模式；hide_keyboard=False 保留键盘。
    """
    if not isinstance(s, str) or any(not 32 <= ord(c) < 127 for c in s):
        raise ValueError("仅支持可打印 ASCII；不支持中文及控制字符")
    if wait < 0 or not math.isfinite(wait):
        raise ValueError("wait 必须非负")
    if char_delay is not None and (not math.isfinite(char_delay) or char_delay < 0):
        raise ValueError('char_delay 必须为非负数或 None')
    if verify is not None:
        _selector_required(verify)
        fields = find_all(**verify)
        if len(fields) != 1 or not fields[0]['enabled'] or not fields[0].get('focused'):
            raise RuntimeError('校验目标不是唯一、启用且已聚焦的输入框，未注入文本')
    if hide_keyboard and s:
        _hide_keyboard()
    # input text 会把 %s 换成空格，批量模式也要拆开字面量的 % 与 s。
    parts = re.split(r"(?<=%)(?=s)", s) if char_delay is None else list(s)
    for part in parts:
        if part:
            _input("text", part.replace(" ", "%s"))
            if char_delay is not None:
                _pause(char_delay)
    _pause(wait)
    if verify is not None:
        fields = find_all(**verify)
        if len(fields) != 1 or fields[0]['text'] != s:
            raise RuntimeError('输入框内容与预期不符，未自动提交；请检查输入法模式及原有内容')


def _png_size(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG 文件头无效")
    offset, size, compressed = 8, None, bytearray()
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset+4])[0]
        end = offset + 12 + length
        if end > len(data):
            break
        kind, payload = data[offset+4:offset+8], data[offset+8:end-4]
        crc = struct.unpack(">I", data[end-4:end])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise ValueError("PNG 校验和不匹配")
        if kind == b"IHDR":
            width, height, depth, color, comp, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if width <= 0 or height <= 0 or depth != 8 or color not in (2, 6) or comp or filtering or interlace:
                raise ValueError("不支持的 screencap PNG 格式")
            size = (width, height)
            row_size = width * (3 if color == 2 else 4) + 1
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            if not size or length or end != len(data):
                raise ValueError("PNG 结构无效")
            decoder = zlib.decompressobj()
            raw = decoder.decompress(compressed, row_size * size[1] + 1)
            if not decoder.eof or len(raw) != row_size * size[1]:
                raise ValueError("PNG 像素数据不完整")
            return size
        offset = end
    raise ValueError("PNG 被截断或缺少 IEND")


def screenshot(path=None, retries=3):
    """校验 CRC、IEND、像素流后保存；exec-out 失败时切换设备文件 + pull。"""
    if retries < 1:
        raise ValueError("retries 必须 >= 1")
    path = path or OUTPUT_DIR / "screen.png"
    last_error = None
    for attempt in range(retries):
        remote = "/sdcard/_screen_" + uuid.uuid4().hex + ".png"
        try:
            if attempt == 0:
                data = _run(["exec-out", "screencap", "-p"], binary=True, timeout=12)
            else:
                _shell("screencap", "-p", remote, timeout=12)
                with tempfile.TemporaryDirectory(prefix="adb_screen_") as directory:
                    local = Path(directory) / "screen.png"
                    _run(["pull", remote, str(local)])
                    data = local.read_bytes()
            _png_size(data)
            return _save(path, data)
        except (AdbError, TimeoutError, OSError, ValueError, zlib.error, struct.error) as exc:
            last_error = exc
        finally:
            if attempt:
                try:
                    _shell("rm", "-f", remote, timeout=2)
                except (AdbError, TimeoutError):
                    pass
    raise RuntimeError(f"截图失败: {last_error}") from last_error


def wait_for(text=None, rid=None, desc=None, timeout=10, interval=0.3, **selector):
    """总预算包含 dump；正常缺失返回 None，设备故障抛错。"""
    selector = dict(text=text, rid=rid, desc=desc, **selector)
    _selector_required(selector)
    if timeout < 0 or interval < 0 or not math.isfinite(timeout + interval):
        raise ValueError("无效超时或轮询间隔")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ui = dump_ui(timeout=_remaining(deadline), retries=1)
        except TimeoutError:
            return None
        el = find(ui, **selector)
        if el:
            return el
        remaining = deadline - time.monotonic()
        if remaining > 0:
            _pause(min(interval, remaining))
    return None


def scroll_to(text=None, rid=None, desc=None, max_swipes=8, dy=None,
              direction="up", container=None, **selector):
    """在可见滚动容器内滑动并复查；到底或预算耗尽返回 None。"""
    selector = dict(text=text, rid=rid, desc=desc, **selector)
    _selector_required(selector)
    if direction not in ("up", "down", "left", "right") or max_swipes < 0 or (dy is not None and dy <= 0):
        raise ValueError("滑动方向、次数或距离无效")
    previous = None
    for attempt in range(max_swipes + 1):
        ui = dump_ui()
        candidates = find_all(ui, **container) if container else [
            e for e in ui if e["scrollable"] and e.get("visible", True)]
        region = None
        if container and len(candidates) != 1:
            raise LookupError("滚动容器必须唯一且可见")
        if candidates:
            chosen = max(candidates, key=lambda e: (e["bounds"][2]-e["bounds"][0])*(e["bounds"][3]-e["bounds"][1]))
            region = chosen.get("visible_bounds", chosen["bounds"])
        el = find(ui, within=region, **selector)
        if el:
            return el
        if attempt == max_swipes:
            return None
        if not region:
            raise LookupError("未发现滚动容器，请显式指定 container")
        signature = tuple((e["text"], e["rid"], e["desc"], e["bounds"]) for e in ui
                          if _match(e, within=region))
        if signature == previous:
            return None
        previous = signature
        l, t, r, b = region
        vertical = direction in ("up", "down")
        low, high = (t, b) if vertical else (l, r)
        start = int(low + (high-low)*0.8)
        end = max(int(low+(high-low)*0.2), start-int(dy)) if dy else int(low+(high-low)*0.2)
        if direction in ("down", "right"):
            start, end = end, start
        if vertical:
            swipe((l+r)//2, start, (l+r)//2, end, 400)
        else:
            swipe(start, (t+b)//2, end, (t+b)//2, 400)
    return None


def find_image(template, threshold=0.98, region=None, screenshot_path=None):
    """同尺度模板识别。返回 bounds/center/score；多处达标时报歧义。

    OpenCV 仅在使用此函数时需要。score 是像素相似度，不是成功概率。
    screenshot_path 仅用于离线识别；实时点击请用 tap_image。
    """
    import cv2
    import numpy as np

    if not 0 < threshold <= 1:
        raise ValueError('threshold 必须在 (0, 1]')
    template = Path(template)
    if not template.is_absolute():
        template = Path(__file__).resolve().parent / template
    target = cv2.imdecode(np.frombuffer(template.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if target is None or min(target.shape[:2]) < 8 or target.reshape(-1, 3).std(axis=0).max() < 2:
        raise ValueError('模板无效、过小或缺少视觉特征')
    with tempfile.TemporaryDirectory(prefix='adb_vision_') as directory:
        path = screenshot_path or screenshot(Path(directory) / 'screen.png')
        screen = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if screen is None:
        raise ValueError('截图无法解码')
    height, width = screen.shape[:2]
    rect = (0, 0, width, height) if region is None else tuple(region)
    if len(rect) != 4 or not all(isinstance(v, int) for v in rect) or _intersect(rect, (0, 0, width, height)) != rect:
        raise ValueError('region 超出截图或无效')
    left, top, right, bottom = rect
    th, tw = target.shape[:2]
    if th > bottom-top or tw > right-left:
        return None
    scores = cv2.matchTemplate(screen[top:bottom, left:right], target, cv2.TM_SQDIFF_NORMED)
    value, _, point, _ = cv2.minMaxLoc(scores)
    if 1-value < threshold:
        return None
    x, y = point
    scores[max(0, y-th//2):y+th//2+1, max(0, x-tw//2):x+tw//2+1] = 1
    if 1-float(scores.min()) >= threshold:
        raise AmbiguousElementError('模板在多处匹配，请提供更完整模板或限制 region')
    bounds = (left+x, top+y, left+x+tw, top+y+th)
    return dict(bounds=bounds, center=(left+x+tw//2, top+y+th//2), score=1-value,
                screen_size=(width, height))


def tap_image(template, threshold=0.98, region=None, wait=ACTION_WAIT):
    """重新截图识别后点击；无匹配、歧义或方向变化时不注入。"""
    if wait < 0 or not math.isfinite(wait):
        raise ValueError('wait 必须非负')
    match = find_image(template, threshold=threshold, region=region)
    if match is None:
        raise LookupError('未找到模板')
    size = display_size()
    if size != match['screen_size']:
        raise RuntimeError('截图后屏幕尺寸发生变化，请重新识别')
    point = _point(*match['center'], size)
    _input('tap', *point)
    _pause(wait)
    return point


def screen_state():
    pw, wm = _shell("dumpsys", "power"), _shell("dumpsys", "window")
    wake = re.search(r"mWakefulness=(\w+)", pw)
    focus = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^}]+)\}", wm)
    focus = focus.group(1) if focus else ""
    locks = re.findall(r"(?:mDreamingLockscreen|mShowingLockscreen|isStatusBarKeyguard)=(true|false)", wm)
    locked = any(value == "true" for value in locks) if locks else None
    return dict(wakefulness=wake.group(1) if wake else "unknown", focus=focus,
                app=focus.split("/")[0] if "/" in focus else "", locked=locked)


def wake():
    keyevent(224, wait=0.8)


def unlock(pin=None, wait=1):
    """至多尝试一次 PIN；仅识别到系统密码框后输入，不猜密码、不循环重试。"""
    wake()
    state = screen_state()
    if state["locked"] is False:
        return state
    if state["locked"] is None:
        raise RuntimeError("无法确定锁屏状态，未输入密码")
    width, height = display_size()
    swipe(width//2, int(height*.85), width//2, int(height*.35), 300, wait=wait)
    state = screen_state()
    if state["locked"] is False:
        return state
    fields = [e for e in dump_ui() if e["pkg"] == "com.android.systemui" and e["enabled"] and e["center"]
              and re.search(r"(pinEntry|passwordEntry)$", e["rid"])]
    if len(fields) != 1:
        raise RuntimeError("未识别到唯一系统 PIN 输入框，未输入密码")
    if pin is None or not re.fullmatch(r"[0-9]{4,16}", str(pin)):
        raise ValueError("需要明确提供 4 至 16 位数字 PIN")
    if fields[0]["text"]:
        raise RuntimeError("PIN 输入框已有内容，拒绝追加密码")
    input_text(str(pin), wait=0.2)
    keyevent(66, wait=wait)
    state = screen_state()
    if state["locked"] is not False:
        raise RuntimeError("解锁未成功，已停止；不会自动重试 PIN")
    return state


if __name__ == "__main__":
    print("屏幕状态:", screen_state())
    elements = dump_ui()
    print("元素总数:", len(elements))
    for el in elements:
        if el["center"] and (el["text"] or el["desc"]):
            print(repr(el["text"] or el["desc"]), el["center"], el["rid"])
