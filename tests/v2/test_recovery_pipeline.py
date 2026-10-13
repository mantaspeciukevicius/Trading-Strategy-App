"""Failure-path tests for prepared encrypted recovery; no production/SFTP writes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
import copy
import gzip
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from recovery_pipeline import PRIMARY, atomic_json, digest, retention_keep, space, lock, storage_budget, validate_index, validate_point_manifest, utc


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result

publisher = module('publish-recovery-package')
generator = module('prepare-scheduled-recovery')
monitor = module('check-recovery-pipeline')


class RecoveryPipelineTests(unittest.TestCase):
    def fixture(self, folder, stamp='20261012T100000Z', created=None, managed=True):
        package = folder / ('v2-windows-recovery-' + stamp + '.gpg')
        package.write_bytes(b'ciphertext test fixture')
        manifest = package.with_suffix('.gpg.manifest.json')
        created = time.time() if created is None else created
        atomic_json(manifest, {'format': 'quant-v2-recovery-1', 'recipient_fingerprint': PRIMARY,
                    'encrypted_sha256': digest(package), 'encrypted_bytes': package.stat().st_size,
                    'created_epoch': created,
                    'dump_completed_epoch': created - min(.5, created / 2), 'pipeline_managed': managed})
        return package

    def test_atomic_publish_index_checksum_and_idempotent_retry(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); package = self.fixture(folder); dest = folder / 'jail/packages'
            with patch.object(publisher, 'packets'):
                publisher.publish(ROOT, dest, package, True)
                before = (dest / package.stem / package.name).stat().st_ino
                publisher.publish(ROOT, dest, package, True)
            index = json.loads((dest / 'index.json').read_text())
            self.assertEqual(index['latest'], package.stem)
            self.assertEqual(index['packages'][0]['manifest_sha256'], digest(package.with_suffix('.gpg.manifest.json')))
            self.assertEqual(before, (dest / package.stem / package.name).stat().st_ino)
            self.assertEqual({p.name for p in (dest / package.stem).iterdir()},
                             {package.name, package.name + '.manifest.json', 'SHA256SUMS'})
            self.assertEqual(index['publication_status'], 'complete')
            self.assertEqual(index['retention'], 'preserve_all_no_automatic_deletion')
            validate_index(index)
            self.assertEqual(index['packages'][0]['status'], 'complete')

    def test_later_publication_of_older_dump_does_not_replace_latest(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); dest = folder / 'jail/packages'
            fresh = self.fixture(folder, stamp='20261012T110000Z', created=time.time()-100)
            old = self.fixture(folder, stamp='20261012T100000Z', created=time.time())
            manifest = old.with_suffix('.gpg.manifest.json'); meta = json.loads(manifest.read_text())
            meta['dump_completed_epoch'] = time.time()-3600; atomic_json(manifest, meta)
            with patch.object(publisher, 'packets'):
                publisher.publish(ROOT, dest, fresh, True); publisher.publish(ROOT, dest, old, True)
            index = json.loads((dest / 'index.json').read_text())
            self.assertEqual(index['latest'], fresh.stem)
            self.assertEqual(len(index['packages']), 2)

    def test_old_completed_points_stay_visible_and_not_deleted(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); dest = folder / 'jail/packages'
            points = [self.fixture(folder, stamp=f'2026090{i}T100000Z', created=time.time()-40*86400) for i in range(1, 4)]
            with patch.object(publisher, 'packets'):
                for package in points: publisher.publish(ROOT, dest, package, True)
            index = json.loads((dest / 'index.json').read_text())
            self.assertEqual(len(index['packages']), 3)
            self.assertTrue(all((dest / p.stem / p.name).exists() for p in points))

    def test_windows_ceiling_and_free_reserve_boundaries(self):
        gib = 1024**3
        storage_budget(31*gib, gib, 11*gib)
        with self.assertRaisesRegex(ValueError, 'windows_storage_ceiling'): storage_budget(31*gib+1, gib, 20*gib)
        with self.assertRaisesRegex(ValueError, 'windows_disk_reserve'): storage_budget(0, gib, 11*gib-1)
        with self.assertRaises(ValueError): storage_budget(-1, 1, 20*gib)

    def test_index_rejects_duplicate_and_mutable_paths(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); dest = folder / 'jail/packages'; package = self.fixture(folder)
            with patch.object(publisher, 'packets'): publisher.publish(ROOT, dest, package, True)
            index = json.loads((dest / 'index.json').read_text())
            index['packages'].append(dict(index['packages'][0]))
            with self.assertRaises(ValueError): validate_index(index)
            index['packages'].pop(); index['packages'][0]['package'] = '/packages/latest.gpg'
            with self.assertRaises(ValueError): validate_index(index)

    def test_fractional_epochs_preserved_and_manifest_cross_checked(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); dest = folder / 'jail/packages'
            package = self.fixture(folder, created=1791802947.3609676)
            with patch.object(publisher, 'packets'): publisher.publish(ROOT, dest, package, True)
            entry = json.loads((dest / 'index.json').read_text())['packages'][0]
            self.assertEqual(entry['created_epoch'], 1791802947.3609676)
            self.assertEqual(entry['created_utc'], '2026-10-12T11:02:27.360968Z')
            metadata = json.loads(package.with_suffix('.gpg.manifest.json').read_text())
            validate_point_manifest(entry, metadata)
            broken = dict(entry); broken['created_epoch'] += .01
            with self.assertRaises(ValueError): validate_point_manifest(broken, metadata)

    def test_contract_limits_chronology_and_utc_mismatch(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); dest = folder / 'jail/packages'; package = self.fixture(folder)
            with patch.object(publisher, 'packets'): publisher.publish(ROOT, dest, package, True)
            index = json.loads((dest / 'index.json').read_text())
            for field, value in [('package_bytes', 2147483649), ('manifest_bytes', 65537),
                                 ('created_epoch', float('nan')), ('managed', 'true'),
                                 ('created_utc', '2000-01-01T00:00:00Z')]:
                broken = copy.deepcopy(index); broken['packages'][0][field] = value
                with self.assertRaises(ValueError): validate_index(broken)
            broken = copy.deepcopy(index); e = broken['packages'][0]
            e['dump_completed_epoch'] = e['created_epoch'] + 1; e['dump_completed_utc'] = utc(e['dump_completed_epoch'])
            with self.assertRaises(ValueError): validate_index(broken)
            broken = copy.deepcopy(index); broken['packages'] *= 5001
            with self.assertRaises(ValueError): validate_index(broken)
            broken = copy.deepcopy(index); broken['extra'] = 'x' * (4*1024**2)
            with self.assertRaises(ValueError): validate_index(broken)

    def test_legacy_null_requires_unmanaged_pin_and_does_not_override_known(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); dest = folder / 'jail/packages'
            known = self.fixture(folder, stamp='20261012T100000Z')
            legacy = self.fixture(folder, stamp='20261013T100000Z', managed=False)
            mf = legacy.with_suffix('.gpg.manifest.json'); m = json.loads(mf.read_text()); m.pop('dump_completed_epoch'); atomic_json(mf, m)
            with patch.object(publisher, 'packets'):
                publisher.publish(ROOT, dest, legacy, True); publisher.publish(ROOT, dest, known, True)
            index = json.loads((dest / 'index.json').read_text()); self.assertEqual(index['latest'], known.stem)
            old = next(e for e in index['packages'] if e['name'] == legacy.stem)
            self.assertIsNone(old['dump_completed_epoch']); self.assertIsNone(old['dump_completed_utc'])
            old['managed'] = True
            with self.assertRaises(ValueError): validate_index(index)

    def test_published_failure_status_visible_without_losing_index(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); dest = folder / 'jail/packages'; package = self.fixture(folder)
            with patch.object(publisher, 'packets'): publisher.publish(ROOT, dest, package, True)
            previous = (dest / 'index.json').read_bytes()
            broken = self.fixture(folder, stamp='20261012T110000Z'); broken.write_bytes(b'bad')
            with self.assertRaises(RuntimeError): publisher.publish(ROOT, dest, broken, True)
            self.assertEqual((dest / 'index.json').read_bytes(), previous)
            self.assertEqual(json.loads((dest / 'publication-status.json').read_text())['last_error'], 'recovery_checksum_or_recipient')

    def test_prepared_windows_client_has_no_retention_deletion(self):
        source = (ROOT / 'infrastructure/windows/Get-QuantV2Recovery.ps1').read_text()
        self.assertNotIn('Remove-Item -Recurse -LiteralPath $retired', source)
        self.assertIn('$StorageCeilingBytes = 32GB', source)
        self.assertIn('$FreeSpaceReserveBytes = 10GB', source)

    def test_corrupt_or_incomplete_pair_never_published(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); package = self.fixture(folder); dest = folder / 'jail/packages'
            package.write_bytes(b'corrupt')
            with self.assertRaises(RuntimeError): publisher.publish(ROOT, dest, package, True)
            self.assertFalse((dest / package.stem).exists())
            self.assertEqual(json.loads((dest.parent / 'publication-status.json').read_text())['last_error'], 'recovery_checksum_or_recipient')
            package.with_suffix('.gpg.manifest.json').unlink()
            with self.assertRaises(RuntimeError): publisher.publish(ROOT, dest, package, True)
            self.assertFalse((dest / package.stem).exists())

    def test_packet_validation_failure_leaves_no_visible_directory(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); package = self.fixture(folder); dest = folder / 'jail/packages'
            with patch.object(publisher, 'packets', side_effect=ValueError('recovery_ciphertext_packets')):
                with self.assertRaises(RuntimeError): publisher.publish(ROOT, dest, package, True)
            self.assertFalse((dest / package.stem).exists())

    def test_copy_corruption_never_published(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); package = self.fixture(folder); dest = folder / 'jail/packages'
            original = publisher.shutil.copyfile
            def corrupt(source, target):
                original(source, target)
                if str(target).endswith('.gpg'): Path(target).write_bytes(b'broken')
            with patch.object(publisher, 'packets'), patch.object(publisher.shutil, 'copyfile', side_effect=corrupt):
                with self.assertRaises(RuntimeError): publisher.publish(ROOT, dest, package, True)
            self.assertFalse((dest / package.stem).exists())
            self.assertFalse(list(dest.parent.glob('.quantv2-publish-*')))

    def test_crash_after_directory_rename_rebuilds_index_on_retry(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); package = self.fixture(folder); dest = folder / 'jail/packages'
            original = publisher.atomic_json
            def crash(path, *args, **kwargs):
                if Path(path).name == 'index.json': raise RuntimeError('fixture crash')
                return original(path, *args, **kwargs)
            with patch.object(publisher, 'packets'), patch.object(publisher, 'atomic_json', side_effect=crash):
                with self.assertRaises(RuntimeError): publisher.publish(ROOT, dest, package, True)
            self.assertTrue((dest / package.stem / package.name).exists())
            self.assertFalse((dest / 'index.json').exists())
            publisher.publish(ROOT, dest, package, True)
            self.assertEqual(json.loads((dest / 'index.json').read_text())['latest'], package.stem)

    def test_generation_start_keeps_consumption_state_if_process_dies(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); output = folder / 'recovery'; output.mkdir()
            status = output / 'generation-status.json'
            processed = {'v2-20261012T090000Z.sql.gz': 'fixture-digest'}
            atomic_json(status, {'initialized': True, 'processed': processed})
            original = generator.atomic_json
            def die(path, value, *args):
                original(path, value, *args)
                raise SystemExit('fixture process death')
            with patch.object(generator, 'atomic_json', side_effect=die):
                with self.assertRaises(SystemExit): generator.prepare(ROOT, folder / 'backups', output)
            recovered = json.loads(status.read_text())
            self.assertTrue(recovered['initialized']); self.assertEqual(recovered['processed'], processed)

    def test_corrupt_completion_marker_rejected_before_encryption(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); backups = folder / 'backups'; backups.mkdir()
            backup = backups / 'v2-20261012T090000Z.sql.gz'; backup.write_bytes(gzip.compress(b'CREATE TABLE public.v2_experiments (id text);\n'))
            atomic_json(backups / 'backup-status.json', {'last_success': time.time(), 'last_finished': time.time(), 'last_error': None})
            atomic_json(backup.with_name(backup.name + '.complete.json'), {'format': 'quant-v2-dump-1', 'name': backup.name, 'sha256': 'bad', 'bytes': 7})
            with self.assertRaisesRegex(RuntimeError, 'completed_dump_checksum_failed'):
                generator.prepare(ROOT, backups, folder / 'recovery')

    def test_pruned_dump_receipt_prevents_regeneration(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); backups = folder / 'backups'; backups.mkdir(); output = folder / 'recovery'; output.mkdir()
            backup = backups / 'v2-20261012T090000Z.sql.gz'; backup.write_bytes(gzip.compress(b'CREATE TABLE public.v2_experiments (id text);\n'))
            atomic_json(backups / 'backup-status.json', {'last_success': time.time(), 'last_finished': time.time(), 'last_error': None})
            atomic_json(output / 'generation-status.json', {'initialized': True, 'processed': {backup.name: digest(backup)}})
            status = generator.prepare(ROOT, backups, output)
            self.assertEqual(status['phase'], 'no_new_dump'); self.assertFalse(list(output.glob('*.gpg')))

    def test_incomplete_ciphertext_preserved_as_private_evidence(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); backups = folder / 'backups'; backups.mkdir(); output = folder / 'recovery'; output.mkdir()
            backup = backups / 'v2-20261012T090000Z.sql.gz'; backup.write_bytes(gzip.compress(b'CREATE TABLE public.v2_experiments (id text);\n'))
            atomic_json(backups / 'backup-status.json', {'last_success': time.time(), 'last_finished': time.time(), 'last_error': None})
            package = output / 'v2-windows-recovery-20261012T090000Z.gpg'; package.write_bytes(b'orphan evidence')
            def fake_encrypt(root, source, recipient, target, scheduled):
                target.write_bytes(b'new ciphertext')
                return {'dump_sha256': digest(source), 'encrypted_sha256': digest(target)}
            spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _: None))
            with patch.object(generator.importlib.util, 'spec_from_file_location', return_value=spec), \
                 patch.object(generator.importlib.util, 'module_from_spec', return_value=SimpleNamespace(encrypt=fake_encrypt)):
                generator.prepare(ROOT, backups, output)
            evidence = list((output / '.incomplete-evidence').iterdir())
            self.assertEqual(len(evidence), 1); self.assertEqual(evidence[0].read_bytes(), b'orphan evidence')

    def test_pre_schema_dump_classification_is_narrow(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            p=Path(tmp)/'dump.gz'
            for sql,expected in [(b'-- PostgreSQL database dump\nSET statement_timeout = 0;\n',True),
                                 (b'CREATE TABLE public.other (id text);\n',False),
                                 (b'CREATE TABLE public.v2_experiments (id text);\n',False)]:
                p.write_bytes(gzip.compress(sql));self.assertEqual(generator.pre_schema_dump(p),expected)
            p.write_bytes(b'corrupt');self.assertRaises(Exception,generator.pre_schema_dump,p)

    def test_empty_dump_preserved_and_newest_selected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder=Path(tmp);backups=folder/'backups';backups.mkdir();output=folder/'recovery'
            now=time.time();paths=[]
            for name,sql,age in [('v2-20261011T090000Z.sql.gz',b'-- PostgreSQL database dump\nSET statement_timeout = 0;\n',100),
                                ('v2-20261012T090000Z.sql.gz',b'CREATE TABLE public.v2_experiments (id text);\n',50),
                                ('v2-20261013T090000Z.sql.gz',b'CREATE TABLE public.v2_experiments (id text);\n',1)]:
                p=backups/name;p.write_bytes(gzip.compress(sql));os.utime(p,(now-age,now-age));paths.append(p)
            original_bytes=paths[0].read_bytes()
            atomic_json(backups/'backup-status.json',{'last_success':now,'last_finished':now,'last_error':None})
            selected=[]
            def fake_encrypt(root,source,recipient,target,scheduled):
                selected.append(source.name);target.write_bytes(b'fixture');return {'dump_sha256':digest(source),'encrypted_sha256':digest(target)}
            spec=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _:None))
            with patch.object(generator.importlib.util,'spec_from_file_location',return_value=spec), patch.object(generator.importlib.util,'module_from_spec',return_value=SimpleNamespace(encrypt=fake_encrypt)):
                state=generator.prepare(ROOT,backups,output)
            self.assertEqual(selected,[paths[2].name]);self.assertEqual(paths[0].read_bytes(),original_bytes)
            self.assertNotIn(paths[0].name,state['processed']);self.assertTrue(state['excluded_pre_schema'][paths[0].name]['preserved'])
            self.assertNotIn(paths[1].name,state['processed'])

    def test_retention_keeps_pins_two_latest_and_daily(self):
        now = 100 * 86400
        entries = [{'name': 'old-pin', 'created_epoch': 1, 'managed': False}]
        entries += [{'name': str(h), 'created_epoch': now - h * 3600, 'managed': True} for h in range(400)]
        keep = retention_keep(entries, now)
        self.assertIn('old-pin', keep); self.assertTrue({str(h) for h in range(49)} <= keep)
        self.assertNotIn('399', keep)
        old = [e for e in entries if e['name'] in keep and e['managed'] and now - e['created_epoch'] > 48 * 3600]
        self.assertEqual(len(old), len({int(e['created_epoch'] // 86400) for e in old}))
        self.assertEqual(retention_keep([{'name': 'a', 'created_epoch': 1, 'managed': True}], now), {'a'})

    def test_corrupt_published_pair_blocks_retention(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); package = self.fixture(folder, created=1); dest = folder / 'jail/packages'
            with patch.object(publisher, 'packets'): publisher.publish(ROOT, dest, package, True)
            path = dest / package.stem / package.name; path.chmod(0o600); path.write_bytes(b'corrupt')
            with self.assertRaises(RuntimeError): publisher.publish(ROOT, dest, package, True)
            self.assertTrue(path.exists())

    def test_retired_package_not_republished(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); package = self.fixture(folder); dest = folder / 'jail/packages'
            dest.mkdir(parents=True)
            atomic_json(dest.parent / 'publication-ledger.json', {package.stem: {
                'package_sha256': digest(package), 'manifest_sha256': digest(package.with_suffix('.gpg.manifest.json'))}})
            publisher.publish(ROOT, dest, package, True)
            self.assertFalse((dest / package.stem).exists())

    def test_disk_guard_and_overlap_lock(self):
        from collections import namedtuple
        with tempfile.TemporaryDirectory() as tmp:
            with patch('recovery_pipeline.shutil.disk_usage', return_value=namedtuple('D', 'total used free')(100*1024**3, 92*1024**3, 8*1024**3)):
                with self.assertRaises(ValueError): space(tmp)
            with lock(Path(tmp) / 'lock'):
                with self.assertRaises(BlockingIOError): lock(Path(tmp) / 'lock')

    def test_generation_refuses_failed_backup_and_sanitizes_error(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); backups = folder / 'backups'; backups.mkdir()
            atomic_json(backups / 'backup-status.json', {'last_error': 'sensitive fixture', 'last_success': time.time()})
            output = folder / 'recovery'
            with self.assertRaises(RuntimeError): generator.prepare(ROOT, backups, output)
            status = (output / 'generation-status.json').read_text()
            self.assertIn('scheduled_backup_not_complete', status); self.assertNotIn('sensitive fixture', status)

    def test_monitor_reports_stale_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            result = monitor.check(folder / 'g', folder / 'p', folder / 'i')
            self.assertEqual(result['assessment'], 'FAIL')
            atomic_json(folder / 'g', {'attempt_started': 1, 'last_finished': 0, 'last_success': 0})
            result = monitor.check(folder / 'g', folder / 'p', folder / 'i', now=1000)
            self.assertIn('generation_stalled', result['failures'])

    def test_safe_dump_crc_marker_failure_and_preserved_last_backup(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as tmp:
            folder = Path(tmp); tools = folder / 'bin'; tools.mkdir()
            dump = tools / 'pg_dump'; dump.write_text('#!/bin/sh\nprintf "CREATE TABLE v2_experiments ();\\n"\n'); dump.chmod(0o700)
            secret = folder / 'secret'; secret.write_text('fixture-only')
            env = {**os.environ, 'PATH': str(tools) + ':' + os.environ['PATH'], 'PGDATABASE': 'quant_v2',
                   'V2_BACKUP_SECRET': str(secret), 'V2_BACKUP_DIR': str(folder / 'backups')}
            subprocess.run(['sh', str(ROOT / 'scripts/backup-once-safe.sh')], env=env, check=True, capture_output=True)
            files = list((folder / 'backups').glob('v2-*.sql.gz')); self.assertEqual(len(files), 1)
            done = json.loads(files[0].with_name(files[0].name + '.complete.json').read_text())
            self.assertEqual(done['sha256'], digest(files[0]))
            dump.write_text('#!/bin/sh\nexit 3\n')
            result = subprocess.run(['sh', str(ROOT / 'scripts/backup-once-safe.sh')], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0); self.assertTrue(files[0].exists())
            self.assertFalse(list((folder / 'backups').glob('.backup-stage-*')))
            self.assertEqual(json.loads((folder / 'backups/backup-status.json').read_text())['last_error'], 'backup_failed')

    def test_real_publication_requires_administrator(self):
        if os.geteuid() == 0: self.skipTest('nonroot gate')
        with self.assertRaises(PermissionError): publisher.publish(ROOT, publisher.DEST)

if __name__ == '__main__': unittest.main()
