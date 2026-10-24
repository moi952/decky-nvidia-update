"""Decky RPC adapter for the bundled systemd-backed A/B transaction."""
import asyncio
import json
import os
from pathlib import Path
import re
import uuid

import decky

UNIT = 'decky-nvidia-update.service'
CONTROLLER = Path(decky.DECKY_PLUGIN_DIR) / 'bin/standalone/driver.py'
LEGACY_LOG = Path(decky.DECKY_PLUGIN_LOG_DIR) / 'nvidia-update-run.log'
# Decky rotates top-level *.log files on backend startup.
LOG = LEGACY_LOG.parent / 'operations' / LEGACY_LOG.name
PHASE_STEPS = dict(preflight=1, custom=2, clone=3, payload=4, write=5, finalize=6, verify=7)
LOG_MAX_BYTES = 512 * 1024
LOG_MAX_LINES = 2000


def preserve_legacy_log():
    if LOG.exists() or not LEGACY_LOG.is_file() or LEGACY_LOG.is_symlink():
        return
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        # Keep the inode: an older systemd worker can still be appending through
        # its open descriptor. A copy would miss those later lines.
        os.link(LEGACY_LOG, LOG)
    except FileExistsError:
        pass  # Never replace a newer operation log.
    except OSError as error:
        decky.logger.error(f'Could not preserve the legacy operation log: {error}')


def current_log_path():
    return LEGACY_LOG if not LOG.exists() and LEGACY_LOG.exists() else LOG


def read_update_log():
    """Read the saved operation log without loading an unbounded file into Decky."""
    preserve_legacy_log()
    path = current_log_path()
    try:
        with path.open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size - LOG_MAX_BYTES)
            stream.seek(start)
            data = stream.read(LOG_MAX_BYTES)
        if start:
            # The byte window may start inside a UTF-8 character or a line.
            data = data.partition(b'\n')[2]
        lines = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', data.decode('utf-8', errors='replace')).splitlines()
        return dict(lines=lines[-LOG_MAX_LINES:], truncated=bool(start or len(lines) > LOG_MAX_LINES),
                    path=str(path), error=None)
    except FileNotFoundError:
        return dict(lines=[], truncated=False, path=str(path), error=None)
    except OSError as error:
        return dict(lines=[], truncated=False, path=str(path), error=str(error))


def update_progress(state, lines):
    """Worker counters describe a subtask, never completion of the whole update."""
    for line in lines:
        phase = re.match(r'^\[phase\] (\w+) (.+)$', line)
        if phase and phase[1] in PHASE_STEPS:
            step = PHASE_STEPS[phase[1]]
            if step >= state.get('step', 0):
                state.update(step=step, total_steps=8, phase=phase[1], message=phase[2])
        elif re.match(r'^\[\d+/\d+\]', line):
            # Keep useful worker details, including during a job started by an
            # older plugin. Only a matching completed transaction can reach 8/8.
            state['message'] = re.sub(r'^\[\d+/\d+\]\s*', '', line)


def launch_result(started=False, code=None, message=None):
    return dict(started=started, code=code, message=message)


def operation_error(lines):
    for line in reversed(lines):
        if (line.startswith('rsync: [sender] ') and
                re.search(r': (?:Bad message \(74\)|Input/output error \(5\))$', line)):
            return ('The current system contains unreadable files. The driver preparation stopped. '
                    'Check and repair the affected filesystem before retrying. ' + line)
    failures = [line[7:] for line in lines if line.startswith('[fail] ')]
    return failures[-1] if failures else 'Preparation did not complete; inspect the log.'


async def command(*args):
    # Avoid Decky's library path for all host utilities.
    import os
    env = os.environ.copy()
    env.pop('LD_LIBRARY_PATH', None)
    env.pop('LD_PRELOAD', None)
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.PIPE, env=env)
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors='replace'), err.decode(errors='replace')


async def controller(action):
    rc, out, err = await command('/usr/bin/python3', '-I', str(CONTROLLER), action)
    if rc:
        raise RuntimeError(err.strip() or out.strip() or 'Controller failed')
    return json.loads(out)


async def active():
    rc, out, err = await command('systemctl', 'show', UNIT, '--property=ActiveState', '--value')
    if rc and out.strip() != 'inactive':
        raise RuntimeError(err.strip() or 'Could not read the update service status')
    return out.strip() in ('active', 'activating', 'deactivating', 'reloading')


class StandaloneMixin:
    async def get_update_log(self):
        return await asyncio.to_thread(read_update_log)

    async def get_rootfs_space_info(self):
        try:
            info = await controller('preflight')
            return dict(available=True, error='', total_mb=info['target_bytes'] // 1048576,
                        avail_mb=info['available_bytes'] // 1048576,
                        required_mb=info['required_bytes'] // 1048576,
                        undersized=not info['enough_space'])
        except Exception as error:
            return dict(available=False, error=str(error), total_mb=0, avail_mb=0,
                        required_mb=0, undersized=False)

    async def get_transaction_status(self):
        try:
            return await controller('status')
        except Exception as error:
            return dict(status='unknown', error=str(error))

    async def start_update(self, driver_spec, force=False, custom=False):
        # Retain the boolean RPC for older frontends and external callers.
        return (await self.request_update(driver_spec, force, custom))['started']

    async def request_update(self, driver_spec, force=False, custom=False):
        if not isinstance(driver_spec, str) or (driver_spec != 'latest' and
                not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*(?:-[0-9]+(?:\.[0-9]+)*)?', driver_spec)):
            return launch_result(code='invalid_version', message='Select a valid NVIDIA driver version.')
        if custom and not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+', driver_spec):
            return launch_result(code='invalid_version', message='Select an exact version for a custom build.')
        return await self._start_standalone('install-custom' if custom else 'install', driver_spec)

    async def rollback_driver(self):
        return (await self.request_rollback())['started']

    async def request_rollback(self):
        return await self._start_standalone('rollback')

    async def _start_standalone(self, action, version=None):
        async with self._lock:
            try:
                busy = self._state['running'] or await active()
            except Exception as error:
                return launch_result(code='status_unavailable', message=str(error))
            if busy:
                return launch_result(code='already_running', message='A driver operation is already running.')
            if action in ('install', 'install-custom') and version != 'latest':
                try:
                    installed = await self.get_current_version()
                except Exception as error:
                    return launch_result(code='status_unavailable', message=str(error))
                if installed and (version == installed or
                                  ('-' not in version and version == installed.rsplit('-', 1)[0])):
                    return launch_result(
                        code='already_installed',
                        message=f'NVIDIA {installed} is already installed. Reinstalling the same version is not supported. '
                                'No update was started and your system has not been changed.')
            operation = uuid.uuid4().hex
            try:
                LOG.parent.mkdir(parents=True, exist_ok=True)
                LOG.write_text('[action] ' + action + '\n[operation] ' + operation + '\n' +
                               ('[driver] ' + version + '\n' if version else ''))
            except OSError as error:
                return launch_result(code='launch_failed', message='Could not create the operation log: ' + str(error))
            args = ['/usr/bin/python3', '-I', '-u', str(CONTROLLER), '--operation-id', operation, action]
            if version:
                args.append(version)
            # systemd owns the process and log descriptor across Decky reloads.
            self._state.update(running=True, done=False, success=None, error=None,
                               error_code=None, step=0, total_steps=8, phase=None, log=[], pending_reboot=False,
                               driver_spec=version, action=action, operation_id=operation, message='Starting driver operation')
            try:
                rc, out, err = await command('systemd-run', '--unit=' + UNIT, '--collect',
                                            '--service-type=exec', '--property=StandardOutput=append:' + str(LOG),
                                            '--property=StandardError=append:' + str(LOG), '--', *args)
            except Exception as error:
                # Dispatch may already have reached systemd. Reconcile status;
                # never stop the service or tell the user to blindly start again.
                self._task = asyncio.create_task(self._monitor_standalone(action))
                return launch_result(code='status_unavailable', message=str(error))
            if rc:
                error = (err or out).strip() or 'The driver service could not be started.'
                self._state.update(running=False, done=True, success=False, error=error, error_code='launch_failed')
                with LOG.open('a') as stream:
                    stream.write('[fail] ' + error.replace('\n', ' ') + '\n')
                await decky.emit('nvidia_update_progress', dict(self._state))
                return launch_result(code='launch_failed', message=error)
            self._task = asyncio.create_task(self._monitor_standalone(action))
            return launch_result(started=True)

    async def _monitor_standalone(self, action='install'):
        while True:
            try:
                running = await active()
                path = current_log_path()
                lines = path.read_text(errors='replace').splitlines() if path.exists() else []
                lines = [re.sub(r'\x1b\[[0-9;]*m', '', line) for line in lines]
                self._state['log'] = lines[-200:]
                update_progress(self._state, lines)
                self._state['running'] = running
                self._state.update(error=None, error_code=None)
                if not running:
                    transaction = await controller('status')
                    wanted = 'rollback-selected' if action == 'rollback' else 'ready'
                    failures = [line[7:] for line in lines if line.startswith('[fail] ')]
                    ok = (transaction.get('status') == wanted and not failures and
                          transaction.get('operation_id') == self._state.get('operation_id') and
                          bool(self._state.get('operation_id')))
                    self._state.update(done=True, success=ok,
                                       pending_reboot=bool(transaction.get('pending_reboot')) if ok else False,
                                       error=None if ok else operation_error(lines))
                    if ok:
                        self._state.update(step=8, total_steps=8,
                                           driver_spec=transaction.get('version', self._state.get('driver_spec')))
                await decky.emit('nvidia_update_progress', dict(self._state))
                if not running:
                    break
                await asyncio.sleep(1)
            except Exception as error:
                # Losing the observer does not mean the systemd job stopped.
                self._state.update(running=True, done=False, success=None,
                                   error=str(error), error_code='status_unavailable',
                                   message='Reconnecting to the driver operation')
                try:
                    await decky.emit('nvidia_update_progress', dict(self._state))
                except Exception:
                    pass
                await asyncio.sleep(2)

    async def _main(self):
        decky.logger.info('Standalone NVIDIA A/B updater loaded')
        preserve_legacy_log()
        if await active():
            self._state['running'] = True
            path = current_log_path()
            action = 'rollback' if path.exists() and path.read_text(errors='replace').startswith('[action] rollback') else 'install'
            self._state['action'] = action
            lines = path.read_text(errors='replace').splitlines() if path.exists() else []
            self._state['operation_id'] = next((line.removeprefix('[operation] ') for line in lines if line.startswith('[operation] ')), None)
            self._state['driver_spec'] = next((line.removeprefix('[driver] ') for line in lines if line.startswith('[driver] ')), None)
            self._task = asyncio.create_task(self._monitor_standalone(action))
        else:
            transaction = await self.get_transaction_status()
            if transaction.get('pending_reboot'):
                action = 'rollback' if transaction.get('status') == 'rollback-selected' else 'install'
                self._state.update(running=False, done=True, success=True, action=action,
                                   pending_reboot=True, operation_id=transaction.get('operation_id'),
                                   step=8, total_steps=8, error=None, error_code=None,
                                   driver_spec=transaction.get('version'))
            try:
                result = await controller('cache-migrate')
                decky.logger.info(f'Legacy NVIDIA cache migration: {result}')
            except Exception as error:
                decky.logger.error(f'Legacy NVIDIA cache migration deferred: {error}')

        # Keep the toolkit's normal plugin updater, independently of the
        # NVIDIA transaction. A failed release check must not stop job recovery.
        try:
            update_info = await self.check_plugin_update_on_load()
            if update_info:
                await decky.emit('plugin_update_available', update_info)
        except Exception as error:
            decky.logger.error(f'Plugin update check failed: {error}')

    async def list_custom_builds(self):
        return await controller('custom-list')

    async def start_custom_build(self, *args, **kwargs):
        # UI uses start_update(custom=True): one durable build + install job.
        return False

    async def delete_custom_build(self, *args, **kwargs):
        # Collection is controller-owned and checks both slots under the locks.
        return False
