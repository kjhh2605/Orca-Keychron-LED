"""Restore rounding regression tests. Pure simulated VIA, never opens HID hardware."""
import importlib.util, unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('restore',Path(__file__).resolve().parents[2]/'scripts/restore-via.py')
restore=importlib.util.module_from_spec(spec);spec.loader.exec_module(restore)
class LightingRestoreTests(unittest.TestCase):
    def run_restore(self,target,model=lambda v:(v*255)>>8,channel=3):
        self.calls=[];self.value=None
        def write(command):
            self.calls.append(command)
            if command[:3]==[7,channel,1]:self.value=model(command[3])
        def read(*prefix):return bytes([*prefix,self.value]+[0]*28)
        result=restore.restore_lighting(write,read,channel,{'1':f'{target:02x}','2':'14','3':'8f','4':'8e59'})
        return result
    def test_observed_regression_203_is_restored_exactly(self):
        result=self.run_restore(203)
        self.assertEqual(result['initial_readback'],202);self.assertEqual(result['corrected_write'],204)
        self.assertEqual(self.value,203);self.assertEqual(self.calls[-2:],[ [7,3,2,20],[9,3] ])
    def test_fixed_firmware_does_not_receive_extra_correction(self):
        self.assertIsNone(self.run_restore(203,lambda v:v))
        self.assertEqual([c for c in self.calls if c[:3]==[7,3,1]],[[7,3,1,203]])
    def test_zero_remains_zero(self):
        self.assertIsNone(self.run_restore(0));self.assertEqual(self.value,0)
    def test_lowest_positive_value_is_representable(self):
        self.assertEqual(self.run_restore(1)['corrected_write'],2);self.assertEqual(self.value,1)
    def test_highest_representable_value(self):
        self.assertEqual(self.run_restore(254)['corrected_write'],255);self.assertEqual(self.value,254)
    def test_unrepresentable_255_is_not_silently_accepted(self):
        with self.assertRaisesRegex(RuntimeError,'no approximate'):self.run_restore(255)
        self.assertFalse(any(c[0]==9 for c in self.calls))
    def test_unexpected_error_is_not_treated_as_rounding(self):
        with self.assertRaisesRegex(RuntimeError,'no approximate'):self.run_restore(203,lambda v:v-2)
        self.assertFalse(any(c[0]==9 for c in self.calls))
    def test_other_lighting_channel_not_given_rgb_matrix_workaround(self):
        with self.assertRaisesRegex(RuntimeError,'no approximate'):self.run_restore(203,channel=2)
        self.assertFalse(any(c[0]==9 for c in self.calls))
class KeymapMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Public synthetic fixture: preserve the six audited positions without
        # requiring or redistributing any user's keymap/macros/backup files.
        source=bytearray(1008)
        cls.locations={212:0x7e00,232:0x7e01,214:0x7e02,230:0x7e03,6:0x7e04,8:0x7e05}
        target=bytearray(source)
        for offset,code in cls.locations.items():
            source[offset:offset+2]=code.to_bytes(2,'big')
            target[offset:offset+2]=restore.CUSTOM_MIGRATION[code].to_bytes(2,'big')
        cls.source=bytes(source);cls.target=bytes(target)
    def setUp(self):
        self.original_hash=patch.object(restore,'ORIGINAL_KEYMAP_SHA',restore.module.digest(self.source))
        self.repaired_hash=patch.object(restore,'REPAIRED_KEYMAP_SHA',restore.module.digest(self.target))
        self.original_hash.start();self.repaired_hash.start()
        self.addCleanup(self.original_hash.stop);self.addCleanup(self.repaired_hash.stop)
    def test_raw_custom_keymap_write_requires_explicit_profile(self):
        with self.assertRaisesRegex(RuntimeError,'explicit --keymap-profile'):restore.prepare_keymap(self.source)
    def test_lighting_only_remains_available_without_profile(self):
        value,audit=restore.prepare_keymap(self.source,writing=False)
        self.assertEqual(value,self.source);self.assertIsNone(audit)
    def test_six_semantic_changes_match_expected_synthetic_map(self):
        value,audit=restore.prepare_keymap(self.source,restore.KEYMAP_PROFILE)
        self.assertEqual(value,self.target)
        self.assertEqual(audit['target_keymap_sha256'],restore.module.digest(self.target))
        self.assertEqual(len(audit['changed_keys']),6);self.assertEqual(audit['unchanged_keys'],498)
        expected={(0,5,1):('7e00','7e02'),(0,5,11):('7e01','7e03'),(0,5,2):('7e02','7e04'),(0,5,10):('7e03','7e05'),(0,0,3):('7e04','7e00'),(0,0,4):('7e05','7e01')}
        self.assertEqual({(c['layer'],c['row'],c['column']):(c['source'],c['target']) for c in audit['changed_keys']},expected)
    def test_unknown_original_is_rejected_before_migration(self):
        source=b'\x00\x04'+self.source[2:]
        with self.assertRaisesRegex(RuntimeError,'source hash mismatch'):restore.prepare_keymap(source,restore.KEYMAP_PROFILE)
    def test_migration_is_never_applied_twice(self):
        value,_=restore.prepare_keymap(self.source,restore.KEYMAP_PROFILE)
        with self.assertRaisesRegex(RuntimeError,'source hash mismatch'):restore.prepare_keymap(value,restore.KEYMAP_PROFILE)
    def test_original_completed_and_partial_retry_are_safe(self):
        target,audit=restore.prepare_keymap(self.source,restore.KEYMAP_PROFILE)
        restore.verify_migration_precondition(self.source,self.source,target)
        restore.verify_migration_precondition(target,self.source,target)
        partial=bytearray(self.source);offset=audit['changed_keys'][0]['offset'];partial[offset:offset+2]=target[offset:offset+2]
        restore.verify_migration_precondition(partial,self.source,target)
    def test_unrelated_user_edit_prevents_any_migration(self):
        target,_=restore.prepare_keymap(self.source,restore.KEYMAP_PROFILE)
        with self.assertRaisesRegex(RuntimeError,'no changes made'):restore.verify_migration_precondition(b'\x00\x04'+self.source[2:],self.source,target)
    def test_repaired_profile_preserves_already_migrated_codes(self):
        value,audit=restore.prepare_keymap(self.target,restore.PINNED_KEYMAP_PROFILE)
        self.assertEqual(value,self.target);self.assertEqual(audit['changed_keys'],[])
        self.assertEqual(audit['unchanged_keys'],504)
    def test_repaired_profile_rejects_original_and_later_user_changes(self):
        for value in (self.source,b'\x00\x04'+self.target[2:]):
            with self.assertRaisesRegex(RuntimeError,'source hash mismatch'):
                restore.prepare_keymap(value,restore.PINNED_KEYMAP_PROFILE)

class ProfileTargetTests(unittest.TestCase):
    def setUp(self):
        self.state={'via_protocol':12,'layers':4,'rows':6,'columns':21,'keymap_be_hex':bytes(1008).hex()}
        self.query={'firmware_verified':True,'version':1,'theme_protocol':1}
        self.image=b'synthetic-reviewed-image'
        self.allowed=patch.object(restore,'TARGET_IMAGE_SHAS',(restore.module.digest(self.image),))
        self.allowed.start();self.addCleanup(self.allowed.stop)
    def test_exact_reviewed_image_and_matrix_accept(self):
        self.assertEqual(restore.verify_profile_target(self.state,self.state,self.query,self.image),restore.module.digest(self.image))
    def test_unreviewed_image_rejects(self):
        with self.assertRaisesRegex(RuntimeError,'differs from the reviewed'):
            restore.verify_profile_target(self.state,self.state,self.query,b'other-image')
    def test_same_via_protocol_is_insufficient_for_unreviewed_matrix(self):
        for field,value in (('layers',3),('rows',7),('columns',20),('keymap_be_hex','0000')):
            with self.assertRaisesRegex(RuntimeError,'reviewed 4-layer'):
                restore.verify_profile_target(self.state,{**self.state,field:value},self.query,self.image)
    def test_unknown_orca_protocol_rejects(self):
        for changes in ({'version':2},{'theme_protocol':2},{'firmware_verified':False}):
            with self.assertRaisesRegex(RuntimeError,'reviewed Orca'):
                restore.verify_profile_target(self.state,self.state,{**self.query,**changes},self.image)

class ScopeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.current={'keymap_be_hex':'7e047e02','macro_buffer_hex':'010203','layout_options_be_hex':'12345678','lighting':{'channel':3,'fields':{'1':'42'}}}
        self.backup={'layout_options_be_hex':'00000000','lighting':{'channel':3,'fields':{'1':'cb'}}}
    def test_keymap_only_preserves_later_user_macro_layout_and_lighting(self):
        expected=restore.expected_fields(self.current,self.backup,b'\x7e\x04\x7e\x02',b'\0\0\0','keymap_only')
        self.assertEqual(expected,self.current)
        self.assertNotEqual(expected['lighting'],self.backup['lighting'])
    def test_lighting_only_preserves_migrated_keymap_and_later_user_fields(self):
        expected=restore.expected_fields(self.current,self.backup,b'\x7e\x02\x7e\x00',b'\0\0\0','lighting_only')
        self.assertEqual(expected,{**self.current,'lighting':self.backup['lighting']})
    def test_full_restore_expects_all_selected_saved_fields(self):
        expected=restore.expected_fields(self.current,self.backup,b'\x7e\x04',b'\0\0\0','all_via_fields')
        self.assertEqual(expected,{'keymap_be_hex':'7e04','macro_buffer_hex':'000000','layout_options_be_hex':'00000000','lighting':self.backup['lighting']})

class ViaDefinitionTests(unittest.TestCase):
    def test_custom_definition_changes_only_first_six_code_labels(self):
        import json
        root=Path(__file__).resolve().parents[2]/'firmware/recovery'
        official=json.loads((root/'k10_pro_us_rgb_v1.0.json').read_text())
        custom=json.loads((root/'k10_pro_ansi_rgb_orca_led_618127a_via.json').read_text())
        entries=official['customKeycodes']
        self.assertEqual(custom,{**official,'customKeycodes':[entries[i] for i in [4,5,0,1,2,3]]+entries[6:]})

if __name__=='__main__':unittest.main()
