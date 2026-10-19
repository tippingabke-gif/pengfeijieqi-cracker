"""Lifecycle boundaries and public-data filtering; no chess process is started."""
from pathlib import Path
from unittest.mock import patch
import hashlib,json,tempfile,unittest
import runtime

class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.app=self.root/'original/App';self.app.mkdir(parents=True)
        self.exe=self.app/'PengfeiChess.exe';self.exe.write_bytes(b'fixture-program')
        self.profile=patch.dict(runtime.PROFILE,{'target_sha256':hashlib.sha256(self.exe.read_bytes()).hexdigest()})
        self.profile.start();self.addCleanup(self.profile.stop)

    def test_resolve_three_supported_input_forms(self):
        for source in (self.exe,self.app,self.app.parent):
            self.assertEqual(runtime.resolve_source(source),self.exe)

    def test_unknown_binary_rejected_before_copy(self):
        self.exe.write_bytes(b'another-version')
        with self.assertRaisesRegex(runtime.LaunchError,'E_VERSION'):
            runtime.prepare_copy(self.exe,self.root/'data')
        self.assertFalse((self.root/'data').exists())

    def test_copy_preserves_original_and_future_configuration(self):
        (self.app/'config.txt').write_text('original')
        (self.app/'log').mkdir();(self.app/'log'/'old.log').write_text('private history')
        copied=runtime.prepare_copy(self.exe,self.root/'data')
        self.assertEqual(copied.read_bytes(),self.exe.read_bytes())
        self.assertFalse((copied.parent/'log').exists())
        (copied.parent/'config.txt').write_text('saved user setting')
        runtime.prepare_copy(self.exe,self.root/'data')
        self.assertEqual((copied.parent/'config.txt').read_text(),'saved user setting')
        self.assertEqual((self.app/'config.txt').read_text(),'original')

    def test_original_directory_cannot_be_used_as_work_copy(self):
        for directory in (self.app,self.app/'child',self.app.parent,self.root):
            with self.assertRaisesRegex(runtime.LaunchError,'E_DATA_PATH'):
                runtime.prepare_copy(self.exe,directory)

    def test_mismatched_existing_copy_is_not_overwritten(self):
        copied=runtime.prepare_copy(self.exe,self.root/'data');copied.write_bytes(b'changed')
        with self.assertRaisesRegex(runtime.LaunchError,'E_COPY_VERSION'):
            runtime.prepare_copy(self.exe,self.root/'data')
        self.assertEqual(copied.read_bytes(),b'changed')

    def test_public_status_excludes_private_fields(self):
        value=runtime.public_status({'stage':'running','running':True,'source':'private path',
                                    'user':'private name','token':'private token','machine_id':'private id'})
        self.assertEqual(value,{'stage':'running','running':True})

    def test_log_path_is_json_encoded_and_functional_script_retained(self):
        log=self.root/'含空格 name'/'events.jsonl'
        source=runtime.render_script(log)
        self.assertIn('const directLogPath = '+json.dumps(str(log.resolve()))+';',source)
        self.assertNotIn('__PF_LOCAL_LOG__',source)
        self.assertIn('local_auth_replacement_ready',source)
        self.assertIn('fit_input_label_adapter_ready',source)
        self.assertIn('classifier_score_repair_ready',source)

if __name__=='__main__':unittest.main()
