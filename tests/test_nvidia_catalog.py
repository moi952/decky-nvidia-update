import asyncio
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

spec = importlib.util.spec_from_file_location(
    'nvidia_catalog', Path(__file__).resolve().parents[1] / 'py_modules/nvidia_update/catalog.py')
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


def entry(version, category='nfb', notes='%3Cul%3E%3Cli%3EFixed%20a%20driver%20issue.%3C/li%3E%3C/ul%3E'):
    return {'DisplayVersion': version, 'ReleaseDateTime': 'Wed Oct 07, 2026',
            'ReleaseNotes': notes, {'nfb':'IsFeaturePreview', 'recommended':'IsRecommended', 'beta':'IsBeta'}[category]: '1'}


class MergeCatalogs(unittest.TestCase):
    def test_production_beta_and_nfb_notes_are_all_retained(self):
        result = catalog.merge_catalogs([[entry('595.104.02','recommended'), entry('590.44.01','beta')],
                                        [entry('615.78.08'), entry('610.57.04')]])
        self.assertEqual(set(result), {'595.104.02','590.44.01','615.78.08','610.57.04'})
        self.assertEqual(result['590.44.01']['category'], 'beta')
        self.assertEqual(result['615.78.08'], {'category':'nfb', 'date':'Wed Oct 07, 2026', 'notes':['Fixed a driver issue.']})
        self.assertTrue(result['610.57.04']['notes'])

    def test_promoted_release_keeps_production_category_and_its_notes(self):
        result = catalog.merge_catalogs([[entry('610.57.04','recommended','Production notes')],
                                        [entry('610.57.04','nfb','Historical NFB notes')]])
        self.assertEqual(len(result), 1)
        self.assertEqual(result['610.57.04']['category'], 'recommended')
        self.assertEqual(result['610.57.04']['notes'], ['Production notes'])

    def test_duplicate_only_fills_missing_notes_and_date(self):
        production = entry('610.57.04','recommended',None)
        production['ReleaseDateTime'] = None
        result = catalog.merge_catalogs([[production], [entry('610.57.04')]])['610.57.04']
        self.assertEqual(result['category'], 'recommended')
        self.assertEqual(result['notes'], ['Fixed a driver issue.'])
        self.assertEqual(result['date'], 'Wed Oct 07, 2026')

    def test_bad_rows_do_not_hide_valid_releases(self):
        result = catalog.merge_catalogs([[None, [], {}, entry('../615'), entry(615),
                                         dict(entry('610.57.04'),IsFeaturePreview=0), entry('615.78.08')]])
        self.assertEqual(list(result), ['615.78.08'])

    def test_notes_do_not_leak_between_different_versions(self):
        result = catalog.merge_catalogs([[entry('595.104.02','recommended')], [entry('615.78.08',notes='')]])
        self.assertEqual(result['615.78.08']['notes'], [])

    def test_encoded_html_becomes_plain_text(self):
        self.assertEqual(catalog.notes_to_lines('%3Cul%3E%3Cli%3EA%20%26amp%3B%20B%3Cbr%3Enext%3C/li%3E%3Cli%3E%3Ca%20href=%22https://example.com%22%3ELink%3C/a%3E%3C/li%3E%3C/ul%3E'),
                         ['A & B next','Link'])
        self.assertEqual(catalog.notes_to_lines(None), [])


class FetchCatalog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logger = types.SimpleNamespace(info=Mock(),error=Mock())
        self.env = {'PATH':'/usr/bin'}

    async def fetch(self, production, nfb, codes=(0,0)):
        async def spawn(*args, **kwargs):
            channel = int(args[-1].rsplit('=',1)[1])
            raw = (production,nfb)[channel]
            if isinstance(raw, Exception):
                raise raw
            # Finish NFB first: the merge must still respect production priority.
            if channel == 0:
                await asyncio.sleep(0)
            return types.SimpleNamespace(returncode=codes[channel], communicate=AsyncMock(return_value=(raw,b'')))
        with patch.object(catalog.asyncio,'create_subprocess_exec',side_effect=spawn) as create:
            result = await catalog.fetch_catalog(self.env,self.logger)
        self.assertEqual(create.call_count,2)
        for call in create.call_args_list:
            self.assertIn('--max-time',call.args)
            self.assertIn('--connect-timeout',call.args)
            self.assertEqual(call.kwargs['env'],self.env)
        return result

    @staticmethod
    def response(*entries):
        return json.dumps({'IDS':[{'downloadInfo':e} for e in entries]}).encode()

    async def test_both_queries_are_merged_despite_completion_order(self):
        result = await self.fetch(self.response(entry('610.57.04','recommended')),
                                  self.response(entry('610.57.04'),entry('615.78.08')))
        self.assertEqual(result['610.57.04']['category'],'recommended')
        self.assertEqual(result['615.78.08']['category'],'nfb')
        self.logger.error.assert_not_called()

    async def test_nfb_network_failure_keeps_production(self):
        result = await self.fetch(self.response(entry('595.104.02','recommended')),b'',codes=(0,28))
        self.assertEqual(list(result),['595.104.02'])
        self.logger.error.assert_called_once()

    async def test_production_network_failure_keeps_nfb(self):
        result = await self.fetch(b'',self.response(entry('615.78.08')),codes=(22,0))
        self.assertEqual(list(result),['615.78.08'])

    async def test_bad_json_in_one_response_keeps_other(self):
        result = await self.fetch(b'<html>unavailable</html>',self.response(entry('615.78.08')))
        self.assertTrue(result['615.78.08']['notes'])

    async def test_invalid_schema_and_process_failure_degrade_to_empty(self):
        self.assertEqual(await self.fetch(b'{"IDS":null}',OSError('curl unavailable')), {})
        self.assertEqual(self.logger.error.call_count,2)

    async def test_bad_download_info_does_not_discard_good_rows(self):
        raw = json.dumps({'IDS':[None,{'downloadInfo':None},{'downloadInfo':entry('615.78.08')}]}).encode()
        self.assertEqual(list(await self.fetch(self.response(),raw)),['615.78.08'])

    async def test_cancellation_reaps_curl(self):
        proc = types.SimpleNamespace(returncode=None,communicate=AsyncMock(side_effect=[asyncio.CancelledError(),(b'',b'')]))
        proc.kill = Mock(side_effect=lambda:setattr(proc,'returncode',-9))
        other = types.SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(self.response(),b'')))
        with patch.object(catalog.asyncio,'create_subprocess_exec',side_effect=[proc,other]):
            with self.assertRaises(asyncio.CancelledError):
                await catalog.fetch_catalog(self.env,self.logger)
        proc.kill.assert_called_once()
        self.assertEqual(proc.communicate.await_count,2)


if __name__ == '__main__':
    unittest.main()
