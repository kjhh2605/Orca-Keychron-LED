#!/usr/bin/env python3
"""Guarded K10 Pro ANSI RGB DFU workflow. Default prints a plan; never enters bootloader."""
from pathlib import Path
from datetime import datetime, timezone
import argparse, hashlib, json, os, re, shlex, subprocess, sys
ROOT=Path(__file__).resolve().parent
FLASH_BASE=0x08000000
FLASH_SIZE=0x40000  # Pinned K10 Pro STM32L432 linker map: 256 KiB, 64 KiB RAM.
EEPROM_BASE=0x0801e000  # Pinned legacy emulation assumes 128 KiB flash and reserves its last 8 KiB.

def sha(data):return hashlib.sha256(data).hexdigest()
def firmware_payload(data):
    if len(data)<32 or data[-8:-5]!=b'UFD' or data[-5]!=16:raise RuntimeError('Expected a 16-byte DFU suffix')
    payload=data[:-16]
    if len(payload)>EEPROM_BASE-FLASH_BASE:raise RuntimeError('Image overlaps the reserved tail region; refusing automatic write')
    sp=int.from_bytes(payload[:4],'little');reset=int.from_bytes(payload[4:8],'little')
    if not 0x20000000<=sp<=0x20010000 or not FLASH_BASE<reset<FLASH_BASE+FLASH_SIZE or not reset&1:raise RuntimeError('Image does not have plausible STM32L432 vectors')
    return payload

def image_data(args):
    manifest=json.loads((ROOT/'build/build-manifest.json').read_text())
    if args.image=='official':
        path=ROOT/'recovery/k10_pro_us_rgb_v1.00_official.bin'
        expected=json.loads((ROOT/'recovery/official-manifest.json').read_text())['firmware_sha256']
    else:
        name='k10_pro_ansi_rgb_orca_led.bin' if args.image=='custom' else 'k10_pro_ansi_rgb_upstream_via_rebuilt.bin'
        path=ROOT/'build'/name;expected=manifest['artifacts'][name]['sha256']
    data=path.read_bytes()
    if sha(data)!=expected:raise RuntimeError('Firmware SHA-256 differs from its recorded manifest')
    payload=firmware_payload(data)
    return path,payload,expected

PROFILE_SOURCE_SHA='666862cb8123b64a6b96718d739c6203ad99031f'
PROFILE_ROM_HEX='ffff1000120011002000220031009100'
def validate_installed_profile(evidence, evidence_path, serial, device_path, observed_span):
    """Verify captured evidence; never infer KB/KC or physical capacity from a DFU map."""
    if (evidence.get('confirmation_source')!='installed_firmware_and_rom_profile'
        or evidence.get('mcu')!='STM32L432' or evidence.get('variant')!='unspecified'
        or evidence.get('serial')!=serial or evidence.get('device_path')!=device_path
        or observed_span!=262144 or evidence.get('observed_read_span_bytes')!=observed_span
        or evidence.get('maximum_write_bytes')!=131072):
        raise RuntimeError('Installed firmware profile identity, span or write bound mismatch')
    official=evidence.get('official_board_source',{})
    if official != {'commit':PROFILE_SOURCE_SHA,'mcu':'STM32L432','matrix_rows':6,'matrix_cols':21,'vendor_id':0x3434,'product_id':0x02a0}:
        raise RuntimeError('Installed firmware profile official board source mismatch')
    base=Path(evidence_path).parent
    capture_path=base/evidence['readback_receipt']
    capture=json.loads(capture_path.read_text())
    expected=evidence.get('installed_flash_sha256')
    original=(capture_path.parent/'current-flash-read1.bin').read_bytes()
    second=(capture_path.parent/'current-flash-read2.bin').read_bytes()
    if (capture.get('current_flash_verified_twice') is not True or capture.get('serial')!=serial
        or capture.get('device_path')!=device_path or capture.get('flash_size_bytes')!=observed_span
        or capture.get('current_flash_readback_sha256')!=expected or len(original)!=observed_span
        or original!=second or sha(original)!=expected):
        raise RuntimeError('Installed firmware profile original readback hash or receipt mismatch')
    for offset,literal in ((0x1036f,b'STM32L432'),(0x103e1,b'Keychron K10 Pro')):
        if original[offset:offset+len(literal)+1]!=literal+b'\x00':
            raise RuntimeError('Installed firmware profile target literal mismatch')
    rom=(base/evidence['rom_identification_file']).read_bytes()
    if (len(rom)!=16 or rom.hex()!=PROFILE_ROM_HEX or sha(rom)!=evidence.get('rom_identification_sha256')
        or evidence.get('rom_address')!=0x1fff6ff0 or rom[14]!=0x91):
        raise RuntimeError('Installed firmware profile ROM identification mismatch')
    return expected

def validate_previous_install(receipt_path, serial, device_path, observed_span, seen=()):
    """Validate the complete local evidence chain, never a receipt flag by itself.

    A first installation must be rooted in the original installed-firmware/ROM
    profile. Each later receipt must bind its twice-read pre-flash prefix to its
    predecessor's verified payload. EEPROM bytes may legitimately change.
    """
    receipt_path=Path(receipt_path).resolve()
    if receipt_path in seen or len(seen)>=32:raise RuntimeError('Previous installation receipt chain is cyclic or too long')
    raw=receipt_path.read_bytes();receipt=json.loads(raw);base=receipt_path.parent
    if (receipt.get('mode')!='execute' or receipt.get('target')!='K10 Pro ANSI RGB'
        or receipt.get('flash_written') is not True or receipt.get('flash_verified') is not True
        or receipt.get('serial')!=serial or receipt.get('device_path')!=device_path
        or observed_span!=262144 or receipt.get('flash_size_bytes')!=observed_span
        or receipt.get('maximum_write_bytes')!=131072 or receipt.get('mcu_variant')!='unspecified'
        or receipt.get('mass_erase_or_unprotect') is not False
        or receipt.get('restores_full_flash_and_eeprom') is not False
        or receipt.get('restores_first_128k_and_eeprom') is not False):
        raise RuntimeError('Previous installation success, identity or bounded-write evidence mismatch')
    staged=(base/'validated-image-to-flash.bin').read_bytes()
    if sha(staged)!=receipt.get('staged_image_sha256') or sha(staged)!=receipt.get('image_sha256'):
        raise RuntimeError('Previous installation staged image hash mismatch')
    payload=firmware_payload(staged)
    if len(payload)!=receipt.get('payload_bytes') or (base/'written-readback.bin').read_bytes()!=payload:
        raise RuntimeError('Previous installation verified payload readback mismatch')
    before=(base/'current-flash-read1.bin').read_bytes()
    if (receipt.get('current_flash_verified_twice') is not True or len(before)!=observed_span
        or before!=(base/'current-flash-read2.bin').read_bytes()
        or sha(before)!=receipt.get('current_flash_readback_sha256')):
        raise RuntimeError('Previous installation pre-flash duplicate readback mismatch')
    chain=receipt.get('previous_install_validation')
    if chain is None:
        if receipt.get('installed_firmware_and_rom_profile_verified') is not True:
            raise RuntimeError('Previous installation has no verified original ROM profile')
        hardware=receipt.get('hardware_evidence') or {}
        original_hash=validate_installed_profile(hardware,receipt_path,serial,device_path,observed_span)
        if sha(before)!=original_hash:raise RuntimeError('Previous installation original hardware readback mismatch')
    else:
        if receipt.get('previous_install_verified') is not True or not isinstance(chain,dict):
            raise RuntimeError('Previous installation chain verification flag mismatch')
        prior_path=base/chain.get('receipt_path','')
        if not prior_path.is_file() or sha(prior_path.read_bytes())!=chain.get('receipt_sha256'):
            raise RuntimeError('Previous installation chained receipt hash mismatch')
        prior=validate_previous_install(prior_path,serial,device_path,observed_span,(*seen,receipt_path))
        if (chain.get('image_sha256')!=prior['image_sha256']
            or chain.get('payload_sha256')!=sha(prior['payload'])
            or chain.get('payload_bytes')!=len(prior['payload'])
            or chain.get('original_profile_sha256')!=prior['original_profile_sha256']
            or before[:len(prior['payload'])]!=prior['payload']):
            raise RuntimeError('Previous installation chain payload or original profile mismatch')
        original_hash=prior['original_profile_sha256']
    return {'receipt_path':str(receipt_path),'receipt_sha256':sha(raw),
            'image_sha256':sha(staged),'payload':payload,'original_profile_sha256':original_hash}

def previous_install_audit(previous, output):
    return {key:value for key,value in previous.items() if key not in ('payload','receipt_path')} | {
        'receipt_path':os.path.relpath(previous['receipt_path'],output.resolve()),
        'payload_bytes':len(previous['payload']),'payload_sha256':sha(previous['payload'])}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',choices=['custom','official','upstream'],default='custom')
    parser.add_argument('--restore-readback',type=Path,help='Receipt for a twice-verified original full-flash readback of this exact device')
    parser.add_argument('--restore-full-flash-and-eeprom',action='store_true',help='Required for executing an exact original full-flash restoration, including EEPROM region')
    parser.add_argument('--restore-guaranteed-128k-and-eeprom',action='store_true',help='Restore only the first 128 KiB of a validated 256 KiB original capture; use when KB/KC variant is unknown')
    parser.add_argument('--backup',type=Path,required=True,help='Verified read-only VIA backup.json')
    parser.add_argument('--serial',help='Exact DFU serial from dfu-util --list')
    parser.add_argument('--device-path',help='Exact DFU bus-port path from dfu-util --list')
    parser.add_argument('--flash-size',type=int,choices=[131072,262144],default=131072,help='Exact advertised DFU flash size; Keychron advertises 128 KiB')
    parser.add_argument('--hardware-evidence',type=Path,help='Reviewed exact-MCU evidence or verified installed firmware + ROM profile JSON')
    parser.add_argument('--previous-install-receipt',type=Path,help='Explicit upgrade: verify this successful installation and its original ROM-profile evidence chain before any write')
    parser.add_argument('--execute',action='store_true',help='Perform readback, write, verification, and leave DFU')
    parser.add_argument('--readback-only',action='store_true',help='With --execute, only read current flash twice and remain in DFU')
    parser.add_argument('--output',type=Path,default=ROOT/'backups'/datetime.now(timezone.utc).strftime('dfu-%Y%m%dT%H%M%SZ'))
    args=parser.parse_args();os.umask(0o077)
    flash_size=args.flash_size
    if args.previous_install_receipt and (args.hardware_evidence or args.restore_readback):
        raise RuntimeError('Previous-install upgrades cannot be combined with original hardware evidence or readback restoration')
    previous=None
    if args.previous_install_receipt:
        if not args.serial or not args.device_path:raise RuntimeError('Previous-install upgrades require the exact original device serial/path')
        previous=validate_previous_install(args.previous_install_receipt,args.serial,args.device_path,flash_size)
    backup=json.loads(args.backup.read_text())
    if backup.get('schema')!='orca-k10-pro-via-backup-v1' or backup.get('verified_twice') is not True or backup.get('device',{}).get('product_id')!=0x02a0 or backup.get('device',{}).get('vendor_id')!=0x3434:raise RuntimeError('Verified K10 Pro ANSI RGB VIA backup required')
    for field in ('keymap','macro'):
        data=bytes.fromhex(backup['keymap_be_hex' if field=='keymap' else 'macro_buffer_hex'])
        if sha(data)!=backup[field+'_sha256']:raise RuntimeError('VIA backup hash mismatch')
    source_receipt=None
    source_original_full_hash=None
    if args.restore_readback:
        source_receipt=json.loads(args.restore_readback.read_text())
        path=args.restore_readback.parent/'current-flash-read1.bin'
        payload=path.read_bytes();expected=source_receipt.get('current_flash_readback_sha256')
        second=(args.restore_readback.parent/'current-flash-read2.bin').read_bytes()
        if source_receipt.get('current_flash_verified_twice') is not True or payload!=second or sha(payload)!=expected or len(payload)!=flash_size or source_receipt.get('flash_size_bytes')!=flash_size:
            raise RuntimeError('Original readback receipt, size or duplicate verification mismatch')
        source_original_full_hash=sha(payload)
        if args.restore_full_flash_and_eeprom and args.restore_guaranteed_128k_and_eeprom:raise RuntimeError('Choose exactly one restoration extent')
        if args.execute and (not (args.restore_full_flash_and_eeprom or args.restore_guaranteed_128k_and_eeprom) or source_receipt.get('serial')!=args.serial or source_receipt.get('device_path')!=args.device_path):
            raise RuntimeError('Full restoration requires explicit --restore-full-flash-and-eeprom or --restore-guaranteed-128k-and-eeprom and the exact original device serial/path')
    else:
        if args.restore_full_flash_and_eeprom or args.restore_guaranteed_128k_and_eeprom:raise RuntimeError('Full-flash flag is allowed only with --restore-readback')
        path,payload,expected=image_data(args)
    staged_image=path.read_bytes()
    if args.restore_guaranteed_128k_and_eeprom:
        if flash_size!=262144:raise RuntimeError('Bounded restoration requires a verified 256 KiB capture')
        payload=payload[:131072];staged_image=payload;expected=sha(payload)
    if sha(staged_image)!=expected:raise RuntimeError('Image changed while preparing the flash; refusing operation')
    tools_dir=ROOT/'tools/bin'
    if not (tools_dir/'dfu-util').exists():tools_dir=ROOT/'vendor/dfu-tools/bin'
    tool=tools_dir/'dfu-util'
    suffix=tools_dir/'dfu-suffix'
    if source_receipt is None:subprocess.run([str(suffix),'-c',str(path)],check=True,stdout=subprocess.DEVNULL)
    selector=[str(tool),'-d','0483:df11','-a','0']
    if args.serial:selector+=['-S',args.serial]
    if args.device_path:selector+=['-p',args.device_path]
    commands=[selector+['-s',f'0x08000000:{flash_size}','-U',str(args.output/'current-flash-read1.bin')],
              selector+['-s',f'0x08000000:{flash_size}','-U',str(args.output/'current-flash-read2.bin')]]
    if not args.readback_only:commands += [selector+['-s','0x08000000','-D',str(args.output/'validated-image-to-flash.bin')],
               selector+['-s',f'0x08000000:{len(payload)}','-U',str(args.output/'written-readback.bin')],
               selector+['-s','0x08000000:16:leave','-U',str(args.output/'leave-vector-read.bin')]]
    plan={'mode':'execute' if args.execute else 'plan_only','target':'K10 Pro ANSI RGB','image':str(path),'image_sha256':expected,'serial':args.serial,'device_path':args.device_path,'restores_full_flash_and_eeprom':source_receipt is not None and not args.restore_guaranteed_128k_and_eeprom,'restores_first_128k_and_eeprom':args.restore_guaranteed_128k_and_eeprom,'payload_bytes':len(payload),'flash_size_bytes':flash_size,'via_backup':str(args.backup),'enters_bootloader':False,'mass_erase_or_unprotect':False,'previous_install_validation':previous_install_audit(previous,args.output) if previous else None,'commands':[shlex.join(c) for c in commands]}
    print(json.dumps(plan,indent=2),flush=True)
    if not args.execute:return
    hardware_verified=False
    hardware=None
    installed_profile_hash=None
    if args.hardware_evidence is not None:
        hardware=json.loads(args.hardware_evidence.read_text())
        if hardware.get('confirmation_source')=='installed_firmware_and_rom_profile':
            installed_profile_hash=validate_installed_profile(hardware,args.hardware_evidence,args.serial,args.device_path,flash_size)
            if len(payload)>hardware['maximum_write_bytes']:raise RuntimeError('Installed firmware profile forbids writes beyond the guaranteed first 128 KiB')
        expected_size={'STM32L432KB':131072,'STM32L432KC':262144}.get(hardware.get('mcu'))
        if installed_profile_hash is None and (hardware.get('reviewed') is not True or expected_size!=flash_size or hardware.get('serial')!=args.serial or hardware.get('device_path')!=args.device_path or hardware.get('confirmation_source') not in ('physical_chip_marking','rom_device_identification') or not hardware.get('evidence')):
            raise RuntimeError('Hardware evidence does not confirm this exact selected MCU, capacity and device')
        hardware_verified=installed_profile_hash is None
    if not args.serial or not args.device_path:raise RuntimeError('Execution requires both exact --serial and --device-path from a fresh DFU listing')
    listing=subprocess.run([str(tool),'-l'],check=True,capture_output=True,text=True).stdout
    matches=[line for line in listing.splitlines() if '[0483:df11]' in line.lower() and re.search(r'\balt=0\b',line) and f'serial="{args.serial}"' in line and f'path="{args.device_path}"' in line]
    if len(matches)!=1 or not re.search(r'@Internal Flash\s*/0x08000000/',matches[0]):raise RuntimeError('Expected exactly one selected STM32 Internal Flash DFU alt0; refusing write')
    memory = re.search(r'name="@Internal Flash\s*/0x08000000/([^"]+)"', matches[0])
    segments = memory.group(1).split(',') if memory else []
    advertised_size = 0
    for segment in segments:
        part = re.fullmatch(r'\s*(\d+)\*(\d+)([BKMbkm])([a-gA-G])\s*', segment)
        if part is None: raise RuntimeError('Unrecognized DFU memory descriptor; refusing write')
        advertised_size += int(part[1])*int(part[2])*{'b':1,'k':1024,'m':1048576}[part[3].lower()]
    if advertised_size != flash_size: raise RuntimeError(f'DFU reports {advertised_size} flash bytes, expected {flash_size}. Stop and inspect the exact hardware.')
    if args.output.exists():raise RuntimeError('Refusing to overwrite a readback directory')
    args.output.mkdir(parents=True,mode=0o700)
    (args.output/'dfu-list.txt').write_text(listing)
    def run(index):
        result=subprocess.run(commands[index],capture_output=True,text=True)
        (args.output/f'step-{index+1}.log').write_text(result.stdout+result.stderr)
        if result.returncode:raise RuntimeError(f'DFU step {index+1} failed; stopped, with log in {args.output}. No unprotect/mass-erase fallback is allowed.')
    run(0);run(1)
    before=(args.output/'current-flash-read1.bin').read_bytes()
    if len(before)!=flash_size or before!=(args.output/'current-flash-read2.bin').read_bytes():raise RuntimeError('Current flash readbacks do not match; refusing write')
    if previous and before[:len(previous['payload'])]!=previous['payload']:
        raise RuntimeError('Current flash prefix does not match the previous verified installation; NO firmware written')
    installed_profile_verified=False
    if installed_profile_hash is not None:
        actual_profile_hash=source_original_full_hash if source_receipt is not None else sha(before)
        if actual_profile_hash!=installed_profile_hash:raise RuntimeError('Current flash hash does not match the verified installed firmware profile; NO firmware written')
        installed_profile_verified=True
    known_matches=[]
    source_known_matches=[]
    # A byte-for-byte known main firmware image on the currently working unit is
    # stronger compatibility evidence than product IDs or a generic DFU descriptor.
    for candidate in ('official','upstream','custom'):
        original_choice=args.image
        try:
            args.image=candidate
            known_path,known_payload,known_sha=image_data(args)
        except FileNotFoundError:
            # Downloaded recovery binaries are optional in a source checkout.
            # The explicitly requested image was already required and verified
            # above. Present-but-corrupt candidates still fail closed.
            continue
        finally:args.image=original_choice
        if before[:len(known_payload)]==known_payload:
            known_matches.append({'image':candidate,'path':str(known_path),'image_sha256':known_sha,'payload_bytes':len(known_payload),'payload_sha256':sha(known_payload)})
        if source_receipt is not None and payload[:len(known_payload)]==known_payload:
            source_known_matches.append({'image':candidate,'payload_sha256':sha(known_payload),'payload_bytes':len(known_payload)})
        # The currently installed image may be corrupted during a failed flash. A
    # proven-compatible original readback remains a valid recovery source.
    source_hardware_verified=False
    if source_receipt is not None and source_receipt.get('installed_firmware_and_rom_profile_verified') is True and installed_profile_hash is None:
        prior=source_receipt.get('hardware_evidence') or {}
        prior_hash=validate_installed_profile(prior,args.restore_readback,args.serial,args.device_path,flash_size)
        if prior_hash!=source_original_full_hash or len(payload)>131072:raise RuntimeError('Original ROM-profile restoration hash or 128 KiB write extent mismatch')
        source_hardware_verified=True
    if source_receipt is not None and source_receipt.get('hardware_evidence_verified') is True:
        prior=source_receipt.get('hardware_evidence') or {}
        prior_size={'STM32L432KB':131072,'STM32L432KC':262144}.get(prior.get('mcu'))
        source_hardware_verified=prior.get('reviewed') is True and prior_size==flash_size and prior.get('serial')==args.serial and prior.get('device_path')==args.device_path and prior.get('confirmation_source') in ('physical_chip_marking','rom_device_identification') and bool(prior.get('evidence'))
    if installed_profile_verified and source_receipt is None:
        (args.output/'rom-identification.bin').write_bytes(bytes.fromhex(PROFILE_ROM_HEX))
        hardware={**hardware,'readback_receipt':'receipt.json','rom_identification_file':'rom-identification.bin'}
    receipt={**plan,'installed_firmware_and_rom_profile_verified':installed_profile_verified,'previous_install_verified':previous is not None,'maximum_write_bytes':131072 if installed_profile_verified or previous else None,'mcu_variant':'unspecified' if installed_profile_verified or previous else None,'observed_read_span_bytes':flash_size,'hardware_evidence_verified':hardware_verified,'hardware_evidence':hardware,'restoration_source_known_matches':source_known_matches,'restoration_source_hardware_verified':source_hardware_verified,'current_known_firmware_matches':known_matches,'current_flash_readback_sha256':sha(before),'current_flash_verified_twice':True,'flash_written':False,'flash_verified':False,'leave_requested':False}
    (args.output/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    if args.readback_only:
        print(json.dumps({'readback_verified_twice':True,'sha256':sha(before),'output':str(args.output)},indent=2));return
    if not previous and not installed_profile_verified and not hardware_verified and not known_matches and not source_known_matches and not source_hardware_verified:raise RuntimeError('Current flash does not match any verified compatible K10 Pro ANSI RGB image and no exact MCU evidence was provided. Readback saved; NO firmware written.')
    staged_path=args.output/'validated-image-to-flash.bin'
    staged_path.write_bytes(staged_image);staged_path.chmod(0o400)
    receipt['staged_image_sha256']=sha(staged_image)
    run(2);receipt['flash_written']=True
    (args.output/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    run(3)
    if (args.output/'written-readback.bin').read_bytes()!=payload:raise RuntimeError('Written flash differs from the image; remain in DFU and use verified recovery image')
    receipt['flash_verified']=True
    (args.output/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    run(4);receipt['leave_requested']=True
    (args.output/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps({'flash_written':True,'flash_verified':True,'output':str(args.output)},indent=2))
if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,ValueError,subprocess.CalledProcessError) as e:raise SystemExit(str(e))
