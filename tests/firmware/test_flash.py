"""Exercise the flashing state machine with a fake DFU process; never accesses hardware."""
import contextlib, hashlib, importlib.util, io, json, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
SOURCE=Path(__file__).resolve().parents[2]/'firmware/flash.py'
spec=importlib.util.spec_from_file_location('flash_tool',SOURCE)
flash=importlib.util.module_from_spec(spec);spec.loader.exec_module(flash)
class FlashTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        for p in ('build','recovery','tools/bin','captures'): (self.root/p).mkdir(parents=True)
        (self.root/'tools/bin/dfu-util').touch()
        self.images={}
        for name,size,value in [('custom',1024,11),('upstream',768,22),('official',512,33)]:
            payload=(0x20000400).to_bytes(4,'little')+(0x080002c1).to_bytes(4,'little')+bytes([value])*(size-8)
            suffix=bytearray(16);suffix[8:11]=b'UFD';suffix[11]=16
            filename={'custom':'k10_pro_ansi_rgb_orca_led.bin','upstream':'k10_pro_ansi_rgb_upstream_via_rebuilt.bin','official':'k10_pro_us_rgb_v1.00_official.bin'}[name]
            path=self.root/('recovery' if name=='official' else 'build')/filename
            path.write_bytes(payload+suffix);self.images[name]=(path,payload,hashlib.sha256(path.read_bytes()).hexdigest())
        (self.root/'build/build-manifest.json').write_text(json.dumps({'artifacts':{p.name:{'sha256':sha} for name,(p,data,sha) in self.images.items() if name!='official'}}))
        (self.root/'recovery/official-manifest.json').write_text(json.dumps({'firmware_sha256':self.images['official'][2]}))
        self.backup=self.root/'via.json';empty=hashlib.sha256(b'').hexdigest()
        self.backup.write_text(json.dumps({'schema':'orca-k10-pro-via-backup-v1','verified_twice':True,'device':{'product_id':0x02a0,'vendor_id':0x3434},'keymap_be_hex':'','macro_buffer_hex':'','keymap_sha256':empty,'macro_sha256':empty}))
        self.memory=self.images['official'][1].ljust(131072,b'\xff');self.calls=[];self.badread=False;self.badwrite=False;self.listing='Found DFU [0483:df11] ver=2200, devnum=1, cfg=1, intf=0, path="1-2", alt=0, name="@Internal Flash /0x08000000/064*002Kg", serial="TEST"\n'
    def tearDown(self):self.temp.cleanup()
    def fake_run(self,cmd,**kw):
        self.calls.append(cmd)
        if '-l' in cmd:return SimpleNamespace(stdout=self.listing,stderr='',returncode=0)
        if '-U' in cmd:
            count=int(cmd[cmd.index('-s')+1].split(':')[1]);data=self.memory[:count]
            if self.badread and len([c for c in self.calls if '-U' in c])==2:data=bytes([data[0]^1])+data[1:]
            if self.badwrite and '-D' in sum(self.calls,[]):data=bytes([data[0]^1])+data[1:]
            Path(cmd[cmd.index('-U')+1]).write_bytes(data)
        if '-D' in cmd:
            data=Path(cmd[cmd.index('-D')+1]).read_bytes()
            if data[-8:-5]==b'UFD':data=data[:-16]
            self.memory=data+self.memory[len(data):]
        return SimpleNamespace(stdout='',stderr='',returncode=0)
    def run_main(self,*extra):
        args=['flash.py','--backup',str(self.backup),'--serial','TEST','--device-path','1-2','--output',str(self.root/'captures/new'),*extra]
        with patch.object(flash,'ROOT',self.root),patch.object(sys,'argv',args),patch.object(flash.subprocess,'run',self.fake_run),contextlib.redirect_stdout(io.StringIO()):flash.main()
    def test_plan_does_not_access_device(self):
        self.run_main();self.assertTrue(all('-c' in c for c in self.calls))
    def test_known_official_readback_allows_verified_write(self):
        self.run_main('--execute');receipt=json.loads((self.root/'captures/new/receipt.json').read_text());self.assertTrue(receipt['flash_verified']);self.assertTrue(receipt['leave_requested']);self.assertEqual(receipt['current_known_firmware_matches'][0]['image'],'official')
    def test_readback_only_never_writes_or_leaves(self):
        self.run_main('--execute','--readback-only');self.assertFalse(any('-D' in c or any(':leave' in x for x in c) for c in self.calls))
    def test_unknown_firmware_stops_before_write(self):
        self.memory=b'\x77'*131072
        with self.assertRaisesRegex(RuntimeError,'NO firmware written'):self.run_main('--execute')
        self.assertFalse(any('-D' in c for c in self.calls))
    def test_readback_mismatch_stops_before_write(self):
        self.badread=True
        with self.assertRaisesRegex(RuntimeError,'readbacks do not match'):self.run_main('--execute')
        self.assertFalse(any('-D' in c for c in self.calls))
    def test_wrong_memory_geometry_stops_before_read(self):
        self.listing=self.listing.replace('064*002Kg','128*002Kg')
        with self.assertRaisesRegex(RuntimeError,'DFU reports'):self.run_main('--execute')
        self.assertFalse(any('-U' in c for c in self.calls))
    def test_bad_postwrite_verification_never_leaves(self):
        self.badwrite=True
        with self.assertRaisesRegex(RuntimeError,'differs from the image'):self.run_main('--execute')
        self.assertFalse(any(any(':leave' in x for x in c) for c in self.calls))
    def test_full_restore_requires_explicit_flag_and_same_identity(self):
        self.run_main('--execute','--readback-only');receipt=self.root/'captures/new/receipt.json'
        with self.assertRaisesRegex(RuntimeError,'explicit --restore'):self.run_main('--execute','--restore-readback',str(receipt))
        self.assertFalse(any('-D' in c for c in self.calls))
    def test_verified_original_restores_even_if_current_flash_is_corrupt(self):
        original=self.memory
        self.run_main('--execute','--readback-only')
        receipt=self.root/'captures/new/receipt.json'
        self.memory=b'\x77'*131072
        self.run_main('--execute','--restore-readback',str(receipt),'--restore-full-flash-and-eeprom','--output',str(self.root/'captures/recovery'))
        self.assertEqual(self.memory,original)
        result=json.loads((self.root/'captures/recovery/receipt.json').read_text())
        self.assertTrue(result['flash_verified'])
        self.assertTrue(result['restores_full_flash_and_eeprom'])
        self.assertTrue(result['restoration_source_known_matches'])
    def make_profile(self):
        data=bytearray(b'\xff'*262144)
        data[0x1036f:0x1036f+10]=b'STM32L432\x00'
        data[0x103e1:0x103e1+17]=b'Keychron K10 Pro\x00'
        self.memory=bytes(data)
        self.listing=self.listing.replace('064*002Kg','128*002Kg')
        folder=self.root/'profile';folder.mkdir()
        for name in ('current-flash-read1.bin','current-flash-read2.bin'):(folder/name).write_bytes(self.memory)
        digest=flash.sha(self.memory)
        (folder/'receipt.json').write_text(json.dumps({'current_flash_verified_twice':True,'serial':'TEST','device_path':'1-2','flash_size_bytes':262144,'current_flash_readback_sha256':digest}))
        rom=bytes.fromhex(flash.PROFILE_ROM_HEX);(folder/'rom.bin').write_bytes(rom)
        evidence={'confirmation_source':'installed_firmware_and_rom_profile','mcu':'STM32L432','variant':'unspecified','serial':'TEST','device_path':'1-2','observed_read_span_bytes':262144,'maximum_write_bytes':131072,'official_board_source':{'commit':flash.PROFILE_SOURCE_SHA,'mcu':'STM32L432','matrix_rows':6,'matrix_cols':21,'vendor_id':0x3434,'product_id':0x02a0},'readback_receipt':'receipt.json','installed_flash_sha256':digest,'rom_identification_file':'rom.bin','rom_identification_sha256':flash.sha(rom),'rom_address':0x1fff6ff0}
        path=folder/'profile.json';path.write_text(json.dumps(evidence));return path
    def test_installed_profile_allows_bounded_verified_write(self):
        profile=self.make_profile()
        self.run_main('--execute','--flash-size','262144','--hardware-evidence',str(profile))
        receipt=json.loads((self.root/'captures/new/receipt.json').read_text())
        self.assertTrue(receipt['flash_verified']);self.assertTrue(receipt['installed_firmware_and_rom_profile_verified'])
        self.assertFalse(receipt['hardware_evidence_verified']);self.assertEqual(receipt['mcu_variant'],'unspecified')
        self.assertFalse(any(any(':force' in v for v in c) for c in self.calls))
    def test_installed_profile_wrong_rom_stops_before_device_read(self):
        profile=self.make_profile();(profile.parent/'rom.bin').write_bytes(b'\x00'*16)
        with self.assertRaisesRegex(RuntimeError,'ROM identification mismatch'):
            self.run_main('--execute','--flash-size','262144','--hardware-evidence',str(profile))
        self.assertFalse(any('-U' in c or '-D' in c for c in self.calls))
    def test_installed_profile_changed_current_flash_stops_before_write(self):
        profile=self.make_profile();self.memory=b'\x77'+self.memory[1:]
        with self.assertRaisesRegex(RuntimeError,'Current flash hash'):
            self.run_main('--execute','--flash-size','262144','--hardware-evidence',str(profile))
        self.assertFalse(any('-D' in c for c in self.calls))
    def test_installed_profile_changed_saved_hash_stops_before_device_read(self):
        profile=self.make_profile();e=json.loads(profile.read_text());e['installed_flash_sha256']='0'*64;profile.write_text(json.dumps(e))
        with self.assertRaisesRegex(RuntimeError,'original readback hash'):
            self.run_main('--execute','--flash-size','262144','--hardware-evidence',str(profile))
        self.assertFalse(any('-U' in c or '-D' in c for c in self.calls))
    def test_original_profile_restores_only_first_128k_even_after_corrupt_flash(self):
        profile=self.make_profile();original=self.memory
        original_receipt=profile.parent/'receipt.json'
        captured=json.loads(original_receipt.read_text())
        captured.update(installed_firmware_and_rom_profile_verified=True,hardware_evidence=json.loads(profile.read_text()))
        original_receipt.write_text(json.dumps(captured))
        self.memory=b'\x77'*131072+b'\x88'*131072
        self.run_main('--execute','--flash-size','262144','--restore-readback',str(original_receipt),'--restore-guaranteed-128k-and-eeprom')
        self.assertEqual(self.memory[:131072],original[:131072]);self.assertEqual(self.memory[131072:],b'\x88'*131072)
        receipt=json.loads((self.root/'captures/new/receipt.json').read_text())
        self.assertTrue(receipt['flash_verified']);self.assertTrue(receipt['restoration_source_hardware_verified'])
        self.assertEqual(receipt['payload_bytes'],131072);self.assertTrue(receipt['restores_first_128k_and_eeprom'])
    def test_profile_refuses_full_256k_original_restore(self):
        profile=self.make_profile();receipt=profile.parent/'receipt.json'
        with self.assertRaisesRegex(RuntimeError,'forbids writes beyond'):
            self.run_main('--execute','--flash-size','262144','--hardware-evidence',str(profile),'--restore-readback',str(receipt),'--restore-full-flash-and-eeprom')
        self.assertFalse(any('-D' in c for c in self.calls))
    def test_flash_uses_immutable_staged_image(self):
        original_run=self.fake_run
        def replace_original_at_write(cmd,**kw):
            if '-D' in cmd:
                self.images['custom'][0].write_bytes(b'changed-build-file')
                self.assertEqual(Path(cmd[cmd.index('-D')+1]).name,'validated-image-to-flash.bin')
            return original_run(cmd,**kw)
        self.fake_run=replace_original_at_write
        self.run_main('--execute')
        self.assertEqual(self.memory[:len(self.images['custom'][1])],self.images['custom'][1])
    def make_previous_install(self):
        profile=self.make_profile()
        self.run_main('--execute','--flash-size','262144','--hardware-evidence',str(profile))
        receipt=self.root/'captures/new/receipt.json'
        # The next build intentionally differs. The old staged image remains
        # the sole source of the previous installed payload.
        path,payload,digest=self.images['custom']
        payload=payload[:20]+b'\x3a'+payload[21:]
        path.write_bytes(payload+path.read_bytes()[-16:])
        self.images['custom']=(path,payload,flash.sha(path.read_bytes()))
        manifest=json.loads((self.root/'build/build-manifest.json').read_text())
        manifest['artifacts'][path.name]['sha256']=self.images['custom'][2]
        (self.root/'build/build-manifest.json').write_text(json.dumps(manifest))
        self.calls=[]
        return receipt
    def run_upgrade(self,receipt,*extra):
        self.run_main('--execute','--flash-size','262144','--previous-install-receipt',str(receipt),'--output',str(self.root/'captures/upgrade'),*extra)
    def test_previous_install_allows_bounded_upgrade_and_preserves_eeprom(self):
        previous=self.make_previous_install()
        self.memory=self.memory[:flash.EEPROM_BASE-flash.FLASH_BASE]+b'\x55'*(len(self.memory)-(flash.EEPROM_BASE-flash.FLASH_BASE))
        old_tail=self.memory[flash.EEPROM_BASE-flash.FLASH_BASE:]
        self.run_upgrade(previous)
        receipt=json.loads((self.root/'captures/upgrade/receipt.json').read_text())
        self.assertTrue(receipt['flash_verified']);self.assertTrue(receipt['previous_install_verified'])
        self.assertFalse(receipt['installed_firmware_and_rom_profile_verified'])
        self.assertEqual(receipt['maximum_write_bytes'],131072)
        self.assertEqual(receipt['previous_install_validation']['receipt_sha256'],flash.sha(previous.read_bytes()))
        self.assertEqual(self.memory[:len(self.images['custom'][1])],self.images['custom'][1])
        self.assertEqual(self.memory[flash.EEPROM_BASE-flash.FLASH_BASE:],old_tail)
        self.assertFalse(any(any(token in value for token in (':force',':unprotect',':mass-erase')) for command in self.calls for value in command))
    def test_previous_install_current_prefix_mismatch_stops_before_write(self):
        receipt=self.make_previous_install();self.memory=bytes([self.memory[0]^1])+self.memory[1:]
        with self.assertRaisesRegex(RuntimeError,'Current flash prefix'):self.run_upgrade(receipt)
        self.assertFalse(any('-D' in c for c in self.calls))
    def test_previous_install_tampered_staged_image_stops_before_read(self):
        receipt=self.make_previous_install()
        path=receipt.parent/'validated-image-to-flash.bin';path.chmod(0o600);data=path.read_bytes();path.write_bytes(bytes([data[0]^1])+data[1:])
        with self.assertRaisesRegex(RuntimeError,'staged image hash'):self.run_upgrade(receipt)
        self.assertFalse(any('-U' in c or '-D' in c for c in self.calls))
    def test_previous_install_tampered_written_readback_stops_before_read(self):
        receipt=self.make_previous_install();(receipt.parent/'written-readback.bin').write_bytes(b'bad')
        with self.assertRaisesRegex(RuntimeError,'verified payload readback'):self.run_upgrade(receipt)
        self.assertFalse(any('-U' in c or '-D' in c for c in self.calls))
    def test_previous_install_unverified_receipt_stops_before_read(self):
        receipt=self.make_previous_install();data=json.loads(receipt.read_text());data['flash_verified']=False;receipt.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError,'success, identity'):self.run_upgrade(receipt)
        self.assertFalse(any('-U' in c or '-D' in c for c in self.calls))
    def test_previous_install_wrong_identity_stops_before_read(self):
        receipt=self.make_previous_install()
        with self.assertRaisesRegex(RuntimeError,'success, identity'):self.run_upgrade(receipt,'--serial','OTHER')
        self.assertFalse(any('-U' in c or '-D' in c for c in self.calls))
    def test_previous_install_unknown_original_profile_stops_before_read(self):
        receipt=self.make_previous_install();data=json.loads(receipt.read_text());data['hardware_evidence']['official_board_source']['commit']='unreviewed';receipt.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError,'board source mismatch'):self.run_upgrade(receipt)
        self.assertFalse(any('-U' in c or '-D' in c for c in self.calls))
    def test_previous_install_cannot_bypass_existing_original_profile_guard(self):
        receipt=self.make_previous_install()
        with self.assertRaisesRegex(RuntimeError,'cannot be combined'):self.run_upgrade(receipt,'--hardware-evidence',str(self.root/'profile/profile.json'))
        self.assertFalse(any('-U' in c or '-D' in c for c in self.calls))
    def test_previous_install_chain_is_validated_for_later_upgrade(self):
        first=self.make_previous_install();self.run_upgrade(first)
        second=self.root/'captures/upgrade/receipt.json'
        self.calls=[]
        self.run_upgrade(second,'--output',str(self.root/'captures/third'))
        receipt=json.loads((self.root/'captures/third/receipt.json').read_text())
        self.assertTrue(receipt['flash_verified']);self.assertTrue(receipt['previous_install_verified'])
    def test_previous_install_chain_tampering_stops_before_read(self):
        first=self.make_previous_install();self.run_upgrade(first)
        first.write_text(first.read_text()+'\n');self.calls=[]
        with self.assertRaisesRegex(RuntimeError,'chained receipt hash'):
            self.run_upgrade(self.root/'captures/upgrade/receipt.json','--output',str(self.root/'captures/third'))
        self.assertFalse(any('-U' in c or '-D' in c for c in self.calls))
    def test_upgrade_rejects_payload_overlapping_eeprom_before_device_read(self):
        previous=self.make_previous_install();path,payload,digest=self.images['custom']
        suffix=path.read_bytes()[-16:];path.write_bytes(payload.ljust(flash.EEPROM_BASE-flash.FLASH_BASE+1,b'\xaa')+suffix)
        manifest=json.loads((self.root/'build/build-manifest.json').read_text());manifest['artifacts'][path.name]['sha256']=flash.sha(path.read_bytes());(self.root/'build/build-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError,'overlaps the reserved'):self.run_upgrade(previous)
        self.assertFalse(any('-U' in c or '-D' in c for c in self.calls))
    def test_absent_optional_official_recovery_does_not_block_profile_upgrade(self):
        receipt=self.make_previous_install()
        self.images['official'][0].unlink();(self.root/'recovery/official-manifest.json').unlink()
        self.run_upgrade(receipt)
        self.assertTrue(json.loads((self.root/'captures/upgrade/receipt.json').read_text())['flash_verified'])
    def test_present_but_tampered_optional_image_still_stops_before_write(self):
        receipt=self.make_previous_install();self.images['official'][0].write_bytes(b'bad')
        with self.assertRaisesRegex(RuntimeError,'Firmware SHA-256 differs'):self.run_upgrade(receipt)
        self.assertFalse(any('-D' in c for c in self.calls))
    def test_explicit_official_request_requires_official_image(self):
        self.images['official'][0].unlink()
        with self.assertRaises(FileNotFoundError):self.run_main('--image','official','--execute')
        self.assertFalse(any('-U' in c or '-D' in c for c in self.calls))
if __name__=='__main__':unittest.main()
