"""Mock platform and metadata enumeration; never enumerate/open real devices."""
import contextlib
import io
import json
import sys
from types import SimpleNamespace as N
import unittest
from unittest.mock import Mock, patch

from conductor import app, devices
from conductor.capture import open_selected_camera


def cfg(backend='avfoundation', index=0):
    return dict(camera_backend=backend, camera_index=index, camera_name='')


DEVICES = [dict(index=0, id='usb-id', name='USB camera', built_in=False),
           dict(index=1, id='builtin-id', name='FaceTime HD Camera', built_in=True)]


class CameraSelectionTests(unittest.TestCase):
    def test_mac_defaults_and_explicit_windows_backend_error(self):
        with patch.object(sys, 'platform', 'darwin'):
            for backend in ('dshow', 'msmf', 'auto', 'avfoundation'):
                self.assertEqual(devices.camera_backend(backend), 'avfoundation')
            for backend in ('dshow', 'msmf'):
                with self.assertRaises(ValueError):
                    devices.camera_backend('dshow', backend)

    def test_windows_backend_behavior_preserved(self):
        with patch.object(sys, 'platform', 'win32'):
            for backend in ('dshow', 'msmf', 'auto'):
                self.assertEqual(devices.camera_backend(backend), backend)

    def test_builtin_selected_by_type_not_index_or_label(self):
        renamed = [dict(DEVICES[0], name='Built-in misleading USB name'),
                   dict(DEVICES[1], name='Localized camera label')]
        with patch.object(devices, 'list_devices', return_value=renamed):
            selected = devices.resolve_camera(cfg(), built_in=True)
        self.assertEqual((selected['camera_index'], selected['camera_id']), (1, 'builtin-id'))

    def test_missing_or_ambiguous_builtin_fails_without_index_fallback(self):
        for rows in ([DEVICES[0]], [dict(row, built_in=True) for row in DEVICES], []):
            with patch.object(devices, 'list_devices', return_value=rows), self.assertRaises(ValueError):
                devices.resolve_camera(cfg(), built_in=True)

    def test_unique_name_and_explicit_index_selection(self):
        with patch.object(devices, 'list_devices', return_value=DEVICES):
            named = devices.resolve_camera(cfg(), 'FACETIME')
            indexed = devices.resolve_camera(cfg(index=0))
        self.assertEqual(named['camera_id'], 'builtin-id')
        self.assertEqual(indexed['camera_id'], 'usb-id')

    def test_bad_name_ambiguous_name_and_unknown_index_fail(self):
        with patch.object(devices, 'list_devices', return_value=DEVICES):
            for name in ('missing', 'camera'):
                with self.assertRaises(ValueError):
                    devices.resolve_camera(cfg(), name)
            with self.assertRaises(ValueError):
                devices.resolve_camera(cfg(index=7))

    def test_native_metadata_order_matches_locked_opencv_and_classifies_type(self):
        def native(uid, name, kind):
            return N(uniqueID=lambda: uid, localizedName=lambda: name, deviceType=lambda: kind)
        av = N(AVMediaTypeVideo='video', AVMediaTypeMuxed='muxed',
               AVCaptureDeviceTypeBuiltInWideAngleCamera='built-in',
               AVCaptureDevice=N(devicesWithMediaType_=lambda kind:
                   [native('z', 'Internal', 'built-in')] if kind == 'video' else
                   [native('a', 'External', 'usb')]))
        with patch.dict(sys.modules, {'AVFoundation': av}):
            rows = devices.list_devices('avfoundation')
        self.assertEqual([(r['index'], r['id'], r['built_in']) for r in rows],
                         [(0, 'a', False), (1, 'z', True)])

    def test_native_unavailable_reports_missing_dependency(self):
        with patch.dict(sys.modules, {'AVFoundation': None}), self.assertRaisesRegex(
                ImportError, 'separate conductor environment'):
            devices.list_devices('avfoundation')

    def test_windows_enumerator_metadata_preserved(self):
        enumerate_cameras = Mock(return_value=[N(index=3, name='USB', vid=1, pid=None)])
        with patch.dict(sys.modules, {'cv2': N(CAP_DSHOW=700),
                'cv2_enumerate_cameras': N(enumerate_cameras=enumerate_cameras)}):
            rows = devices.list_devices('dshow')
        enumerate_cameras.assert_called_once_with(700)
        self.assertEqual(rows, [dict(index=3, name='USB', backend='dshow', vid='0001', pid=None)])

    def test_open_rechecks_identity_and_uses_exact_backend(self):
        selected = dict(cfg(index=1), camera_id='builtin-id')
        capture = Mock()
        with patch.object(devices, 'list_devices', return_value=DEVICES), patch.dict(
                sys.modules, {'cv2': N(CAP_AVFOUNDATION=1200, VideoCapture=capture)}):
            open_selected_camera(selected)
        capture.assert_called_once_with(1, 1200)

    def test_identity_change_or_disappearance_never_opens_another_camera(self):
        selected = dict(cfg(index=1), camera_id='builtin-id')
        for rows in ([], [dict(DEVICES[0], index=1)], [dict(DEVICES[1], index=0)]):
            capture = Mock()
            with patch.object(devices, 'list_devices', return_value=rows), patch.dict(
                    sys.modules, {'cv2': N(CAP_AVFOUNDATION=1200, VideoCapture=capture)}):
                with self.assertRaises(ValueError):
                    open_selected_camera(selected)
            capture.assert_not_called()

    def test_cli_options_and_conflicts(self):
        args = app.parse_args(['--camera-built-in', '--backend', 'avfoundation'])
        self.assertTrue(args.camera_built_in)
        for extra in (['--camera', '0'], ['--camera-name', 'USB']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app.parse_args(['--camera-built-in'] + extra)

    def test_actual_app_defaults_to_builtin_on_mac_and_respects_selection(self):
        for args, builtin, name in (([], True, None), (['--camera', '0'], False, None),
                                    (['--camera-name', 'USB'], False, 'USB')):
            with patch.object(sys, 'platform', 'darwin'), patch.object(sys, 'argv', ['app'] + args), \
                    patch.object(devices, 'resolve_camera', side_effect=RuntimeError('selection boundary')) as resolve:
                with self.assertRaisesRegex(RuntimeError, 'selection boundary'):
                    app.main()
                call = resolve.call_args
                self.assertEqual(call.args[0]['camera_backend'], 'avfoundation')
                self.assertEqual(call.args[1], name)
                self.assertEqual(call.kwargs['built_in'], builtin)

    def test_list_command_returns_before_imaging_imports_or_streams(self):
        output = io.StringIO()
        with patch.object(sys, 'platform', 'darwin'), patch.object(sys, 'argv', ['app', '--list-cameras']), \
                patch.object(devices, '_mac_devices', return_value=DEVICES), \
                patch.dict(sys.modules, {'cv2': None, 'numpy': None}), contextlib.redirect_stdout(output):
            self.assertEqual(app.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), DEVICES)


if __name__ == '__main__':
    unittest.main()
