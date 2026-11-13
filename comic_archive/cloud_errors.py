"""Fixed recovery hints; provider diagnostics and credentials never escape."""
import re

from .models import ArchiveError
from .messages import say

MESSAGES = {
    'b2_key_permissions': say('cloud.create_a_read_and_write_application'),
    'b2_bucket_mismatch': say('dashboard.this_key_is_for_a_different'),
    'b2_private_bucket': say('cloud.your_b2_library_bucket_must_be'),
    'cloud_login_required': say('cloud.cloud_sign_in_needs_repair_sign'),
    'cloud_space': say('cloud.the_provider_reported_insufficient_cloud_storage'),
    'cloud_cap': say('cloud.backblaze_stopped_the_upload_your_account'),
    'cloud_rate_limited': say('cloud.the_provider_limited_requests_wait_before'),
    'cloud_connection': say('cloud.the_cloud_transfer_was_unavailable_or'),
    'cloud_permission': say('cloud.the_provider_refused_write_access_check'),
    'cloud_verification': say('cloud.the_cloud_copy_could_not_be'),
    'cloud_transfer_limit': say('cloud.a_cloud_transfer_limit_was_reached'),
    'cloud_network_held': say('cloud.the_network_policy_held_the_cloud'),
    'cloud_unknown': say('cloud.the_cloud_transfer_failed_for_an'),
}


class CloudTransferError(ArchiveError):
    def __init__(self, code='cloud_unknown'):
        self.cloud_code = code if isinstance(code, str) and code in MESSAGES else 'cloud_unknown'
        super().__init__(MESSAGES[self.cloud_code])


class CloudCleanupError(CloudTransferError):
    def __init__(self, code='cloud_unknown'):
        super().__init__(code)
        self.args = (say('cloud.cloud_test_cleanup_is_pending')+MESSAGES[self.cloud_code],)


def safe_code(error):
    from .network import NetworkPolicyHold
    if isinstance(error, NetworkPolicyHold):
        return 'cloud_network_held'
    code = getattr(error, 'cloud_code', None)
    return code if isinstance(error, CloudTransferError) and isinstance(code, str) and code in MESSAGES else 'cloud_unknown'


def transfer_failure(stderr, exit_code):
    """Inspect at most 64 KiB of private diagnostics; retain only a known code.

    B2's documented error codes are recognized, along with the wording other providers
    used before only B2 was offered; other backend failures remain unknown. These are repair hints, not a provider-health test.
    Rclone exit 5 is temporary, and 8/10 are local transfer/duration limits.
    """
    end = stderr.seek(0, 2)
    stderr.seek(max(0, end - 65536))
    text = stderr.read(65536).decode('utf-8', errors='replace').lower()
    reasons = (
        ('cloud_space', r'\bstoragequotaexceeded\b|\binsufficient_space\b'),
        # Backblaze's storage, transaction and download caps: storage_cap_exceeded, transaction_cap_exceeded and the like.
        ('cloud_cap', r'\b\w*cap_exceeded\b'),
        ('cloud_rate_limited', r'\b(?:userratelimitexceeded|ratelimitexceeded|too_many_requests|too_many_write_operations)\b'),
        ('cloud_login_required', r'\b(?:autherror|invalid_grant|invalid_token|invalid_access_token|expired_access_token|bad_auth_token|expired_auth_token)\b|invalid access token|incorrect login credentials'),
        ('cloud_permission', r'\binsufficientfilepermissions\b|\b(?:missing_scope|no_write_permission)\b'),
    )
    for code, pattern in reasons:
        if re.search(pattern, text):
            return CloudTransferError(code)
    return CloudTransferError('cloud_connection' if exit_code == 5 else
                              'cloud_transfer_limit' if exit_code in (8, 10) else 'cloud_unknown')
