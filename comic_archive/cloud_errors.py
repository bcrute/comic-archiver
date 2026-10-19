"""Fixed recovery hints; provider diagnostics and credentials never escape."""
import re

from .models import ArchiveError

MESSAGES = {
    'b2_key_permissions': 'Create a Read and Write application key restricted to your library bucket, with the file prefix and duration left blank. Copy its keyID and applicationKey again.',
    'b2_bucket_mismatch': 'This key is for a different bucket than the bucket name in step 2. Type the name exactly as Backblaze’s Buckets page shows it, or create the key again for that bucket.',
    'b2_private_bucket': 'Your B2 library bucket must be Private. Change its privacy setting in Backblaze, then connect again.',
    'cloud_login_required': 'Cloud sign-in needs repair. Sign in to this provider again, then retry. Local episodes are retained.',
    'cloud_space': 'The provider reported insufficient cloud storage. Free space through your provider or choose another destination, then retry. Local episodes are retained.',
    'cloud_cap': 'Backblaze stopped the upload: your account has used its free 10 GB, or reached a spending limit set on its Caps & Alerts page. Add a card on Backblaze’s Billing page or raise the limit, then retry. Local episodes are retained.',
    'cloud_rate_limited': 'The provider limited requests. Wait before retrying. Your saved sign-in and local episodes are retained.',
    'cloud_connection': 'The cloud transfer was unavailable or timed out. Check the connection and rclone installation, then retry. Local episodes are retained.',
    'cloud_permission': 'The provider refused write access. Check the selected account and connection permissions, then retry. Local episodes are retained.',
    'cloud_verification': 'The cloud copy could not be verified. Local episodes are retained; retry the upload before removing any files.',
    'cloud_transfer_limit': 'A cloud transfer limit was reached. Check the connection settings before retrying. Local episodes are retained.',
    'cloud_network_held': 'The network policy held the cloud transfer. Reconnect to an allowed network, then retry. Local episodes are retained.',
    'cloud_unknown': 'The cloud transfer failed for an unrecognized reason. Check sign-in, connection and cloud space, then retry. Local episodes are retained.',
}


class CloudTransferError(ArchiveError):
    def __init__(self, code='cloud_unknown'):
        self.cloud_code = code if isinstance(code, str) and code in MESSAGES else 'cloud_unknown'
        super().__init__(MESSAGES[self.cloud_code])


class CloudCleanupError(CloudTransferError):
    def __init__(self, code='cloud_unknown'):
        super().__init__(code)
        self.args = ('Cloud test cleanup is pending. '+MESSAGES[self.cloud_code],)


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
