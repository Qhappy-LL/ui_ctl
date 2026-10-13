import subprocess
import tempfile
import time
import shlex
import unittest
from pathlib import Path
from unittest.mock import patch

import ui_ctl as u


def element(x=10, y=20, **values):
    el = dict(text='target', rid='r', cls='c', desc='', pkg='test',
              bounds=(x-5, y-5, x+5, y+5), center=(x, y),
              clickable=True, enabled=True, scrollable=False)
    el.update(values)
    return el


class ControlTests(unittest.TestCase):
    def setUp(self):
        keyboard = patch.object(u, '_hide_keyboard')
        keyboard.start()
        self.addCleanup(keyboard.stop)

    def test_index_selects_second_match(self):
        with patch.object(u, 'dump_ui', return_value=[element(), element(30, 40)]), \
                patch.object(u, '_input'), patch.object(u, 'display_size', return_value=(1080, 2248), create=True):
            self.assertEqual(u.tap(text='target', index=1, wait=0), (30, 40))

    def test_ambiguous_action_does_not_inject(self):
        with patch.object(u, 'dump_ui', return_value=[element(), element(30, 40)]), \
                patch.object(u, '_input') as inject:
            with self.assertRaises(LookupError):
                u.tap(text='target', wait=0)
            inject.assert_not_called()

    def test_invisible_node_is_not_found(self):
        self.assertIsNone(u.find([element(bounds=(0, 0, 0, 0), center=(0, 0))], text='target'))

    def test_disabled_target_is_not_clicked(self):
        with patch.object(u, 'dump_ui', return_value=[element(enabled=False)]), \
                patch.object(u, '_input') as inject:
            with self.assertRaises(LookupError):
                u.tap(text='target', wait=0, retries=0)
            inject.assert_not_called()

    def test_empty_selector_does_not_click_root(self):
        with patch.object(u, 'dump_ui', return_value=[element()]), patch.object(u, '_input') as inject:
            with self.assertRaises(ValueError):
                u.tap(wait=0)
            inject.assert_not_called()

    def test_adb_error_is_not_success(self):
        result = subprocess.CompletedProcess([], 1, b'', b'device offline')
        with patch.object(u.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError):
                u._run(['shell', 'true'])

    def test_adb_zero_exit_security_error_is_not_success(self):
        result = subprocess.CompletedProcess([], 0, b'', b'java.lang.SecurityException: INJECT_EVENTS')
        with patch.object(u.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError):
                u._input('tap', 1, 2)

    def test_drag_has_hold_before_motion(self):
        with patch.object(u, '_input') as inject, \
                patch.object(u, 'display_size', return_value=(1080, 2248), create=True):
            u.drag(10, 20, 30, 40, wait=0)
            self.assertEqual(inject.call_args.args[0], 'draganddrop')

    def test_short_drag_is_rejected_before_injection(self):
        with patch.object(u, '_input') as inject:
            with self.assertRaises(ValueError):
                u.drag(10, 20, 30, 40, ms=650, wait=0)
            inject.assert_not_called()

    def test_no_extra_swipe_at_budget_end(self):
        with patch.object(u, 'dump_ui', return_value=[]), patch.object(u, 'swipe') as swipe:
            self.assertIsNone(u.scroll_to(text='missing', max_swipes=0))
            swipe.assert_not_called()

    def test_notification_shade_is_not_lock_screen(self):
        wm = 'mCurrentFocus=Window{abc u0 StatusBar}\nmDreamingLockscreen=false\nisStatusBarKeyguard=false'
        with patch.object(u, '_run', side_effect=['mWakefulness=Awake', wm]):
            self.assertIs(u.screen_state()['locked'], False)

    def test_lock_state_unknown_is_not_unlocked(self):
        with patch.object(u, '_run', side_effect=['', '']):
            self.assertIsNone(u.screen_state()['locked'])

    def test_negative_bounds_parse(self):
        self.assertEqual(u._parse_bounds('[-1,2][20,30]'), (-1, 2, 20, 30))

    def test_unknown_selector_is_not_ignored(self):
        with self.assertRaises(TypeError):
            u.find_all([element()], txet='target')

    def test_out_of_range_index(self):
        with self.assertRaises(ValueError):
            u.find([element()], index=-1)

    def test_truncated_png_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.png'
            with patch.object(u, '_run', return_value=b'\x89PNG\r\n\x1a\n' + b'x' * 1200):
                with self.assertRaises(RuntimeError):
                    u.screenshot(path, retries=1)
            self.assertFalse(path.exists())

    def test_shell_characters_are_one_text_argument(self):
        text = "a b;'\"&|$()<>"
        with patch.object(u, '_run', return_value='') as run:
            u.input_text(text, wait=0, char_delay=None)
        command = shlex.split(run.call_args.args[0][1])
        if u.SU_INPUT:
            self.assertEqual(command[:2], ['su', '-c'])
            command = shlex.split(command[2])
        self.assertEqual(command, ['input', 'text', text.replace(' ', '%s')])

    def test_literal_percent_s_survives_android_conversion(self):
        text = 'a%s b%%s'
        with patch.object(u, '_input') as inject:
            u.input_text(text, wait=0, char_delay=None)
        reconstructed = ''.join(c.args[1].replace('%s', ' ') for c in inject.call_args_list)
        self.assertEqual(reconstructed, text)

    def test_stale_ui_is_refreshed_before_click(self):
        with patch.object(u, 'dump_ui', return_value=[element(30, 40)]), \
                patch.object(u, '_input'), patch.object(u, 'display_size', return_value=(100, 100)):
            self.assertEqual(u.tap(ui=[element()], text='target', wait=0), (30, 40))

    def test_outside_coordinates_do_not_inject(self):
        with patch.object(u, 'display_size', return_value=(100, 200)), patch.object(u, '_input') as inject:
            for point in [(-1, 50), (100, 50), (50, 200), (None, 10), (float('nan'), 20)]:
                with self.assertRaises(ValueError):
                    u.tap(*point, wait=0)
            inject.assert_not_called()

    def test_wait_passes_remaining_budget_to_dump(self):
        with patch.object(u, 'dump_ui', side_effect=TimeoutError) as dump:
            self.assertIsNone(u.wait_for(text='missing', timeout=0.2))
            self.assertLessEqual(dump.call_args.kwargs['timeout'], 0.201)

    def test_parse_clips_children_to_parent(self):
        xml = b'<hierarchy><node bounds="[0,0][100,100]" enabled="true"><node text="target" bounds="[-20,90][40,140]" enabled="true"/></node></hierarchy>'
        child = u._parse_ui(xml)[1]
        self.assertEqual(child['visible_bounds'], (0, 90, 40, 100))
        self.assertEqual(child['center'], (20, 95))

    def test_scaled_screenshot_coordinates(self):
        self.assertEqual(u.map_point(100, 200, (540, 1124), viewport=(1080, 2248)), (200, 400))

    def test_webview_offscreen_sliver_is_not_found(self):
        self.assertIsNone(u.find([element(bounds=(0, 100, 300, 103))], text='target'))

    def test_input_verification_rejects_ime_rewrite(self):
        with patch.object(u, '_input'), patch.object(u, 'find_all', return_value=[element(text='a。b', focused=True)]):
            with self.assertRaises(RuntimeError):
                u.input_text('a.b', wait=0, char_delay=0, verify={'rid': 'r'})

    def test_input_target_must_be_focused(self):
        with patch.object(u, '_input') as inject, patch.object(u, 'find_all', return_value=[element(focused=False)]):
            with self.assertRaises(RuntimeError):
                u.input_text('abc', wait=0, verify={'rid': 'r'})
            inject.assert_not_called()

    def test_unlock_unknown_state_never_enters_pin(self):
        with patch.object(u, 'wake'), patch.object(u, 'screen_state', return_value={'locked': None}), \
                patch.object(u, 'input_text') as enter:
            with self.assertRaises(RuntimeError):
                u.unlock('1234')
            enter.assert_not_called()

    def test_image_matching_uniqueness_and_miss(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest('图像模板测试需要可选的 OpenCV / NumPy')
        with tempfile.TemporaryDirectory() as directory:
            template = np.random.default_rng(3).integers(0, 255, (18, 20, 3), dtype=np.uint8)
            screen = np.zeros((100, 160, 3), dtype=np.uint8)
            screen[30:48, 40:60] = template
            template_path, screen_path = Path(directory)/'模板.png', Path(directory)/'screen.png'
            template_path.write_bytes(cv2.imencode('.png', template)[1].tobytes())
            screen_path.write_bytes(cv2.imencode('.png', screen)[1].tobytes())
            hit = u.find_image(template_path, screenshot_path=screen_path)
            self.assertEqual(hit['center'], (50, 39))
            screen[60:78, 110:130] = template
            screen_path.write_bytes(cv2.imencode('.png', screen)[1].tobytes())
            with self.assertRaises(LookupError):
                u.find_image(template_path, screenshot_path=screen_path)
            self.assertIsNone(u.find_image(template_path, region=(0, 0, 30, 30), screenshot_path=screen_path))
            solid = np.zeros((18, 20, 3), dtype=np.uint8)
            solid[:, :, 2] = 255
            template_path.write_bytes(cv2.imencode('.png', solid)[1].tobytes())
            with self.assertRaises(ValueError):
                u.find_image(template_path, screenshot_path=screen_path)


class KeyboardTests(unittest.TestCase):
    def test_hidden_keyboard_does_not_press_back(self):
        with patch.object(u, '_shell', return_value='mInputShown=false'), patch.object(u, 'keyevent') as key:
            u._hide_keyboard()
            key.assert_not_called()

    def test_shown_keyboard_is_hidden_and_checked(self):
        with patch.object(u, '_shell', side_effect=['mInputShown=true', 'mInputShown=false']), \
                patch.object(u, 'keyevent') as key:
            u._hide_keyboard()
            key.assert_called_once_with('BACK', wait=0.2)

    def test_unknown_keyboard_does_not_press_back(self):
        with patch.object(u, '_shell', return_value=''), patch.object(u, 'keyevent') as key:
            with self.assertRaises(RuntimeError):
                u._hide_keyboard()
            key.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
