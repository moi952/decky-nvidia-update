import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
decky = types.ModuleType('decky')
decky.DECKY_PLUGIN_DIR = str(ROOT)
decky.DECKY_PLUGIN_LOG_DIR = str(ROOT / '.verify-work')
decky.DECKY_PLUGIN_SETTINGS_DIR = str(ROOT / '.verify-work')
decky.logger = types.SimpleNamespace(info=lambda *a: None, error=lambda *a: None)
decky.emit = AsyncMock()
sys.modules['decky'] = decky
spec = importlib.util.spec_from_file_location('standalone', ROOT / 'py_modules/nvidia_update/standalone.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Adapter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = m.StandaloneMixin()
        self.adapter._lock = asyncio.Lock()
        self.adapter._state = dict(running=False, driver_spec=None)
        self.adapter.check_plugin_update_on_load = AsyncMock(return_value=None)
        self.adapter.get_current_version = AsyncMock(return_value='')
        # These are isolated adapter tests, including startup cache migration.
        controller = patch.object(m, 'controller', new_callable=AsyncMock, return_value={})
        controller.start()
        self.addCleanup(controller.stop)

    async def test_rejects_invalid_rpc_version_without_launch(self):
        with patch.object(m, 'command', new_callable=AsyncMock) as command:
            self.assertFalse(await self.adapter.start_update('../x'))
            self.assertFalse(await self.adapter.start_update('610.1\n'))
            command.assert_not_called()

    async def test_service_launch_has_no_pipe_to_decky(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LOG', Path(tmp) / 'run.log'), patch.object(m, 'active', new_callable=AsyncMock, return_value=False), patch.object(m, 'command', new_callable=AsyncMock, return_value=(0, '', '')) as command, patch.object(self.adapter, '_monitor_standalone', new_callable=AsyncMock):
            self.assertTrue(await self.adapter.start_update('610.57.04-1'))
            await self.adapter._task
            args = command.call_args.args
            self.assertEqual(args[0], 'systemd-run')
            self.assertNotIn('--pipe', args)
            self.assertTrue(any(a.startswith('--property=StandardOutput=append:') for a in args))
            self.assertEqual(args[-2:], ('install', '610.57.04-1'))
            self.assertIn('[driver] 610.57.04-1', m.LOG.read_text())

    async def test_failure_does_not_reuse_old_ready_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'run.log'
            log.write_text('[fail] Not enough space\n')
            self.adapter._state['driver_spec'] = '610.57.04'
            with patch.object(m, 'LOG', log), patch.object(m, 'active', new_callable=AsyncMock, return_value=False), patch.object(m, 'controller', new_callable=AsyncMock, return_value={'status': 'ready', 'version': 'old'}):
                await self.adapter._monitor_standalone()
                self.assertFalse(self.adapter._state['success'])
                self.assertEqual(self.adapter._state['error'], 'Not enough space')
                self.assertEqual(self.adapter._state['driver_spec'], '610.57.04')

    async def test_reload_keeps_the_requested_version_for_the_running_job(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LOG', Path(tmp) / 'run.log'), patch.object(m, 'active', AsyncMock(return_value=True)), patch.object(self.adapter, '_monitor_standalone', AsyncMock()):
            m.LOG.write_text('[action] install\n[operation] active-job\n[driver] 610.57.04\n')
            await self.adapter._main()
            await self.adapter._task
        self.assertEqual(self.adapter._state['driver_spec'], '610.57.04')

    async def test_custom_selection_stays_in_one_durable_service(self):
        with patch.object(self.adapter, '_start_standalone', new_callable=AsyncMock, return_value=m.launch_result(started=True)) as start:
            self.assertTrue(await self.adapter.start_update('615.71.09', custom=True))
            start.assert_awaited_once_with('install-custom', '615.71.09')
            self.assertFalse(await self.adapter.start_update('latest', custom=True))
            self.assertFalse(await self.adapter.start_update('615.71.09-1', custom=True))

    async def test_custom_packages_cannot_be_deleted_through_rpc(self):
        self.assertFalse(await self.adapter.delete_custom_build('615.71.09'))

    async def test_missing_operation_receipt_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'run.log'
            log.write_text('[action] install\n')
            self.adapter._state['operation_id'] = 'new-operation'
            with patch.object(m, 'LOG', log), patch.object(m, 'active', new_callable=AsyncMock, return_value=False), patch.object(m, 'controller', new_callable=AsyncMock, return_value={'status': 'ready', 'operation_id': 'previous-operation'}):
                await self.adapter._monitor_standalone()
                self.assertFalse(self.adapter._state['success'])

    async def test_preflight_failure_is_explicit(self):
        with patch.object(m, 'controller', new_callable=AsyncMock, side_effect=RuntimeError('Unsupported layout')):
            result = await self.adapter.get_rootfs_space_info()
            self.assertFalse(result['available'])
            self.assertEqual(result['error'], 'Unsupported layout')

    async def test_plugin_exports_standalone_rpc_methods(self):
        sys.path.insert(0, str(ROOT / 'py_modules'))
        try:
            from nvidia_update import Plugin
            self.assertTrue(Plugin.start_update.__module__.endswith('.standalone'))
            self.assertTrue(Plugin.get_rootfs_space_info.__module__.endswith('.standalone'))
            self.assertTrue(Plugin._main.__module__.endswith('.standalone'))
            self.assertEqual(Plugin.check_plugin_update_now.__module__, 'decky_plugin_toolkit.plugin_updater')
            self.assertEqual(Plugin.check_plugin_update_on_load.__module__, 'decky_plugin_toolkit.plugin_updater')
            self.assertFalse(await Plugin().start_update('610;reboot'))
        finally:
            sys.path.pop(0)

    async def test_booted_transaction_does_not_offer_reboot_again(self):
        with patch.object(m, 'active', new_callable=AsyncMock, return_value=False), patch.object(self.adapter, 'get_transaction_status', new_callable=AsyncMock, return_value=dict(status='ready', pending_reboot=False)):
            await self.adapter._main()
            self.assertNotIn('done', self.adapter._state)

    async def test_pending_rollback_survives_backend_reload(self):
        transaction = dict(status='rollback-selected', pending_reboot=True, operation_id='rollback-job')
        with patch.object(m, 'active', AsyncMock(return_value=False)), patch.object(self.adapter, 'get_transaction_status', AsyncMock(return_value=transaction)):
            await self.adapter._main()
        self.assertEqual(self.adapter._state['action'], 'rollback')
        self.assertTrue(self.adapter._state['pending_reboot'])
        self.assertTrue(self.adapter._state['success'])
        self.assertEqual(self.adapter._state['operation_id'], 'rollback-job')

    async def test_launch_failure_is_not_reported_as_already_running(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LOG', Path(tmp) / 'run.log'), patch.object(m, 'active', AsyncMock(return_value=False)), patch.object(m, 'command', AsyncMock(return_value=(1, '', 'Unit start refused'))):
            result = await self.adapter.request_update('615.78.08')
            self.assertFalse(result['started'])
            self.assertEqual(result['code'], 'launch_failed')
            self.assertEqual(result['message'], 'Unit start refused')
            self.assertFalse(self.adapter._state['success'])
            self.assertIn('[fail] Unit start refused', m.LOG.read_text())

    async def test_busy_request_does_not_overwrite_the_running_job(self):
        self.adapter._state.update(running=True, operation_id='existing-job')
        with patch.object(m, 'command', AsyncMock()) as command:
            result = await self.adapter.request_rollback()
        self.assertEqual(result['code'], 'already_running')
        self.assertEqual(self.adapter._state['operation_id'], 'existing-job')
        command.assert_not_called()

    async def test_unknown_service_status_does_not_launch_another_job(self):
        with patch.object(m, 'active', AsyncMock(side_effect=OSError('systemd unavailable'))), patch.object(m, 'command', AsyncMock()) as command:
            result = await self.adapter.request_update('615.78.08')
        self.assertEqual(result['code'], 'status_unavailable')
        command.assert_not_called()

    async def test_invalid_request_has_an_explicit_result(self):
        result = await self.adapter.request_update('../invalid')
        self.assertEqual(result['code'], 'invalid_version')
        self.assertFalse(result['started'])

    async def test_same_version_leaves_previous_operation_and_log_untouched(self):
        self.adapter.get_current_version.return_value = '615.78.08-1'
        self.adapter._state.update(operation_id='previous-job', done=True, success=True)
        previous = dict(self.adapter._state)
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LOG', Path(tmp) / 'run.log'), patch.object(m, 'active', AsyncMock(return_value=False)), patch.object(m, 'command', AsyncMock()) as command:
            m.LOG.write_text('Previous operation log\n')
            for version, custom in [('615.78.08', False), ('615.78.08-1', False), ('615.78.08', True)]:
                result = await self.adapter.request_update(version, custom=custom)
                self.assertFalse(result['started'])
                self.assertEqual(result['code'], 'already_installed')
                self.assertIn('your system has not been changed', result['message'])
                self.assertEqual(self.adapter._state, previous)
                self.assertEqual(m.LOG.read_text(), 'Previous operation log\n')
            self.assertFalse(await self.adapter.start_update('615.78.08'))
            command.assert_not_called()

    async def test_different_driver_or_package_revision_can_still_start(self):
        self.adapter.get_current_version.return_value = '615.78.08-1'
        for version in ['610.57.04', '615.78.08-2', 'latest']:
            self.adapter._state['running'] = False
            with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LOG', Path(tmp) / 'run.log'), patch.object(m, 'active', AsyncMock(return_value=False)), patch.object(m, 'command', AsyncMock(return_value=(0, '', ''))) as command, patch.object(self.adapter, '_monitor_standalone', AsyncMock()):
                self.assertTrue((await self.adapter.request_update(version))['started'])
                await self.adapter._task
                self.assertEqual(command.call_args.args[-1], version)

    async def test_monitor_recovers_after_a_transient_status_error(self):
        self.adapter._state['operation_id'] = 'same-job'
        transaction = dict(status='ready', operation_id='same-job', pending_reboot=True)
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LOG', Path(tmp) / 'run.log'), patch.object(m, 'active', AsyncMock(side_effect=[OSError('temporarily unavailable'), True, False])), patch.object(m, 'controller', AsyncMock(return_value=transaction)), patch.object(m.asyncio, 'sleep', AsyncMock()), patch.object(m.decky, 'emit', AsyncMock()) as emit:
            m.LOG.write_text('[phase] verify Verifying the prepared system\n')
            await self.adapter._monitor_standalone()
            snapshots = [call.args[1] for call in emit.await_args_list]
        self.assertTrue(snapshots[0]['running'])
        self.assertFalse(snapshots[0]['done'])
        self.assertEqual(snapshots[0]['error_code'], 'status_unavailable')
        self.assertEqual(snapshots[1]['step'], 7)
        self.assertEqual(snapshots[-1]['step'], 8)
        self.assertTrue(snapshots[-1]['success'])

    async def test_payload_success_followed_by_write_failure_is_not_completion(self):
        self.adapter._state['operation_id'] = 'failed-job'
        transaction = dict(status='failed', operation_id='failed-job')
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LOG', Path(tmp) / 'run.log'), patch.object(m, 'active', AsyncMock(return_value=False)), patch.object(m, 'controller', AsyncMock(return_value=transaction)):
            m.LOG.write_text('[phase] payload Preparing driver\n[8/8] Payload verified\n[phase] write Writing system\n[fail] Write failed\n')
            await self.adapter._monitor_standalone()
        self.assertFalse(self.adapter._state['success'])
        self.assertEqual(self.adapter._state['step'], 5)
        self.assertEqual(self.adapter._state['error'], 'Write failed')


class SavedLog(unittest.TestCase):
    def test_legacy_writer_survives_decky_log_rotation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LEGACY_LOG', Path(tmp) / 'run.log'), patch.object(m, 'LOG', Path(tmp) / 'operations/run.log'):
            with m.LEGACY_LOG.open('w') as writer:
                writer.write('before reload\n')
                writer.flush()
                self.assertEqual(m.read_update_log()['lines'], ['before reload'])
                self.assertEqual(m.LOG.stat().st_ino, m.LEGACY_LOG.stat().st_ino)
                # Decky removes old top-level *.log files, never this subdirectory.
                m.LEGACY_LOG.unlink()
                writer.write('after reload\n')
                writer.flush()
                self.assertEqual(m.read_update_log()['lines'], ['before reload', 'after reload'])

    def test_legacy_migration_does_not_replace_new_log(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LEGACY_LOG', Path(tmp) / 'old.log'), patch.object(m, 'LOG', Path(tmp) / 'new.log'):
            m.LEGACY_LOG.write_text('old\n')
            m.LOG.write_text('current\n')
            m.preserve_legacy_log()
            self.assertEqual(m.read_update_log()['lines'], ['current'])
            self.assertEqual(m.LEGACY_LOG.read_text(), 'old\n')

    def test_link_failure_keeps_legacy_log_readable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LEGACY_LOG', Path(tmp) / 'old.log'), patch.object(m, 'LOG', Path(tmp) / 'new.log'):
            m.LEGACY_LOG.write_text('legacy\n')
            with patch.object(m.os, 'link', side_effect=OSError('No hard links')):
                result = m.read_update_log()
            self.assertEqual(result['lines'], ['legacy'])
            self.assertEqual(result['path'], str(m.LEGACY_LOG))

    def test_saved_log_is_available_without_an_active_operation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LOG', Path(tmp) / 'run.log'):
            m.LOG.write_text('\x1b[1;32mFinished\x1b[0m\nSaved after reboot\n', encoding='utf-8')
            result = m.read_update_log()
        self.assertEqual(result['lines'], ['Finished', 'Saved after reboot'])
        self.assertFalse(result['truncated'])
        self.assertIsNone(result['error'])

    def test_large_log_returns_bounded_recent_complete_lines(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LOG', Path(tmp) / 'run.log'):
            m.LOG.write_text(('é' * 500 + '\n') * 1000 + ''.join(f'line {n}\n' for n in range(3000)), encoding='utf-8')
            result = m.read_update_log()
        self.assertTrue(result['truncated'])
        self.assertEqual(result['lines'], [f'line {n}' for n in range(1000, 3000)])

    def test_missing_log_is_empty_and_read_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'LOG', Path(tmp) / 'missing.log'):
            self.assertEqual(m.read_update_log()['lines'], [])
            self.assertIsNone(m.read_update_log()['error'])
        with patch.object(m, 'LOG', Path(tmp)):
            with patch.object(Path, 'open', side_effect=PermissionError('Access denied')):
                self.assertIn('Access denied', m.read_update_log()['error'])


class Progress(unittest.TestCase):
    def test_filesystem_read_error_is_not_hidden_by_the_worker_exit_code(self):
        for reason in ('Bad message (74)', 'Input/output error (5)'):
            error = 'rsync: [sender] readdir("/etc/NetworkManager/conf.d"): ' + reason
            result = m.operation_error([error, '[fail] Worker returned non-zero exit status 23.'])
            self.assertIn('unreadable files', result)
            self.assertIn(error, result)

    def test_unrelated_rsync_error_keeps_the_recorded_failure(self):
        self.assertEqual(m.operation_error(['rsync: [sender] opendir("/etc/private"): Permission denied (13)', '[fail] Copy failed']), 'Copy failed')

    def test_nested_build_completion_never_finishes_the_whole_operation(self):
        state = dict(step=0)
        phases = ['custom', 'clone', 'payload', 'write', 'finalize', 'verify']
        previous = 0
        for phase in phases:
            m.update_progress(state, [f'[phase] {phase} {phase}', '[1/8] Start worker', '[8/8] Worker finished'])
            self.assertGreaterEqual(state['step'], previous)
            self.assertLess(state['step'], 8)
            previous = state['step']
        self.assertEqual(state['step'], 7)

    def test_old_worker_counter_and_unknown_phase_cannot_report_success(self):
        state = dict(step=0)
        m.update_progress(state, ['[8/8] Worker finished', '[phase] complete Done', '[phase] bogus Unknown'])
        self.assertEqual(state['step'], 0)

    def test_replayed_log_does_not_move_progress_backwards(self):
        state = dict(step=6)
        m.update_progress(state, ['[phase] clone Copying', '[phase] payload Building'])
        self.assertEqual(state['step'], 6)


class PluginUpdates(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sys.path.insert(0, str(ROOT / 'py_modules'))
        from nvidia_update import Plugin
        from nvidia_update import standalone
        from decky_plugin_toolkit import plugin_updater, project_config
        sys.path.pop(0)
        self.plugin = Plugin()
        self.plugin._state = dict(running=False, driver_spec=None)
        self.standalone = standalone
        self.updater = plugin_updater
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for module, key, filename in (
                (plugin_updater, 'PLUGIN_JSON_PATH', 'plugin.json'),
                (plugin_updater, 'PACKAGE_JSON_PATH', 'package.json'),
                (project_config, '_CONFIG_PATH', 'project.config.json')):
            mock = patch.object(module, key, self.root / filename)
            mock.start()
            self.addCleanup(mock.stop)
        self.identity('moi952', '0.4.0')

    def identity(self, owner, version):
        for name, value in (
                ('plugin.json', dict(name='Decky NVIDIA Update', version=version)),
                ('package.json', dict(name='decky-nvidia-update')),
                ('project.config.json', dict(githubOwner=owner, githubRepo='decky-nvidia-update',
                                             displayName='NVIDIA Driver Update'))):
            (self.root / name).write_text(json.dumps(value), encoding='utf-8')

    async def test_unload_cancels_only_the_observer_not_the_systemd_job(self):
        self.plugin._task = asyncio.create_task(asyncio.Event().wait())
        with patch.object(self.standalone, 'command', AsyncMock()) as command:
            await self.plugin._unload()
        self.assertTrue(self.plugin._task.cancelled())
        command.assert_not_called()

    async def test_original_to_ab_and_next_update_use_configured_repository(self):
        for owner in ('moi952', '60plus'):
            for current, latest in (('0.3.0', '0.4.0'), ('0.4.0', '0.4.1'),
                                    ('0.4.0-standalone.8', '0.4.1')):
                with self.subTest(owner=owner, current=current):
                    self.identity(owner, current)
                    base = f'https://github.com/{owner}/decky-nvidia-update'
                    asset = f'{base}/releases/download/v{latest}/decky-nvidia-update-v{latest}.zip'
                    with patch.object(self.updater.http_json, 'resolve_redirect', new_callable=AsyncMock,
                                      return_value=f'{base}/releases/download/v{latest}/_') as redirect, \
                         patch.object(self.updater.http_json, 'url_exists', new_callable=AsyncMock,
                                      return_value=True) as exists:
                        info = await self.plugin.check_plugin_update_now()
                        self.assertTrue(info['has_update'])
                        self.assertTrue(info['checked_ok'])
                        self.assertEqual(info['current_version'], current)
                        self.assertEqual(info['asset_url'], asset)
                        self.assertEqual(info['plugin_display_name'], 'Decky NVIDIA Update')
                        self.assertEqual(await self.plugin.check_plugin_update_on_load(), info)
                        redirect.assert_awaited_with(f'{base}/releases/latest/download/_')
                        exists.assert_awaited_with(asset)

    async def test_equal_or_older_release_has_no_notification(self):
        for latest in ('0.4.0', '0.3.0'):
            with patch.object(self.updater, 'resolve_latest_release', new_callable=AsyncMock,
                              return_value=dict(version=latest, url='release', asset_url='asset', sha256='')):
                self.assertIsNone(await self.plugin.check_plugin_update_on_load())
                self.assertFalse((await self.plugin.check_plugin_update_now())['has_update'])

    async def test_unavailable_github_or_missing_zip_is_not_success(self):
        with patch.object(self.updater.http_json, 'resolve_redirect', new_callable=AsyncMock,
                          side_effect=OSError('offline')):
            self.assertFalse((await self.plugin.check_plugin_update_now())['checked_ok'])
        with patch.object(self.updater.http_json, 'resolve_redirect', new_callable=AsyncMock,
                          return_value='https://github.com/moi952/decky-nvidia-update/releases/download/v0.4.1/_'), \
             patch.object(self.updater.http_json, 'url_exists', new_callable=AsyncMock, return_value=False):
            self.assertFalse((await self.plugin.check_plugin_update_now())['checked_ok'])
            self.assertIsNone(await self.plugin.check_plugin_update_on_load())

    async def test_startup_notifies_without_losing_pending_reboot(self):
        info = dict(latest_version='0.4.1', has_update=True)
        with patch.object(self.standalone, 'active', new_callable=AsyncMock, return_value=False), \
             patch.object(self.standalone, 'controller', new_callable=AsyncMock, return_value={'migrated': []}) as controller, \
             patch.object(self.plugin, 'get_transaction_status', new_callable=AsyncMock,
                          return_value=dict(pending_reboot=True, version='615.78.08')), \
             patch.object(self.plugin, 'check_plugin_update_on_load', new_callable=AsyncMock, return_value=info), \
             patch.object(self.standalone.decky, 'emit', new_callable=AsyncMock) as emit:
            await self.plugin._main()
            controller.assert_awaited_once_with('cache-migrate')
            emit.assert_awaited_once_with('plugin_update_available', info)
            self.assertTrue(self.plugin._state['success'])
            self.assertEqual(self.plugin._state['driver_spec'], '615.78.08')

    async def test_migration_failure_does_not_hide_pending_reboot_or_plugin_update(self):
        with patch.object(self.standalone, 'active', new_callable=AsyncMock, return_value=False), \
             patch.object(self.standalone, 'controller', new_callable=AsyncMock, side_effect=OSError('busy')), \
             patch.object(self.plugin, 'get_transaction_status', new_callable=AsyncMock,
                          return_value=dict(pending_reboot=True, version='615.78.08')), \
             patch.object(self.plugin, 'check_plugin_update_on_load', new_callable=AsyncMock, return_value=None) as check:
            await self.plugin._main()
            self.assertTrue(self.plugin._state['success'])
            check.assert_awaited_once()

    async def test_failed_startup_check_does_not_stop_running_job_recovery(self):
        log = self.root / 'run.log'
        log.write_text('[action] rollback\n[operation] existing-operation\n')
        with patch.object(self.standalone, 'active', new_callable=AsyncMock, return_value=True), \
             patch.object(self.standalone, 'LOG', log), \
             patch.object(self.plugin, '_monitor_standalone', new_callable=AsyncMock) as monitor, \
             patch.object(self.plugin, 'check_plugin_update_on_load', new_callable=AsyncMock,
                          side_effect=OSError('offline')):
            await self.plugin._main()
            await self.plugin._task
            monitor.assert_awaited_once_with('rollback')
            self.assertTrue(self.plugin._state['running'])
            self.assertEqual(self.plugin._state['operation_id'], 'existing-operation')


if __name__ == '__main__':
    unittest.main()
