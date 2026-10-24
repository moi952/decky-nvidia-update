import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('initcpio_compat',Path(__file__).resolve().parents[1]/'bin/standalone/initcpio_compat.py')
compat=importlib.util.module_from_spec(spec);spec.loader.exec_module(compat)
HOOK='''#!/bin/bash
build() {
    declare -a DECK_MODULES=(
        blake2b_generic
        btrfs
        other_required
    )
    printf '%s\\n' "${DECK_MODULES[@]}"
}
'''


class HookCompatibility(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'tmp').mkdir()
        self.hook=self.root/'usr/lib/initcpio/install/steam-deck'
        self.hook.parent.mkdir(parents=True);self.hook.write_text(HOOK)

    @unittest.skipUnless(shutil.which('bash'),'bash required')
    def test_kernel_selection_preserves_old_and_fails_closed(self):
        script=self.root/'rendered.sh';script.write_text(compat.render_hook(HOOK))
        builtins=self.root/'usr/lib/modules/target-kernel/modules.builtin'
        builtins.parent.mkdir(parents=True)
        mock='''modinfo() {
            [[ "$1" == -b && "$2" == "$ROOT" && "$3" == -k && "$4" == target-kernel ]] || exit 99
            if [[ "${@: -1}" == blake2b_generic ]]; then [[ "$OLD" == yes ]];
            elif [[ "$NEW" == builtin ]]; then printf '(builtin)\\n'; else return 1; fi
        }
        _optmoduleroot=$ROOT; KERNELVERSION=target-kernel; source "$HOOKFILE"; build
        '''
        for old,new,entries,want in [
            ('yes','builtin','both','blake2b_generic'),
            ('no','builtin','both','blake2b'),
            ('no','missing','both','blake2b_generic'),
            ('no','builtin','crypto-only','blake2b_generic'),
            ('no','builtin','none','blake2b_generic')]:
            with self.subTest(old=old,new=new,entries=entries):
                builtins.write_text(('kernel/crypto/blake2b.ko\n' if entries!='none' else '')+
                                    ('kernel/lib/crypto/libblake2b.ko\n' if entries=='both' else ''))
                env=dict(os.environ,ROOT=str(self.root),OLD=old,NEW=new,HOOKFILE=str(script))
                p=subprocess.run(['bash','-eu','-c',mock],env=env,capture_output=True,text=True)
                self.assertEqual(p.returncode,0,p.stderr)
                self.assertEqual(p.stdout.splitlines(),[want,'btrfs','other_required'])

    def test_fixed_or_unfamiliar_hook_is_unchanged(self):
        for text in (HOOK.replace('blake2b_generic','blake2b'),HOOK.replace('DECK_MODULES','OTHER'),
                     HOOK.replace('        blake2b_generic','        blake2b_generic\n        blake2b_generic')):
            self.assertEqual(compat.render_hook(text),text)

    def test_override_is_scoped_and_error_is_preserved(self):
        directories=[]
        original=self.hook.read_bytes()
        def call(args,env):
            self.assertEqual(args,['chroot',str(self.root),'mkinitcpio','-P'])
            directory=env['MKINITCPIO_INSTALL'].split(':')[0]
            path=self.root/directory.lstrip('/')
            directories.append(path.parent)
            self.assertIn('kernel/crypto/blake2b.ko',(path/'steam-deck').read_text())
            self.assertTrue(env['MKINITCPIO_INSTALL'].endswith(':/etc/initcpio/install:/usr/lib/initcpio/install'))
            return 7
        with patch.dict(os.environ,{},clear=True),patch.object(compat.subprocess,'call',side_effect=call):
            self.assertEqual(compat.run_chroot(self.root,['mkinitcpio','-P']),7)
        self.assertEqual(self.hook.read_bytes(),original)
        self.assertTrue(directories)
        self.assertFalse(directories[0].exists())

    def test_user_hook_precedence_and_fixed_hook_no_override(self):
        user=self.root/'etc/initcpio/install/steam-deck'
        user.parent.mkdir(parents=True);user.write_text('custom fixed hook')
        with patch.dict(os.environ,{},clear=True),patch.object(compat.subprocess,'call',return_value=0) as call:
            compat.run_chroot(self.root,['mkinitcpio','-P'])
            self.assertNotIn('MKINITCPIO_INSTALL',call.call_args.kwargs['env'])

    def test_custom_search_path_is_respected(self):
        custom=self.root/'custom/install/steam-deck'
        custom.parent.mkdir(parents=True);custom.write_text('custom unchanged hook')
        with patch.dict(os.environ,{'MKINITCPIO_INSTALL':'/custom/install'},clear=True),patch.object(compat.subprocess,'call',return_value=0) as call:
            compat.run_chroot(self.root,['mkinitcpio','-P'])
            self.assertEqual(call.call_args.kwargs['env']['MKINITCPIO_INSTALL'],'/custom/install')

    def test_active_root_and_unsafe_tmp_refused(self):
        with self.assertRaises(ValueError):compat.run_chroot('/', ['true'])
        (self.root/'tmp').rmdir();(self.root/'tmp').symlink_to('/tmp')
        with patch.dict(os.environ,{},clear=True),self.assertRaises(ValueError):compat.run_chroot(self.root,['true'])


if __name__=='__main__':unittest.main()
