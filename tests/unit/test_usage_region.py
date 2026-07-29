"""Usage-region resolution tests for live Kiro quota polling."""

from types import SimpleNamespace

from kiro.usage import _usage_region


def _account(profile_arn: str = "", api_host: str = "", region: str = "") -> SimpleNamespace:
    return SimpleNamespace(auth_manager=SimpleNamespace(profile_arn=profile_arn, api_host=api_host, region=region))


def test_profile_arn_region_is_authoritative():
    account = _account(
        profile_arn="arn:aws:codewhisperer:eu-central-1:123456789012:profile/example",
        api_host="https://runtime.us-east-1.kiro.dev",
    )
    assert _usage_region(account) == "eu-central-1"


def test_region_falls_back_to_resolved_api_host():
    # Accounts without a profile ARN must still resolve a usable region.
    assert _usage_region(_account(api_host="https://runtime.us-east-1.kiro.dev")) == "us-east-1"


def test_region_defaults_when_host_is_unknown():
    assert _usage_region(_account(api_host="https://example.invalid")) == "us-east-1"
