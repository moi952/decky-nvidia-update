#!/usr/bin/python3 -I
"""Temporary compatibility for Valve's obsolete BLAKE2b hook module name."""
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile


def render_hook(text):
    # Only change the known literal array member; unfamiliar hooks run normally.
    arrays = list(re.finditer(r'(?ms)^[ \t]*declare -a DECK_MODULES=\(\n(.*?)^[ \t]*\)', text))
    if len(arrays) != 1:
        return text
    array = arrays[0]
    body = array.group(1)
    old = list(re.finditer(r'(?m)^([ \t]*)blake2b_generic[ \t]*$', body))
    if len(old) != 1:
        return text
    match = old[0]
    # Evaluate against EACH preset's kernel and module root, never the host's
    # uname. Missing/uncertain support retains the original failing request.
    replacement = match[1] + '''"$(
            if modinfo -b "${_optmoduleroot:-/}" -k "$KERNELVERSION" blake2b_generic >/dev/null 2>&1; then
                printf '%s' blake2b_generic
            elif grep -Fxq 'kernel/crypto/blake2b.ko' "${_optmoduleroot:-/}/usr/lib/modules/$KERNELVERSION/modules.builtin" &&
                 grep -Fxq 'kernel/lib/crypto/libblake2b.ko' "${_optmoduleroot:-/}/usr/lib/modules/$KERNELVERSION/modules.builtin" &&
                 [[ $(modinfo -b "${_optmoduleroot:-/}" -k "$KERNELVERSION" -F filename blake2b 2>/dev/null) == '(builtin)' ]]; then
                printf '%s' blake2b
            else
                printf '%s' blake2b_generic
            fi
        )"'''
    changed = body[:match.start()] + replacement + body[match.end():]
    return text[:array.start(1)] + changed + text[array.end(1):]


def run_chroot(root, command):
    root = Path(root).resolve(strict=True)
    if root == Path('/') or not root.is_dir() or not command:
        raise ValueError('A separate target root and command are required')
    env = os.environ.copy()
    search = env.get('MKINITCPIO_INSTALL','/etc/initcpio/install:/usr/lib/initcpio/install')
    hook = None
    for directory in search.split(':'):
        if not directory.startswith('/') or '..' in Path(directory).parts:
            raise ValueError('Unsupported initcpio hook search path')
        candidate = root / directory.lstrip('/') / 'steam-deck'
        if candidate.exists():
            if not candidate.is_file() or not candidate.resolve().is_relative_to(root):
                raise ValueError('Unsafe target initcpio hook')
            hook = candidate
            break
    if hook is None:
        return subprocess.call(['chroot',str(root),*command],env=env)
    original = hook.read_text()
    changed = render_hook(original)
    if changed == original:
        return subprocess.call(['chroot',str(root),*command],env=env)
    tmp = root/'tmp'
    if tmp.is_symlink() or not tmp.is_dir():
        raise ValueError('Unsafe target temporary directory')
    with tempfile.TemporaryDirectory(prefix='decky-initcpio-',dir=tmp) as private:
        install = Path(private)/'install'
        install.mkdir()
        (install/'steam-deck').write_text(changed)
        # Only override install-hook search for this child process. Retain
        # normal user and distro hooks, including their original precedence.
        env['MKINITCPIO_INSTALL'] = '/' + install.relative_to(root).as_posix() + ':' + search
        return subprocess.call(['chroot',str(root),*command],env=env)


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('Run as root')
    raise SystemExit(run_chroot(sys.argv[1],sys.argv[2:]))
