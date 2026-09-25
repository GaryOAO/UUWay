import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

class SettingsTests(unittest.TestCase):
    def test_fractional_scaling_preserves_absolute_coordinates(self):
        with tempfile.TemporaryDirectory() as name:
            executable=Path(name)/'scale'
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-I',str(ROOT/'src'),
                str(ROOT/'tests/probes/native_input_settings.c'),'-o',str(executable)],check=True,capture_output=True,timeout=15)
            subprocess.run([str(executable)],check=True,timeout=5)

    def test_private_settings_parser_and_no_partial_output(self):
        with tempfile.TemporaryDirectory() as name:
            parent=Path(name); library=parent/'settings.so'
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',
                str(ROOT/'src/native_input_settings.c'),'-ljson-c','-o',str(library)],check=True,capture_output=True,timeout=15)
            read=ctypes.CDLL(str(library)).uurb_input_settings_read
            read.argtypes=[ctypes.c_char_p,ctypes.c_void_p]; read.restype=ctypes.c_int
            output=(ctypes.c_int*3)(77,88,99); path=parent/'input.json'
            self.assertEqual(read(str(path).encode(),output),0)
            valid={'version':1,'relative_percent':75,'wheel_percent':200,'invert_wheel':True}
            path.write_text(json.dumps(valid)); path.chmod(0o600)
            self.assertEqual(read(str(path).encode(),output),1); self.assertEqual(list(output),[75,200,1])
            for payload in ['', '{}', json.dumps(dict(valid,wheel_percent=0)),json.dumps(dict(valid,relative_percent=True)),
                            json.dumps(dict(valid,unknown=1)),json.dumps(valid)+' garbage','x'*2049]:
                path.write_text(payload)
                self.assertEqual(read(str(path).encode(),output),-1); self.assertEqual(list(output),[75,200,1])
            path.write_text(json.dumps(valid)); path.chmod(0o644)
            self.assertEqual(read(str(path).encode(),output),-1)
            path.chmod(0o600); path.rename(parent/'saved'); path.symlink_to(parent/'saved')
            self.assertEqual(read(str(path).encode(),output),-1)

if __name__=='__main__': unittest.main()
