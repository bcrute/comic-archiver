import io
import subprocess
import unittest
from unittest.mock import Mock, patch

from comic_archive.cloud import Transfer
from comic_archive.cloud_errors import CloudTransferError, MESSAGES, safe_code, transfer_failure
from comic_archive.models import ArchiveError
from comic_archive.network import NetworkPolicyHold


class CloudErrorTests(unittest.TestCase):
    def test_known_provider_reasons_are_fixed_hints_without_private_diagnostics(self):
        for reason, code in (
            ('storageQuotaExceeded', 'cloud_space'),
            ('insufficient_space', 'cloud_space'),
            ('storage_cap_exceeded', 'cloud_cap'),
            ('transaction_cap_exceeded', 'cloud_cap'),
            ('cap_exceeded', 'cloud_cap'),
            ('expired_auth_token', 'cloud_login_required'),
            ('bad_auth_token', 'cloud_login_required'),
            ('too_many_write_operations', 'cloud_rate_limited'),
            ('too_many_requests', 'cloud_rate_limited'),
            ('invalid_access_token', 'cloud_login_required'),
            ('expired_access_token', 'cloud_login_required'),
            ('missing_scope', 'cloud_permission'),
            ('no_write_permission', 'cloud_permission'),
            ('userRateLimitExceeded', 'cloud_rate_limited'),
            ('rateLimitExceeded', 'cloud_rate_limited'),
            ('invalid_client', 'cloud_client_required'),
            ('unauthorized_client', 'cloud_client_required'),
            ('accessNotConfigured', 'cloud_client_required'),
            ('authError', 'cloud_login_required'),
            ('invalid_grant', 'cloud_login_required'),
            ('invalid_token', 'cloud_login_required'),
            ('Invalid access token (Code=401, Status=401)', 'cloud_login_required'),
            ('Incorrect login credentials', 'cloud_login_required'),
            ('insufficientFilePermissions', 'cloud_permission'),
        ):
            with self.subTest(reason=reason):
                diagnostic = io.BytesIO(('PRIVATE_PATH PRIVATE_TOKEN '+reason).encode())
                error = transfer_failure(diagnostic, 7)
                self.assertEqual(safe_code(error), code)
                self.assertEqual(str(error), MESSAGES[code])
                self.assertNotIn('PRIVATE', str(error))
        # An ambiguous "quota" message does not distinguish storage from API limits.
        self.assertEqual(safe_code(transfer_failure(io.BytesIO(b'quota exceeded PRIVATE'), 7)), 'cloud_unknown')

    def test_exit_codes_are_temporary_or_transfer_limits_without_claiming_cloud_quota(self):
        for exit_code, code in ((5,'cloud_connection'), (8,'cloud_transfer_limit'), (10,'cloud_transfer_limit'),
                                (1,'cloud_unknown'), (6,'cloud_unknown'), (7,'cloud_unknown'), (9,'cloud_unknown')):
            self.assertEqual(safe_code(transfer_failure(io.BytesIO(b'PRIVATE'), exit_code)),code)

    def test_diagnostics_are_bounded_and_foreign_error_attributes_are_not_trusted(self):
        class Bounded(io.BytesIO):
            def read(self, amount=-1):
                self.amount = amount
                return super().read(amount)
        diagnostic = Bounded(b'invalid_grant PRIVATE'+b'x'*65536)
        self.assertEqual(safe_code(transfer_failure(diagnostic, 1)),'cloud_unknown')
        self.assertEqual(diagnostic.amount,65536)
        error = ArchiveError('PRIVATE');error.cloud_code='cloud_space'
        self.assertEqual(safe_code(error),'cloud_unknown')
        for code in ('PRIVATE', [], None):
            self.assertEqual(safe_code(CloudTransferError(code)),'cloud_unknown')
        self.assertEqual(safe_code(NetworkPolicyHold('PRIVATE')),'cloud_network_held')

    def test_subprocess_diagnostics_timeout_and_failed_verification_use_typed_safe_errors(self):
        transfer=object.__new__(Transfer);transfer.binary='/fixture/rclone';transfer.config='/fixture/private-config'
        def failure(command, **options):
            options['stderr'].write(b'PRIVATE_PATH PRIVATE_TOKEN storageQuotaExceeded')
            return subprocess.CompletedProcess(command,7)
        with patch('comic_archive.cloud.subprocess.run',side_effect=failure):
            with self.assertRaises(CloudTransferError) as caught:transfer.run('copyto','PRIVATE','google:PRIVATE')
            self.assertEqual(safe_code(caught.exception),'cloud_space')
            self.assertNotIn('PRIVATE',str(caught.exception))
        with patch('comic_archive.cloud.subprocess.run',side_effect=OSError('PRIVATE')):
            with self.assertRaises(CloudTransferError) as caught:transfer.run('listremotes')
            self.assertEqual(safe_code(caught.exception),'cloud_connection')
        transfer.provider='proton';transfer.target={'remote':'proton'};transfer.run=Mock(return_value=b'b'*64)
        with self.assertRaises(CloudTransferError) as caught:transfer.verify('own/episode.cbz','a'*64)
        self.assertEqual(safe_code(caught.exception),'cloud_verification')
