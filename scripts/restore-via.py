#!/usr/bin/env python3
"""Restore an explicitly reviewed K10 Pro VIA keycode profile; plan-only by default."""
from pathlib import Path
import argparse, importlib.util, json, os, sys
spec=importlib.util.spec_from_file_location('via_backup',Path(__file__).with_name('backup-via.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
Helper=module.Helper
ROOT=Path(__file__).resolve().parent.parent

KEYMAP_PROFILE='official-to-pinned-618127a'
PINNED_KEYMAP_PROFILE='repaired-pinned-618127a'
ORIGINAL_KEYMAP_SHA='566a9452465546a3e3d19a0e9ab286b456bc671ba700cf084d9c13ce600aefcc'
REPAIRED_KEYMAP_SHA='571784dd6b9ad2061b7144d3ef13a241fc718062b6e6c9024a7b8fd5b77e6950'
# Reviewed builds share the exact pinned 618127a keycode enum. A matching VIA
# protocol or a matching local filename is never sufficient to add a build.
TARGET_IMAGE_SHAS=(
    '6627ff5992ac8d3bf99689fc97b13562968914d129851328b64c146b3dcfdbf5',
    'ac17f3e789fe709943eb5c66303c30e84b4a721d5223989f63412d0e31e5913d',
)
CUSTOM_MIGRATION={0x7e00:0x7e02,0x7e01:0x7e03,0x7e02:0x7e04,0x7e03:0x7e05,0x7e04:0x7e00,0x7e05:0x7e01}
CUSTOM_NAMES={0x7e00:'Left Option',0x7e01:'Right Option',0x7e02:'Left Command',0x7e03:'Right Command',0x7e04:'Mission Control',0x7e05:'Launchpad'}
def prepare_keymap(keymap, profile=None, writing=True):
    if len(keymap)%2:raise RuntimeError('Odd keymap byte count')
    codes=[int.from_bytes(keymap[i:i+2],'big') for i in range(0,len(keymap),2)]
    if profile is None:
        if writing and any(0x7e00<=code<=0x7e3f for code in codes):
            raise RuntimeError('Custom keycodes require an explicit --keymap-profile; VIA protocol equality does not guarantee equal key meanings')
        return keymap,None
    if profile==PINNED_KEYMAP_PROFILE:
        if len(keymap)!=1008 or module.digest(keymap)!=REPAIRED_KEYMAP_SHA:
            raise RuntimeError('Pinned preservation profile applies only to the audited repaired K10 Pro keymap; source hash mismatch')
        return keymap,{'profile':profile,'source_keymap_sha256':module.digest(keymap),'target_keymap_sha256':module.digest(keymap),'allowed_target_firmware_sha256':list(TARGET_IMAGE_SHAS),'changed_keys':[],'unchanged_keys':504,'basis':'Exact audited repaired keymap and reviewed images with unchanged upstream 618127a custom keycode enum'}
    if profile!=KEYMAP_PROFILE or len(keymap)!=1008 or module.digest(keymap)!=ORIGINAL_KEYMAP_SHA:
        raise RuntimeError('Migration profile applies only to the audited original K10 Pro keymap; source hash mismatch')
    target=bytearray(keymap);changes=[]
    for index,code in enumerate(codes):
        if code not in CUSTOM_MIGRATION:continue
        mapped=CUSTOM_MIGRATION[code];offset=index*2
        target[offset:offset+2]=mapped.to_bytes(2,'big')
        changes.append({'layer':index//126,'row':index%126//21,'column':index%21,'offset':offset,'meaning':CUSTOM_NAMES[code],'source':f'{code:04x}','target':f'{mapped:04x}'})
    if len(changes)!=6:raise RuntimeError('Audited migration must modify exactly six keys')
    audit={'profile':profile,'source_keymap_sha256':module.digest(keymap),'target_keymap_sha256':module.digest(target),'allowed_target_firmware_sha256':list(TARGET_IMAGE_SHAS),'changed_keys':changes,'unchanged_keys':len(codes)-len(changes),'total_distinct_source_codes':len(set(codes)),'basis':'Official k10_pro_us_rgb_v1.0.json customKeycodes order plus original binary dispatch and pinned 618127a k10_pro.h; VIA version alone is insufficient'}
    return bytes(target),audit

def verify_migration_precondition(current, original, target):
    if len(current)!=len(original) or any(current[i:i+2] not in (original[i:i+2],target[i:i+2]) for i in range(0,len(original),2)):
        raise RuntimeError('Current keymap differs from the audited original/migration; no changes made')

def verify_profile_target(backup,current,query,image):
    if (query.get('firmware_verified') is not True or query.get('version')!=1
        or query.get('theme_protocol',0) not in (0,1)):
        raise RuntimeError('Keymap profile requires a reviewed Orca OL v1 firmware protocol; no changes made')
    image_sha=module.digest(image)
    if image_sha not in TARGET_IMAGE_SHAS:
        raise RuntimeError('Profile target firmware differs from the reviewed images; no changes made')
    for state in (backup,current):
        if (state.get('via_protocol')!=12 or state.get('layers')!=4
            or state.get('rows')!=6 or state.get('columns')!=21
            or len(bytes.fromhex(state['keymap_be_hex']))!=1008):
            raise RuntimeError('Keymap profile requires the reviewed 4-layer 6x21 matrix; no changes made')
    return image_sha

RESTORED_FIELDS=('keymap_be_hex','macro_buffer_hex','layout_options_be_hex','lighting')
def expected_fields(current, backup, keymap, macros, scope):
    """Non-target fields must retain their observed values, including later user edits."""
    expected={field:current[field] for field in RESTORED_FIELDS}
    if scope!='lighting_only':expected['keymap_be_hex']=keymap.hex()
    if scope=='all_via_fields':
        expected['macro_buffer_hex']=macros.hex()
        expected['layout_options_be_hex']=backup['layout_options_be_hex']
    if scope!='keymap_only' and backup['lighting'] is not None:
        if current['lighting'] is None:raise RuntimeError('Target lighting channel unavailable; no changes made')
        expected['lighting']={'channel':current['lighting']['channel'],'fields':backup['lighting']['fields']}
    return expected

def restore_lighting(write, read, channel, fields):
    """Correct only the observed legacy RGB-matrix /256 rounding; require exact readback."""
    write([0x07,channel,4,*bytes.fromhex(fields['4'])])
    desired=int(fields['1'],16)
    write([0x07,channel,1,desired])
    observed=read(0x08,channel,1)[3]
    correction=None
    if observed!=desired:
        # Pinned QMK via.c uses scale8(v,255) -> floor(v*255/256),
        # while its getter returns the stored byte unchanged. One inverse step
        # restores every representable value; 255 has no exact preimage.
        if channel!=3 or not 0<desired<255 or observed!=desired-1:
            raise RuntimeError(f'RGB brightness exact restoration failed: requested {desired}, read {observed}; no approximate result accepted')
        write([0x07,channel,1,desired+1])
        corrected=read(0x08,channel,1)[3]
        if corrected!=desired:raise RuntimeError(f'RGB brightness correction failed: requested {desired}, read {corrected}; no approximate result accepted')
        correction={'requested_readback':desired,'initial_write':desired,'initial_readback':observed,'corrected_write':desired+1,'verified_readback':corrected,'reason':'Pinned QMK legacy scale8(input,255) divides by 256'}
    write([0x07,channel,3,*bytes.fromhex(fields['3'])])
    # Brightness can toggle the enabled state, so restore the effect last.
    write([0x07,channel,2,*bytes.fromhex(fields['2'])])
    write([0x09,channel])
    return correction

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup',type=Path,required=True)
    parser.add_argument('--helper',type=Path,default=ROOT/'build/orca-keychron-helper')
    parser.add_argument('--registry-id')
    parser.add_argument('--keymap-profile',choices=[KEYMAP_PROFILE,PINNED_KEYMAP_PROFILE],help='Exact audited map only: migrate official key meanings, or preserve an already-repaired pinned map. User-modified maps require a new review.')
    parser.add_argument('--keymap-only',action='store_true',help='Only restore keycodes, preserving macros, layout and lighting')
    parser.add_argument('--lighting-only',action='store_true',help='Restore saved lighting; verify the current keymap, macros and layout remain unchanged')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--receipt',type=Path)
    args=parser.parse_args();os.umask(0o077)
    backup=json.loads(args.backup.read_text())
    if backup.get('schema')!='orca-k10-pro-via-backup-v1' or backup.get('verified_twice') is not True or backup.get('device',{}).get('vendor_id')!=0x3434 or backup.get('device',{}).get('product_id')!=0x02a0:raise RuntimeError('Verified ANSI RGB backup required')
    if backup.get('via_protocol')!=12:raise RuntimeError('Only reviewed protocol 12 to 12 restoration is enabled')
    if args.lighting_only and args.keymap_only:raise RuntimeError('Choose at most one restoration scope')
    keymap=bytes.fromhex(backup['keymap_be_hex']);macros=bytes.fromhex(backup['macro_buffer_hex'])
    if module.digest(keymap)!=backup['keymap_sha256'] or module.digest(macros)!=backup['macro_sha256']:raise RuntimeError('Backup checksum mismatch')
    original_keymap=keymap
    keymap,migration=prepare_keymap(keymap,args.keymap_profile,writing=not args.lighting_only)
    plan={'keymap_migration':migration,'action':'restore_via' if args.execute else 'plan_only','target':'Keychron K10 Pro ANSI RGB','keymap_bytes':len(keymap),'macro_bytes_saved':len(macros),'lighting':backup['lighting'] is not None,'restoration_scope':'lighting_only' if args.lighting_only else ('keymap_only' if args.keymap_only else 'all_via_fields'),'will_reset_or_enter_bootloader':False,'backup':str(args.backup)}
    print(json.dumps(plan,indent=2),flush=True)
    if not args.execute:return
    destination=args.receipt or args.backup.with_name('restore-receipt.json')
    if destination.exists():raise RuntimeError('Receipt path already exists; choose --receipt before making changes')
    helper=Helper(args.helper.resolve())
    try:
        candidates=[d for d in helper.request('devices')['devices'] if d.get('vendor_id')==0x3434 and d.get('product_id')==0x02a0 and d.get('transport','').lower()=='usb' and d.get('usage_page')==0xff60 and (args.registry_id is None or d.get('registry_id')==args.registry_id)]
        if len(candidates)!=1:raise RuntimeError('Exactly one selected ANSI RGB USB Raw HID interface required')
        helper.request('open',product_id=0x02a0,transport='USB',registry_id=candidates[0]['registry_id'])
        current=module.capture(helper)
        if migration:
            query=helper.request('query')
            image=ROOT/'firmware/build/k10_pro_ansi_rgb_orca_led.bin'
            migration['target_firmware_sha256']=verify_profile_target(backup,current,query,image.read_bytes())
            if args.keymap_profile==KEYMAP_PROFILE:verify_migration_precondition(bytes.fromhex(current['keymap_be_hex']),original_keymap,keymap)
        if current['via_protocol']!=12 or current['layers']!=backup['layers'] or len(bytes.fromhex(current['keymap_be_hex']))!=len(keymap):raise RuntimeError('Target protocol or matrix differs; no changes made')
        size=current['macro_size']
        if plan['restoration_scope']=='all_via_fields':
            if current['macro_count']!=backup['macro_count']:raise RuntimeError('Macro count differs; no changes made')
            if any(macros[size:]):raise RuntimeError('Non-empty macros exceed target capacity; no changes made')
        desired_macros=macros[:size].ljust(size,b'\0')
        expected=expected_fields(current,backup,keymap,desired_macros,plan['restoration_scope'])
        def write(prefix):
            packet=list(prefix)+[0]*(32-len(prefix))
            result=helper.request('via-write',bytes=packet)
            if result.get('bytes')!=packet:raise RuntimeError('VIA write echo mismatch; verify backup before continuing')
        def write_buffer(command,data):
            for off in range(0,len(data),28):
                part=data[off:off+28]
                write([command,off>>8,off&255,len(part),*part])
        if not args.lighting_only:
            if args.keymap_only and args.keymap_profile==KEYMAP_PROFILE:
                for change in migration['changed_keys']:
                    off=change['offset'];write([0x13,off>>8,off&255,2,*keymap[off:off+2]])
            else:write_buffer(0x13,keymap)
            if not args.keymap_only:
                write_buffer(0x0f,desired_macros)
                write([0x03,0x02,*bytes.fromhex(backup['layout_options_be_hex'])])
        brightness_correction=None
        if backup['lighting'] is not None and not args.keymap_only:
            channel=current['lighting']['channel']
            brightness_correction=restore_lighting(write,helper.read,channel,backup['lighting']['fields'])
        after=module.capture(helper)
        if migration and after!=module.capture(helper):raise RuntimeError('Two independent post-migration reads differ')
        field_checks={field:after[field]==value for field,value in expected.items()}
        matched=all(field_checks.values())
        receipt={**plan,'verified':matched,'device':candidates[0],'keymap_sha256':after['keymap_sha256'],'macro_sha256':after['macro_sha256'],'macro_bytes_restored':size if plan['restoration_scope']=='all_via_fields' else 0,'lighting_verified':field_checks['lighting'],'keymap_verified':field_checks['keymap_be_hex'],'field_verification':field_checks,'keymap_read_twice':bool(migration),'brightness_correction':brightness_correction,'read_only':False}
        destination.write_text(json.dumps(receipt,indent=2)+'\n')
        if not matched:raise RuntimeError('Readback mismatch after restore; inspect receipt')
        print(json.dumps({'restored_and_verified':True,'receipt':str(destination),'macro_capacity_adjusted_with_zero_tail_only':plan['restoration_scope']=='all_via_fields' and size!=len(macros)},indent=2))
    finally:helper.close()
if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,ValueError) as e:raise SystemExit(str(e))
