#!/usr/bin/env python3
"""
Quick Space Migrator — Bedrock AgentCore MCP Server
════════════════════════════════════════════════════════

Migrates Quick Spaces, Agents, Action Connectors, and S3 Knowledge Bases
between AWS accounts.

Recreates every link exactly as in the source — agents are created with
their Spaces and Action Connectors attached; connectors and knowledge
bases are linked to their spaces. No manual UI linkage step is required.

Permissions are NOT hardcoded — the server DESCRIBES the permissions on
each source resource and copies the exact same actions to the target.

Env vars (configure in AgentCore):
  SOURCE_ROLE_ARN  — IAM role in source account (read-only QuickSight + S3)
  TARGET_ROLE_ARN  — IAM role in target account (read+write QuickSight + S3)

Tool inputs:
  source_account_id, target_account_id, region
  spaces: "all" or comma-separated list of space IDs
  source_env, target_env: environment names used in KB bucket names
                          (knowledge-base-<env>-<account>)
  qs_service_role: QuickSight service role name (no API to look this up)
"""

import os
import sys
import json
import time
import uuid
import logging
import traceback

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, BotoCoreError
from mcp.server.fastmcp import FastMCP

# Configure logging for CloudWatch (AgentCore captures stdout/stderr)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)
logger.info("=== Quick Space Migrator MCP Server starting ===")
logger.info(f"SOURCE_ROLE_ARN configured: {'YES' if os.environ.get('SOURCE_ROLE_ARN') else 'NO'}")
logger.info(f"TARGET_ROLE_ARN configured: {'YES' if os.environ.get('TARGET_ROLE_ARN') else 'NO'}")

# ═══════════════════════════════════════════════════════════════════
# ENV CONFIG (set these in AgentCore runtime config)
# ═══════════════════════════════════════════════════════════════════

SOURCE_ROLE_ARN = os.environ.get("SOURCE_ROLE_ARN", "")
TARGET_ROLE_ARN = os.environ.get("TARGET_ROLE_ARN", "")

DEFAULT_QS_SERVICE_ROLE = "aws-quicksight-service-role-v0"

MEDIA_EXTRACTION_CONFIG = {
    "imageExtractionConfiguration": {"imageExtractionStatus": "ENABLED"},
    "audioExtractionConfiguration": {"audioExtractionStatus": "ENABLED"},
    "videoExtractionConfiguration": {
        "videoExtractionStatus": "ENABLED",
        "videoExtractionType": "VISUAL_CONTENT_AND_AUDIO_TRANSCRIPTION",
    },
}


# ═══════════════════════════════════════════════════════════════════
# ERROR HELPERS
# ═══════════════════════════════════════════════════════════════════

def classify_error(e: Exception) -> dict:
    """Classify an AWS error into a user-friendly message."""
    error_info = {
        "error_type": type(e).__name__,
        "raw_message": str(e),
        "user_message": str(e),
        "is_retryable": False,
        "is_kms": False,
        "is_access_denied": False,
    }

    if isinstance(e, ClientError):
        error_code = e.response.get("Error", {}).get("Code", "")
        error_msg = e.response.get("Error", {}).get("Message", "")
        error_info["error_code"] = error_code

        if "KMS" in error_msg or "key" in error_msg.lower() or "encrypt" in error_msg.lower():
            error_info["is_kms"] = True
            error_info["user_message"] = (
                f"Encryption key error: {error_msg}. "
                "The resource may reference a deleted or inaccessible KMS key. "
                "The resource may need to be recreated."
            )
        elif error_code == "AccessDeniedException":
            error_info["is_access_denied"] = True
            if "KMS" in error_msg or "key" in error_msg.lower() or "encrypt" in error_msg.lower():
                error_info["is_kms"] = True
                error_info["user_message"] = (
                    f"Access denied due to encryption key issue: {error_msg}. "
                    "Required encryption key not found or inaccessible. "
                    "The resource may need to be recreated."
                )
            else:
                error_info["user_message"] = (
                    f"Access denied: {error_msg}. "
                    "Check that the IAM role has the required permissions."
                )
        elif error_code == "ResourceNotFoundException":
            error_info["user_message"] = f"Resource not found: {error_msg}"
        elif error_code == "ResourceExistsException":
            error_info["user_message"] = f"Resource already exists: {error_msg}"
        elif error_code == "ThrottlingException":
            error_info["is_retryable"] = True
            error_info["user_message"] = f"API rate limit hit: {error_msg}. Try again shortly."
        elif error_code == "ConflictException":
            error_info["is_retryable"] = True
            error_info["user_message"] = (
                f"Resource conflict: {error_msg}. The resource may be in a transitional state."
            )
        elif error_code == "InvalidParameterValueException":
            error_info["user_message"] = f"Invalid parameter: {error_msg}"
        else:
            error_info["user_message"] = f"{error_code}: {error_msg}"

    elif isinstance(e, BotoCoreError):
        error_info["user_message"] = f"AWS SDK error: {str(e)}"
        error_info["is_retryable"] = True

    return error_info


def format_error_for_report(context: str, e: Exception) -> dict:
    classified = classify_error(e)
    return {
        "context": context,
        "error_type": classified["error_type"],
        "error_code": classified.get("error_code", "Unknown"),
        "user_message": classified["user_message"],
        "is_kms": classified["is_kms"],
        "is_access_denied": classified["is_access_denied"],
    }


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def assume_role_client(service: str, role_arn: str, region: str):
    """Assume an IAM role and return a boto3 client."""
    logger.info(f"Assuming role: {role_arn} (service={service}, region={region})")
    try:
        sts = boto3.client("sts")
        creds = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="space-migrator",
            DurationSeconds=3600,
        )["Credentials"]
        client = boto3.client(
            service,
            region_name=region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            config=Config(retries={"max_attempts": 3, "mode": "adaptive"}),
        )
        logger.info(f"  ✓ Role assumed successfully ({service})")
        return client
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        error_msg = e.response.get("Error", {}).get("Message", "")
        logger.error(f"  ✗ Failed to assume role: {error_code} - {error_msg}")
        raise RuntimeError(
            f"Failed to assume role {role_arn}: {error_code} - {error_msg}. "
            "Verify the role ARN exists and the trust policy allows this account to assume it."
        ) from e
    except Exception as e:
        logger.error(f"  ✗ Unexpected error assuming role: {e}")
        raise RuntimeError(f"Unexpected error assuming role {role_arn}: {str(e)}") from e


def remap_arn(arn: str, target_account: str, target_region: str) -> str:
    """Swap account + region in a QuickSight ARN."""
    parts = arn.split(":")
    parts[3] = target_region
    parts[4] = target_account
    return ":".join(parts)


def remap_principal(principal_arn: str, target_account: str, target_region: str) -> str:
    """Swap account + region in a QuickSight user/group principal ARN."""
    if not principal_arn or ":" not in principal_arn:
        return principal_arn
    parts = principal_arn.split(":")
    if len(parts) > 4:
        parts[3] = target_region
        parts[4] = target_account
    return ":".join(parts)


def sanitize_auth_config(auth_config: dict) -> dict:
    """
    Convert AuthenticationConfig from describe (read model) to create (write model).
    Uses PLACEHOLDER values for secrets (connector must be re-authenticated in target UI).
    """
    if not auth_config:
        return auth_config

    sanitized = json.loads(json.dumps(auth_config, default=str))
    metadata = sanitized.get("AuthenticationMetadata", {})
    if not metadata:
        return sanitized

    if "AuthorizationCodeGrantMetadata" in metadata:
        acg = metadata["AuthorizationCodeGrantMetadata"]
        read_details = acg.pop("ReadAuthorizationCodeGrantCredentialsDetails", {})
        read_grant = read_details.get("ReadAuthorizationCodeGrantDetails", {})
        acg["AuthorizationCodeGrantCredentialsSource"] = "PLAIN_CREDENTIALS"
        acg["AuthorizationCodeGrantCredentialsDetails"] = {
            "AuthorizationCodeGrantDetails": {
                "ClientId": read_grant.get("ClientId", "PLACEHOLDER_REQUIRES_REAUTH"),
                "ClientSecret": "PLACEHOLDER_REQUIRES_REAUTH",
                "TokenEndpoint": read_grant.get("TokenEndpoint", "https://example.com/token"),
                "AuthorizationEndpoint": read_grant.get("AuthorizationEndpoint", "https://example.com/authorize"),
            }
        }
        metadata["AuthorizationCodeGrantMetadata"] = acg

    if "ClientCredentialsGrantMetadata" in metadata:
        ccg = metadata["ClientCredentialsGrantMetadata"]
        read_details = ccg.pop("ReadClientCredentialsDetails", {})
        read_grant = read_details.get("ReadClientCredentialsGrantDetails", {})
        ccg["ClientCredentialsSource"] = "PLAIN_CREDENTIALS"
        ccg["ClientCredentialsDetails"] = {
            "ClientCredentialsGrantDetails": {
                "ClientId": read_grant.get("ClientId", "PLACEHOLDER_REQUIRES_REAUTH"),
                "ClientSecret": "PLACEHOLDER_REQUIRES_REAUTH",
                "TokenEndpoint": read_grant.get("TokenEndpoint", "https://example.com/token"),
            }
        }
        metadata["ClientCredentialsGrantMetadata"] = ccg

    if "BasicAuthConnectionMetadata" in metadata:
        metadata["BasicAuthConnectionMetadata"].setdefault("Password", "PLACEHOLDER_REQUIRES_REAUTH")

    if "ApiKeyConnectionMetadata" in metadata:
        metadata["ApiKeyConnectionMetadata"].setdefault("ApiKey", "PLACEHOLDER_REQUIRES_REAUTH")

    if "IamConnectionMetadata" in metadata:
        metadata["IamConnectionMetadata"].pop("SourceArn", None)

    sanitized["AuthenticationMetadata"] = metadata
    return sanitized


def wait_for_active(client, account_id: str, agent_id: str, timeout: int = 60):
    """Poll until agent is ACTIVE."""
    for _ in range(timeout // 5):
        try:
            resp = client.describe_agent(AwsAccountId=account_id, AgentId=agent_id)
            if resp["Agent"].get("AgentStatus") == "ACTIVE":
                return True
        except ClientError as e:
            error_info = classify_error(e)
            logger.warning(f"  Poll agent '{agent_id}' failed: {error_info['user_message']}")
            if not error_info["is_retryable"]:
                return False
        time.sleep(5)
    return False


def wait_for_kb_active(client, account_id: str, kb_id: str, timeout: int = 120):
    """Poll until a knowledge base is ACTIVE.

    After create/update the KB goes into CREATING/UPDATING and permission
    grants are rejected until it returns to ACTIVE.
    """
    for _ in range(timeout // 5):
        try:
            resp = client.describe_knowledge_base(AwsAccountId=account_id, KnowledgeBaseId=kb_id)
            if resp.get("KnowledgeBase", {}).get("Status") == "ACTIVE":
                return True
        except ClientError:
            return False
        time.sleep(5)
    return False



def copy_space_permissions(source_qs, target_qs, source_account, target_account, region, space_id, report):
    """Describe source space permissions and replicate the exact actions in target."""
    try:
        perms = source_qs.describe_space_permissions(
            AwsAccountId=source_account, SpaceId=space_id
        ).get("Permissions", [])
    except ClientError as e:
        logger.warning(f"  ⚠ describe_space_permissions '{space_id}': {classify_error(e)['user_message']}")
        return
    _grant(target_qs, target_account, region, "space", space_id, perms, report)


def copy_agent_permissions(source_qs, target_qs, source_account, target_account, region, agent_id, report):
    try:
        perms = source_qs.describe_agent_permissions(
            AwsAccountId=source_account, AgentId=agent_id
        ).get("Permissions", [])
    except ClientError as e:
        logger.warning(f"  ⚠ describe_agent_permissions '{agent_id}': {classify_error(e)['user_message']}")
        return
    _grant(target_qs, target_account, region, "agent", agent_id, perms, report)


def copy_connector_permissions(source_qs, target_qs, source_account, target_account, region, connector_id, report):
    try:
        perms = source_qs.describe_action_connector_permissions(
            AwsAccountId=source_account, ActionConnectorId=connector_id
        ).get("Permissions", [])
    except ClientError as e:
        logger.warning(f"  ⚠ describe_action_connector_permissions '{connector_id}': {classify_error(e)['user_message']}")
        return
    _grant(target_qs, target_account, region, "connector", connector_id, perms, report)


def copy_kb_permissions(source_qs, target_qs, source_account, target_account, region, kb_id, report):
    try:
        perms = source_qs.describe_knowledge_base_permissions(
            AwsAccountId=source_account, KnowledgeBaseId=kb_id
        ).get("Permissions", [])
    except ClientError as e:
        logger.warning(f"  ⚠ describe_knowledge_base_permissions '{kb_id}': {classify_error(e)['user_message']}")
        return
    _grant(target_qs, target_account, region, "kb", kb_id, perms, report)


def _parse_principal_arn(principal_arn):
    """Split a QuickSight principal ARN into (kind, namespace, name).

    ARN tail looks like: user/<namespace>/<user-name...> or
    group/<namespace>/<group-name...>.
    """
    resource = principal_arn.split(":", 5)[-1]
    segments = resource.split("/")
    kind = segments[0] if segments else ""
    namespace = segments[1] if len(segments) > 1 else "default"
    name = "/".join(segments[2:]) if len(segments) > 2 else ""
    return kind, namespace, name


def _list_target_users(target_qs, target_account, namespace, _cache={}):
    """List and cache all registered QuickSight users in the target namespace."""
    key = (target_account, namespace)
    if key in _cache:
        return _cache[key]
    users = []
    try:
        paginator = target_qs.get_paginator("list_users")
        for page in paginator.paginate(AwsAccountId=target_account, Namespace=namespace):
            users.extend(page.get("UserList", []))
    except Exception:
        try:
            resp = target_qs.list_users(AwsAccountId=target_account, Namespace=namespace, MaxResults=100)
            users = resp.get("UserList", [])
        except ClientError:
            users = []
    _cache[key] = users
    return users


def _resolve_target_principal(target_qs, target_account, region, principal_arn, _cache={}):
    """Resolve a remapped source principal to a real principal ARN that is
    registered in the target account.

    QuickSight federated users are registered under an ARN that embeds the IAM
    role + session name (e.g. user/default/<IAM-Role>/<session-name>). The
    same human in the target account may be registered under a different role,
    so a blind account-swap of the ARN often doesn't exist. We therefore:
      1. Use the remapped ARN as-is if it is already registered.
      2. Otherwise list the target users and match by (a) identical UserName,
         (b) same trailing session/user name, then (c) same email.
      3. Fall back to the sole registered user if there is exactly one.
    Returns the resolved principal ARN, or None if nothing suitable was found.
    """
    if not principal_arn or ":" not in principal_arn:
        return None
    if principal_arn in _cache:
        return _cache[principal_arn]

    kind, namespace, name = _parse_principal_arn(principal_arn)
    resolved = None

    # 1. Direct hit — already registered in the target.
    try:
        if kind == "user":
            target_qs.describe_user(AwsAccountId=target_account, Namespace=namespace, UserName=name)
            resolved = principal_arn
        elif kind == "group":
            target_qs.describe_group(AwsAccountId=target_account, Namespace=namespace, GroupName=name)
            resolved = principal_arn
    except ClientError:
        resolved = None

    # 2. For users, try to match a registered target user by name/email.
    if resolved is None and kind == "user":
        users = _list_target_users(target_qs, target_account, namespace)
        if users:
            src_tail = name.split("/")[-1].lower()      # e.g. <session-name>
            src_full = name.lower()
            # (a) identical UserName, (b) same trailing session name, (c) same email
            def _match(u):
                un = u.get("UserName", "")
                return un.lower() == src_full or un.split("/")[-1].lower() == src_tail
            candidates = [u for u in users if _match(u)]
            if not candidates:
                # (c) email local-part match (session name often equals email alias)
                candidates = [u for u in users
                              if u.get("Email", "").split("@")[0].lower() == src_tail]
            # (d) last resort: if exactly one user is registered, use it.
            if not candidates and len(users) == 1:
                candidates = users
            if candidates:
                resolved = candidates[0].get("Arn")

    _cache[principal_arn] = resolved
    return resolved


def _grant(target_qs, target_account, region, kind, resource_id, perms, report):
    """Grant the copied permissions on the target resource, remapping principals."""
    if not perms:
        return
    grant = []
    unresolved = []
    remapped_note = []
    for p in perms:
        original = remap_principal(p.get("Principal", ""), target_account, region)
        actions = p.get("Actions", [])
        if not (original and actions):
            continue
        # Resolve to a principal that actually exists in the target account —
        # QuickSight rejects the whole grant call if any principal is unknown.
        principal = _resolve_target_principal(target_qs, target_account, region, original)
        if not principal:
            unresolved.append(original)
            continue
        if principal != original:
            remapped_note.append({"from": original, "to": principal})
        grant.append({"Principal": principal, "Actions": actions})
    if remapped_note:
        for m in remapped_note:
            logger.info(f"  ⓘ {kind} '{resource_id}': resolved principal {m['from']} → {m['to']}")
    if unresolved:
        report.setdefault("skipped_permissions", []).append({
            "resource_type": kind, "resource_id": resource_id,
            "unresolved_principals": unresolved,
            "reason": "No registered QuickSight user could be matched in the "
                      "target account. Register/log into QuickSight in the target "
                      "account, then re-run to copy these permissions.",
        })
        logger.info(f"  ⓘ {kind} '{resource_id}': {len(unresolved)} principal(s) could not be resolved — see skipped_permissions in report")
    if not grant:
        return
    try:
        if kind == "space":
            target_qs.update_space_permissions(AwsAccountId=target_account, SpaceId=resource_id, GrantPermissions=grant)
        elif kind == "agent":
            target_qs.update_agent_permissions(AwsAccountId=target_account, AgentId=resource_id, GrantPermissions=grant)
        elif kind == "connector":
            target_qs.update_action_connector_permissions(AwsAccountId=target_account, ActionConnectorId=resource_id, GrantPermissions=grant)
        elif kind == "kb":
            target_qs.update_knowledge_base_permissions(AwsAccountId=target_account, KnowledgeBaseId=resource_id, GrantPermissions=grant)
        logger.info(f"  ✓ Copied {kind} permissions to '{resource_id}' ({len(grant)} principals)")
    except ClientError as e:
        logger.warning(f"  ⚠ Grant {kind} perms '{resource_id}': {classify_error(e)['user_message']}")


# ── S3 bucket creation ──────────────────────────────────────────────

def kb_bucket_name(env: str, account_id: str) -> str:
    """knowledge-base-<env>-<account> — must start with knowledge-base- for the IAM policy."""
    return f"knowledge-base-{env}-{account_id}"


def bucket_policy_for_quicksight(bucket_name: str, account_id: str, qs_service_role: str) -> dict:
    role_arn = f"arn:aws:iam::{account_id}:role/service-role/{qs_service_role}"
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowQuick",
            "Effect": "Allow",
            "Principal": {"AWS": role_arn},
            "Action": [
                "s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation",
                "s3:GetObjectVersion", "s3:ListBucketVersions",
            ],
            "Resource": [
                f"arn:aws:s3:::{bucket_name}",
                f"arn:aws:s3:::{bucket_name}/*",
            ],
        }],
    }


# Expected trust policy for the QuickSight service role.
EXPECTED_QS_TRUST = {
    "principal_service": "quicksight.amazonaws.com",
    "actions": {"sts:AssumeRole", "sts:TagSession"},
}


def verify_qs_service_role(iam_client, account_id: str, qs_service_role: str, report: dict) -> bool:
    """
    Preflight: confirm the QuickSight service role exists (via iam:GetRole) and
    that its trust policy allows quicksight.amazonaws.com to assume it.

    Returns True if the role exists and the trust policy looks correct.
    Records a clear error in the report (and returns False) otherwise, so a
    name mismatch or bad trust policy fails loudly instead of producing a
    bucket QuickSight can't read.
    """
    role_name = qs_service_role
    try:
        resp = iam_client.get_role(RoleName=role_name)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchEntity", "NoSuchEntityException"):
            report["errors"].append({
                "context": f"verify_qs_service_role({role_name})",
                "user_message": (
                    f"QuickSight service role '{role_name}' not found in account {account_id}. "
                    "Pass the correct qs_service_role (e.g. aws-quicksight-service-role-v0) "
                    "or create it before migrating knowledge bases."
                ),
            })
        else:
            report["errors"].append(format_error_for_report(f"verify_qs_service_role({role_name})", e))
        return False

    # Validate the trust policy.
    role = resp.get("Role", {})
    trust = role.get("AssumeRolePolicyDocument", {})
    # boto3 may return the trust doc URL-encoded as a string
    if isinstance(trust, str):
        try:
            import urllib.parse
            trust = json.loads(urllib.parse.unquote(trust))
        except Exception:
            trust = {}

    services = set()
    actions = set()
    for stmt in trust.get("Statement", []):
        if stmt.get("Effect") != "Allow":
            continue
        principal = stmt.get("Principal", {})
        svc = principal.get("Service")
        if isinstance(svc, str):
            services.add(svc)
        elif isinstance(svc, list):
            services.update(svc)
        act = stmt.get("Action")
        if isinstance(act, str):
            actions.add(act)
        elif isinstance(act, list):
            actions.update(act)

    if EXPECTED_QS_TRUST["principal_service"] not in services:
        report["errors"].append({
            "context": f"verify_qs_service_role({role_name})",
            "user_message": (
                f"QuickSight service role '{role_name}' trust policy does not allow "
                f"'{EXPECTED_QS_TRUST['principal_service']}' to assume it "
                f"(found principals: {sorted(services) or 'none'}). "
                "QuickSight will not be able to read the knowledge base bucket."
            ),
        })
        return False

    if "sts:AssumeRole" not in actions:
        report["errors"].append({
            "context": f"verify_qs_service_role({role_name})",
            "user_message": (
                f"QuickSight service role '{role_name}' trust policy is missing 'sts:AssumeRole' "
                f"(found actions: {sorted(actions) or 'none'})."
            ),
        })
        return False

    missing = EXPECTED_QS_TRUST["actions"] - actions
    if missing:
        # sts:TagSession missing is a warning, not fatal — QuickSight may still work.
        logger.warning(f"  ⚠ QuickSight service role '{role_name}' trust policy missing {sorted(missing)} "
                       "(non-fatal, but recommended).")

    logger.info(f"  ✓ QuickSight service role '{role_name}' verified (trust allows quicksight.amazonaws.com)")
    return True


def create_kb_bucket(s3, bucket_name, region, account_id, qs_service_role, report):
    """Create bucket (idempotent) + attach QuickSight bucket policy."""
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(Bucket=bucket_name, CreateBucketConfiguration={"LocationConstraint": region})
        logger.info(f"  ✓ Bucket created: {bucket_name}")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            logger.info(f"  ℹ Bucket exists: {bucket_name} ({code})")
        else:
            logger.warning(f"  ⚠ create_bucket '{bucket_name}': {classify_error(e)['user_message']}")
            report["errors"].append(format_error_for_report(f"create_bucket({bucket_name})", e))
    try:
        s3.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(
            bucket_policy_for_quicksight(bucket_name, account_id, qs_service_role)))
        logger.info(f"  ✓ Bucket policy attached: {bucket_name}")
    except ClientError as e:
        logger.warning(f"  ⚠ put_bucket_policy '{bucket_name}': {classify_error(e)['user_message']}")


# ═══════════════════════════════════════════════════════════════════
# MIGRATION LOGIC
# ═══════════════════════════════════════════════════════════════════

def do_migrate(source_account_id, target_account_id, space_ids, region,
               source_env, target_env, qs_service_role) -> dict:
    logger.info("=" * 60)
    logger.info("MIGRATION STARTED")
    logger.info(f"  Source: {source_account_id} (env={source_env})")
    logger.info(f"  Target: {target_account_id} (env={target_env})")
    logger.info(f"  Spaces: {space_ids}")
    logger.info(f"  Region: {region}")
    logger.info("=" * 60)

    report = {
        "source_account": source_account_id,
        "target_account": target_account_id,
        "region": region,
        "source_env": source_env,
        "target_env": target_env,
        "migrated": {"spaces": [], "agents": [], "connectors": [], "knowledge_bases": [], "buckets": []},
        "linkage_instructions": [],
        "skipped_permissions": [],
        "errors": [],
        "steps": [],
    }

    # — Assume roles —
    try:
        source_qs = assume_role_client("quicksight", SOURCE_ROLE_ARN, region)
    except RuntimeError as e:
        report["errors"].append({"context": "assume_source_role", "user_message": str(e)})
        report["overall_status"] = "FAILED"
        return report
    try:
        target_qs = assume_role_client("quicksight", TARGET_ROLE_ARN, region)
        target_s3 = assume_role_client("s3", TARGET_ROLE_ARN, region)
        target_iam = assume_role_client("iam", TARGET_ROLE_ARN, region)
    except RuntimeError as e:
        report["errors"].append({"context": "assume_target_role", "user_message": str(e)})
        report["overall_status"] = "FAILED"
        return report

    # —— Step 1: Resolve space IDs ——
    logger.info("Step 1: Resolving spaces...")
    if space_ids == ["all"]:
        try:
            resp = source_qs.list_spaces(AwsAccountId=source_account_id)
            space_ids = [s["spaceId"] for s in resp.get("SpaceSummaries", [])]
        except ClientError as e:
            report["errors"].append(format_error_for_report("list_spaces", e))
            report["overall_status"] = "FAILED"
            return report
    if not space_ids:
        report["errors"].append({"context": "resolve_spaces", "user_message": "No spaces found in source account."})
        report["overall_status"] = "FAILED"
        return report
    report["steps"].append({"step": "resolve_spaces", "space_ids": space_ids})

    # —— Step 2: Read source spaces + resources (connectors + KBs) ——
    logger.info("Step 2: Reading source spaces...")
    source_spaces = {}
    all_connector_arns = set()
    all_kb_ids = set()

    for space_id in space_ids:
        try:
            space_info = source_qs.describe_space(AwsAccountId=source_account_id, SpaceId=space_id)
        except ClientError as e:
            report["errors"].append(format_error_for_report(f"describe_space({space_id})", e))
            continue
        try:
            resources = source_qs.list_space_resources(
                AwsAccountId=source_account_id, SpaceId=space_id
            ).get("SpaceResources", [])
        except ClientError as e:
            report["errors"].append(format_error_for_report(f"list_space_resources({space_id})", e))
            resources = []

        space_connectors, space_kbs = [], []
        for r in resources:
            rtype = r.get("ResourceType")
            arn = r.get("ResourceDetails", {}).get("resourceArn", "")
            if rtype == "ACTION_CONNECTOR":
                all_connector_arns.add(arn); space_connectors.append(arn)
            elif rtype == "KNOWLEDGE_BASE":
                kb_id = arn.split("/")[-1]
                all_kb_ids.add(kb_id); space_kbs.append(kb_id)

        source_spaces[space_id] = {
            "name": space_info.get("Name", space_id),
            "description": space_info.get("Description", ""),
            "connector_arns": space_connectors,
            "kb_ids": space_kbs,
        }

    if not source_spaces:
        report["errors"].append({"context": "read_spaces", "user_message": "Could not read any source spaces."})
        report["overall_status"] = "FAILED"
        return report
    report["steps"].append({"step": "read_spaces", "spaces": {sid: s["name"] for sid, s in source_spaces.items()}})

    # —— Step 3: Discover agents for these spaces ——
    logger.info("Step 3: Discovering agents...")
    source_agents = []
    try:
        all_agent_data = source_qs.list_agents(AwsAccountId=source_account_id)
    except ClientError as e:
        report["errors"].append(format_error_for_report("list_agents", e))
        all_agent_data = {"AgentSummaries": []}

    for summary in all_agent_data.get("AgentSummaries", []):
        try:
            detail = source_qs.describe_agent(AwsAccountId=source_account_id, AgentId=summary["AgentId"])
        except ClientError as e:
            report["errors"].append(format_error_for_report(f"describe_agent({summary['AgentId']})", e))
            continue
        agent = detail["Agent"]
        linked_space_ids = []
        for space_id in space_ids:
            space_arn = f"arn:aws:quicksight:{region}:{source_account_id}:space/{space_id}"
            if space_arn in agent.get("Spaces", []):
                linked_space_ids.append(space_id)
        if linked_space_ids:
            source_agents.append({"agent": agent, "linked_spaces": linked_space_ids})
            for arn in agent.get("ActionConnectors", []):
                all_connector_arns.add(arn)
    report["steps"].append({"step": "discover_agents", "agents": [a["agent"]["AgentId"] for a in source_agents]})

    # —— Step 4: Create spaces in target + copy permissions ——
    logger.info("Step 4: Creating spaces in target...")
    for space_id, space_cfg in source_spaces.items():
        try:
            target_qs.create_space(
                AwsAccountId=target_account_id, SpaceId=space_id,
                Name=space_cfg["name"], Description=space_cfg["description"],
            )
            status = "CREATED"
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ResourceExistsException":
                # Idempotent: update the existing space to match the source
                try:
                    target_qs.update_space(
                        AwsAccountId=target_account_id, SpaceId=space_id,
                        Name=space_cfg["name"], Description=space_cfg["description"],
                    )
                    status = "UPDATED"
                except ClientError as ue:
                    report["errors"].append(format_error_for_report(f"update_space({space_id})", ue))
                    status = f"FAILED: {classify_error(ue)['user_message']}"
            else:
                report["errors"].append(format_error_for_report(f"create_space({space_id})", e))
                status = f"FAILED: {classify_error(e)['user_message']}"
        copy_space_permissions(source_qs, target_qs, source_account_id, target_account_id, region, space_id, report)
        report["migrated"]["spaces"].append({"space_id": space_id, "name": space_cfg["name"], "status": status})

    # Build a robust connector_id -> [space_ids] map. A connector counts as
    # linked to a space if EITHER the space lists it as a resource OR an agent
    # attached to that space references it. Matching is by connector_id (last
    # ARN segment) to avoid ARN-format/region mismatches.
    conn_space_map = {}
    def _cid(arn):
        return arn.split("/")[-1]
    for sid, scfg in source_spaces.items():
        for arn in scfg["connector_arns"]:
            conn_space_map.setdefault(_cid(arn), set()).add(sid)
    for agent_entry in source_agents:
        agent_conns = {_cid(a) for a in agent_entry["agent"].get("ActionConnectors", [])}
        for sid in agent_entry["linked_spaces"]:
            for cid in agent_conns:
                conn_space_map.setdefault(cid, set()).add(sid)

    # —— Step 5: Create connectors in target + copy permissions + link ——
    logger.info("Step 5: Creating connectors in target...")
    for source_connector_arn in all_connector_arns:
        connector_id = source_connector_arn.split("/")[-1]
        connector_data = None
        try:
            src_cfg = source_qs.describe_action_connector(
                AwsAccountId=source_account_id, ActionConnectorId=connector_id)
            connector_data = src_cfg.get("ActionConnector", src_cfg)
        except ClientError as e:
            report["errors"].append(format_error_for_report(f"describe_action_connector({connector_id})", e))
            report["migrated"]["connectors"].append({
                "connector_id": connector_id, "name": connector_id,
                "type": "UNKNOWN (failed to describe)",
                "status": f"FAILED: {classify_error(e)['user_message']}", "linked_to_spaces": []})
            continue

        try:
            target_qs.create_action_connector(
                AwsAccountId=target_account_id, ActionConnectorId=connector_id,
                Name=connector_data.get("Name", connector_id),
                Type=connector_data.get("Type", "GENERIC_HTTP"),
                AuthenticationConfig=sanitize_auth_config(
                    connector_data.get("AuthenticationConfig", {
                        "AuthenticationType": "NONE",
                        "AuthenticationMetadata": {"NoneConnectionMetadata": {"BaseEndpoint": "https://example.com"}},
                    })),
            )
            status = "CREATED"
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ResourceExistsException":
                # Idempotent: update the existing connector (Type is immutable — omit it)
                try:
                    upd_params = {
                        "AwsAccountId": target_account_id,
                        "ActionConnectorId": connector_id,
                        "Name": connector_data.get("Name", connector_id),
                        "AuthenticationConfig": sanitize_auth_config(
                            connector_data.get("AuthenticationConfig", {
                                "AuthenticationType": "NONE",
                                "AuthenticationMetadata": {"NoneConnectionMetadata": {"BaseEndpoint": "https://example.com"}},
                            })),
                    }
                    if connector_data.get("Description"):
                        upd_params["Description"] = connector_data["Description"]
                    target_qs.update_action_connector(**upd_params)
                    status = "UPDATED"
                except ClientError as ue:
                    report["errors"].append(format_error_for_report(f"update_action_connector({connector_id})", ue))
                    status = f"FAILED: {classify_error(ue)['user_message']}"
            else:
                report["errors"].append(format_error_for_report(f"create_action_connector({connector_id})", e))
                status = f"FAILED: {classify_error(e)['user_message']}"

        copy_connector_permissions(source_qs, target_qs, source_account_id, target_account_id, region, connector_id, report)

        # Link to every space this connector belongs to (space resource OR agent-attached).
        linked_spaces = sorted(conn_space_map.get(connector_id, set()))
        for sid in linked_spaces:
            target_connector_arn = remap_arn(source_connector_arn, target_account_id, region)
            try:
                target_qs.update_space_resources(
                    AwsAccountId=target_account_id, SpaceId=sid,
                    AddResources=[{"ResourceType": "ACTION_CONNECTOR",
                                   "ResourceDetails": {"resourceArn": target_connector_arn}}])
            except ClientError as e:
                logger.warning(f"  ⚠ Link connector to space '{sid}': {classify_error(e)['user_message']}")

        report["migrated"]["connectors"].append({
            "connector_id": connector_id,
            "name": connector_data.get("Name", connector_id),
            "type": connector_data.get("Type", "UNKNOWN"),
            "status": status,
            "linked_to_spaces": linked_spaces,
        })

    # —— Step 6: Migrate knowledge bases (create target bucket + data source + KB) ——
    logger.info("Step 6: Migrating knowledge bases...")
    target_bucket = kb_bucket_name(target_env, target_account_id)
    source_bucket = kb_bucket_name(source_env, source_account_id)
    kb_migration_ok = True
    if all_kb_ids:
        # Preflight: verify the QuickSight service role exists + trust policy is correct
        # BEFORE creating the bucket, so a name mismatch fails loudly (not silently
        # producing a bucket QuickSight can't read).
        if not verify_qs_service_role(target_iam, target_account_id, qs_service_role, report):
            logger.error("  ✗ QuickSight service role preflight failed — skipping KB migration.")
            report["steps"].append({"step": "kb_service_role_check", "status": "FAILED"})
            kb_migration_ok = False
        else:
            # Service role OK — proceed to create the bucket + KBs.
            create_kb_bucket(target_s3, target_bucket, region, target_account_id, qs_service_role, report)
            report["migrated"]["buckets"].append({"bucket": target_bucket, "env": target_env, "account": target_account_id})
            report["steps"].append({"step": "kb_buckets", "source_bucket": source_bucket, "target_bucket": target_bucket})

    # Only migrate KBs if the service-role preflight passed.
    for kb_id in (all_kb_ids if kb_migration_ok else []):
        try:
            kb_detail = source_qs.describe_knowledge_base(
                AwsAccountId=source_account_id, KnowledgeBaseId=kb_id).get("KnowledgeBase", {})
        except ClientError as e:
            report["errors"].append(format_error_for_report(f"describe_knowledge_base({kb_id})", e))
            report["migrated"]["knowledge_bases"].append({
                "knowledge_base_id": kb_id, "status": f"FAILED: {classify_error(e)['user_message']}"})
            continue

        kb_name = kb_detail.get("Name", kb_id)
        new_kb_id = kb_id  # preserve source KB id in target
        kb_config = {"templateConfiguration": {"template": {
            "deletionProtectionConfiguration": {"enableDeletionProtection": "false", "deletionProtectionThreshold": "15"},
            "type": "S3V2",
            "filterConfiguration": {"inclusionPatterns": [], "maxFileSizeInMegaBytes": "10240",
                                    "inclusionPrefixes": [], "exclusionPatterns": [], "exclusionPrefixes": []},
            "connectionConfiguration": {"bucketName": target_bucket, "bucketOwnerAccountId": target_account_id},
        }}}

        # Is there already a KB with this id in the target? (idempotent re-run)
        existing_kb = None
        try:
            existing_kb = target_qs.describe_knowledge_base(
                AwsAccountId=target_account_id, KnowledgeBaseId=new_kb_id).get("KnowledgeBase")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code", "") != "ResourceNotFoundException":
                logger.warning(f"  ⚠ describe target KB '{new_kb_id}': {classify_error(e)['user_message']}")

        if existing_kb:
            # ── UPDATE path ── reuse the existing data source, update KB + data source
            status = "UPDATED"
            existing_ds_arn = existing_kb.get("DataSourceArn", "")
            existing_ds_id = existing_ds_arn.split("/")[-1] if existing_ds_arn else None
            if existing_ds_id:
                try:
                    target_qs.update_data_source(
                        AwsAccountId=target_account_id, DataSourceId=existing_ds_id,
                        Name=f"{kb_name} - datasource",
                        DataSourceParameters={"S3KnowledgeBaseParameters": {
                            "BucketUrl": f"s3://{target_bucket}"}},
                    )
                except ClientError as ue:
                    logger.warning(f"  ⚠ update_data_source(kb {kb_id}): {classify_error(ue)['user_message']}")
            try:
                target_qs.update_knowledge_base(
                    AwsAccountId=target_account_id, KnowledgeBaseId=new_kb_id,
                    Name=kb_name,
                    KnowledgeBaseConfiguration=kb_config,
                    MediaExtractionConfiguration=MEDIA_EXTRACTION_CONFIG,
                )
            except ClientError as ue:
                report["errors"].append(format_error_for_report(f"update_knowledge_base({kb_id})", ue))
                status = f"FAILED: {classify_error(ue)['user_message']}"
        else:
            # ── CREATE path ── fresh data source + knowledge base
            new_ds_id = str(uuid.uuid4())
            ds_arn = None
            try:
                ds_resp = target_qs.create_data_source(
                    AwsAccountId=target_account_id, DataSourceId=new_ds_id,
                    Name=f"{kb_name} - datasource", Type="S3_KNOWLEDGE_BASE",
                    DataSourceParameters={"S3KnowledgeBaseParameters": {
                        "BucketUrl": f"s3://{target_bucket}"}},
                )
                ds_arn = ds_resp.get("Arn", f"arn:aws:quicksight:{region}:{target_account_id}:datasource/{new_ds_id}")
            except ClientError as e:
                report["errors"].append(format_error_for_report(f"create_data_source(kb {kb_id})", e))

            status = "FAILED: data source not created"
            if ds_arn:
                try:
                    target_qs.create_knowledge_base(
                        AwsAccountId=target_account_id, KnowledgeBaseId=new_kb_id,
                        Name=kb_name, DataSourceArn=ds_arn,
                        KnowledgeBaseConfiguration=kb_config,
                        MediaExtractionConfiguration=MEDIA_EXTRACTION_CONFIG,
                    )
                    status = "CREATED"
                except ClientError as e:
                    report["errors"].append(format_error_for_report(f"create_knowledge_base({kb_id})", e))
                    status = f"FAILED: {classify_error(e)['user_message']}"

        # copy KB permissions from source (wait for ACTIVE — grants are rejected while UPDATING/CREATING)
        wait_for_kb_active(target_qs, target_account_id, new_kb_id)
        copy_kb_permissions(source_qs, target_qs, source_account_id, target_account_id, region, kb_id, report)

        # attach KB to the target spaces that referenced it
        target_kb_arn = f"arn:aws:quicksight:{region}:{target_account_id}:knowledge-base/{new_kb_id}"
        linked_spaces = [sid for sid, scfg in source_spaces.items() if kb_id in scfg["kb_ids"]]
        for sid in linked_spaces:
            try:
                target_qs.update_space_resources(
                    AwsAccountId=target_account_id, SpaceId=sid,
                    AddResources=[{"ResourceType": "KNOWLEDGE_BASE",
                                   "ResourceDetails": {"resourceArn": target_kb_arn}}])
            except ClientError as e:
                logger.warning(f"  ⚠ Attach KB to space '{sid}': {classify_error(e)['user_message']}")

        report["migrated"]["knowledge_bases"].append({
            "knowledge_base_id": new_kb_id, "name": kb_name, "status": status,
            "target_bucket": target_bucket, "linked_to_spaces": linked_spaces,
        })

    # —— Step 7: Create agents in target (linked to spaces, exactly like source) + copy permissions ——
    logger.info("Step 7: Creating agents in target...")
    for agent_entry in source_agents:
        agent = agent_entry["agent"]
        agent_id = agent["AgentId"]
        linked_spaces = agent_entry["linked_spaces"]
        # Remap the source space ARNs to the target account so the agent is
        # linked to its spaces exactly as in the source.
        target_space_arns = [
            f"arn:aws:quicksight:{region}:{target_account_id}:space/{sid}"
            for sid in linked_spaces
        ]

        custom_prompt_input = None
        if agent.get("CustomPromptInterface"):
            src_p = agent["CustomPromptInterface"]
            new_prompt = {k: v for k, v in {
                "CustomInstructions": src_p.get("CustomInstructions"),
                "Identity": src_p.get("Identity"),
                "Tone": src_p.get("Tone"),
                "OutputStyle": src_p.get("OutputStyle"),
                "ResponseLength": src_p.get("ResponseLength"),
            }.items() if v}
            if new_prompt:
                custom_prompt_input = {"NewPrompt": new_prompt}

        target_connector_arns = [remap_arn(a, target_account_id, region) for a in agent.get("ActionConnectors", [])]
        create_params = {
            "AwsAccountId": target_account_id, "AgentId": agent_id,
            "Name": agent["Name"], "AgentLifecycle": agent.get("AgentLifecycle", "PUBLISHED"),
            "ActionConnectors": target_connector_arns if target_connector_arns else [],
            "Spaces": target_space_arns if target_space_arns else [],
        }
        if agent.get("Description"):
            create_params["Description"] = agent["Description"]
        if custom_prompt_input:
            create_params["CustomPromptInput"] = custom_prompt_input
        if agent.get("StarterPrompts"):
            create_params["StarterPrompts"] = agent["StarterPrompts"]
        if agent.get("WelcomeMessage"):
            create_params["WelcomeMessage"] = agent["WelcomeMessage"]
        if agent.get("IconId"):
            create_params["IconId"] = agent["IconId"]
        if not create_params["ActionConnectors"]:
            del create_params["ActionConnectors"]
        if not create_params["Spaces"]:
            del create_params["Spaces"]

        try:
            target_qs.create_agent(**create_params)
            status = "CREATED"
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ResourceExistsException":
                # Idempotent: wait for ACTIVE, then update (no AgentLifecycle; uses *ToAdd fields)
                try:
                    wait_for_active(target_qs, target_account_id, agent_id)
                    # Dedup: only add spaces/connectors not already linked, else
                    # re-runs keep appending the same ARNs (duplicate linkages).
                    existing_spaces, existing_connectors = set(), set()
                    try:
                        cur = target_qs.describe_agent(
                            AwsAccountId=target_account_id, AgentId=agent_id
                        ).get("Agent", {})
                        existing_spaces = set(cur.get("Spaces", []) or [])
                        existing_connectors = set(cur.get("ActionConnectors", []) or [])
                    except ClientError:
                        pass
                    upd_params = {
                        "AwsAccountId": target_account_id, "AgentId": agent_id,
                        "Name": agent["Name"],
                    }
                    if agent.get("Description"):
                        upd_params["Description"] = agent["Description"]
                    if custom_prompt_input:
                        upd_params["CustomPromptInput"] = custom_prompt_input
                    if agent.get("StarterPrompts"):
                        upd_params["StarterPrompts"] = agent["StarterPrompts"]
                    if agent.get("WelcomeMessage"):
                        upd_params["WelcomeMessage"] = agent["WelcomeMessage"]
                    if agent.get("IconId"):
                        upd_params["IconId"] = agent["IconId"]
                    connectors_to_add = [a for a in target_connector_arns if a not in existing_connectors]
                    spaces_to_add = [a for a in target_space_arns if a not in existing_spaces]
                    if connectors_to_add:
                        upd_params["ActionConnectorsToAdd"] = connectors_to_add
                    if spaces_to_add:
                        upd_params["SpacesToAdd"] = spaces_to_add
                    target_qs.update_agent(**upd_params)
                    status = "UPDATED"
                except ClientError as ue:
                    ucode = ue.response.get("Error", {}).get("Code", "")
                    if ucode == "ConflictException":
                        status = "SKIPPED_UPDATING (agent busy — retry later)"
                        logger.warning(f"  ⚠ update_agent '{agent_id}' conflict: agent is UPDATING")
                    else:
                        report["errors"].append(format_error_for_report(f"update_agent({agent_id})", ue))
                        status = f"FAILED: {classify_error(ue)['user_message']}"
            else:
                report["errors"].append(format_error_for_report(f"create_agent({agent_id})", e))
                status = f"FAILED: {classify_error(e)['user_message']}"

        if "FAILED" not in status:
            wait_for_active(target_qs, target_account_id, agent_id)
            copy_agent_permissions(source_qs, target_qs, source_account_id, target_account_id, region, agent_id, report)

        report["migrated"]["agents"].append({
            "agent_id": agent_id, "name": agent["Name"], "status": status,
            "connectors": [a.split("/")[-1] for a in agent.get("ActionConnectors", [])],
            "linked_to_spaces": linked_spaces,
        })

    report["steps"].append({"step": "create_agents", "count": len(source_agents)})

    # — Overall status —
    has_errors = len(report["errors"]) > 0
    spaces_ok = any(s["status"] in ("CREATED", "ALREADY_EXISTS", "UPDATED") for s in report["migrated"]["spaces"])
    if not spaces_ok:
        report["overall_status"] = "FAILED"
    elif has_errors:
        report["overall_status"] = "COMPLETED_WITH_ERRORS"
    else:
        report["overall_status"] = "COMPLETE"

    logger.info("=" * 60)
    logger.info(f"MIGRATION {report['overall_status']}")
    logger.info(f"  Spaces: {len(report['migrated']['spaces'])}  Connectors: {len(report['migrated']['connectors'])}")
    logger.info(f"  Agents: {len(report['migrated']['agents'])}  KBs: {len(report['migrated']['knowledge_bases'])}")
    logger.info(f"  Errors: {len(report['errors'])}")
    logger.info("=" * 60)
    return report


# ═══════════════════════════════════════════════════════════════════
# MCP SERVER
# ═══════════════════════════════════════════════════════════════════

# Bind host. Amazon Bedrock AgentCore delivers requests to the container from
# outside its loopback interface, so the server must listen on 0.0.0.0 to
# receive them — binding to 127.0.0.1 would make the runtime unreachable.
# This is not a public exposure: the container runs behind AgentCore's managed
# ingress, inbound is gated by the Cognito JWT authorizer, and the runtime
# operates in VPC network mode (private subnets). Override with BIND_HOST if
# you run the server in a different context.
#
# nosec B104 — bind-all is required for the AgentCore container runtime (see above).
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")  # nosec B104

mcp = FastMCP("quick-space-migrator", host=BIND_HOST, stateless_http=True)


@mcp.tool()
def migrate_spaces(
    source_account_id: str,
    target_account_id: str,
    spaces: str = "all",
    region: str = "us-east-1",
    source_env: str = "dev",
    target_env: str = "prod",
    qs_service_role: str = DEFAULT_QS_SERVICE_ROLE,
) -> str:
    """
    Migrate Quick Spaces, Agents, Connectors, and S3 Knowledge Bases from source
    to target account. Recreates every link exactly as in the source — agents are
    created with their Spaces and Action Connectors attached. Permissions are
    copied by DESCRIBING the source resource permissions and replicating the exact
    actions in the target.

    Args:
        source_account_id: 12-digit source AWS account ID
        target_account_id: 12-digit target AWS account ID
        spaces: "all" or comma-separated space IDs
        region: AWS region (default: us-east-1)
        source_env: env name used in the source KB bucket (knowledge-base-<env>-<account>)
        target_env: env name used in the target KB bucket (knowledge-base-<env>-<account>)
        qs_service_role: QuickSight service role name for the S3 bucket policy
                         (no API exists to look this up — pass it explicitly)

    Returns:
        JSON migration report with migrated spaces, agents, connectors,
        knowledge_bases (each with linked_to_spaces), buckets, and errors.
    """
    logger.info(f"[TOOL] migrate_spaces: source={source_account_id}({source_env}) target={target_account_id}({target_env}) spaces={spaces}")
    try:
        space_list = ["all"] if spaces.strip().lower() == "all" else [s.strip() for s in spaces.split(",")]
        result = do_migrate(source_account_id, target_account_id, space_list, region,
                            source_env, target_env, qs_service_role)
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"[TOOL] migrate_spaces fatal: {traceback.format_exc()}")
        return json.dumps({
            "overall_status": "FAILED", "error": str(e),
            "user_message": f"Migration failed unexpectedly: {str(e)}. Check CloudWatch logs.",
        }, indent=2)


@mcp.tool()
def preview_migration(
    source_account_id: str,
    spaces: str = "all",
    region: str = "us-east-1",
) -> str:
    """
    Dry run: show what would be migrated (spaces, agents, connectors, knowledge bases).
    Read-only — no changes made.

    Args:
        source_account_id: Source AWS account ID
        spaces: "all" or comma-separated space IDs
        region: AWS region

    Returns:
        JSON inventory of spaces, agents, connectors, and knowledge bases.
    """
    logger.info(f"[TOOL] preview_migration: source={source_account_id}, spaces={spaces}")
    try:
        source_qs = assume_role_client("quicksight", SOURCE_ROLE_ARN, region)
    except RuntimeError as e:
        return json.dumps({"status": "FAILED", "user_message": f"Preview failed: {str(e)}"}, indent=2)

    try:
        if spaces.strip().lower() == "all":
            resp = source_qs.list_spaces(AwsAccountId=source_account_id)
            space_ids = [s["spaceId"] for s in resp.get("SpaceSummaries", [])]
        else:
            space_ids = [s.strip() for s in spaces.split(",")]
    except ClientError as e:
        error_info = classify_error(e)
        return json.dumps({"status": "FAILED", "error_code": error_info.get("error_code", "Unknown"),
                           "user_message": f"Preview failed: {error_info['user_message']}"}, indent=2)

    if not space_ids:
        return json.dumps({"status": "FAILED", "user_message": "No spaces found in source account."}, indent=2)

    inventory = {"spaces": [], "agents": [], "connectors": set(), "knowledge_bases": set(), "errors": []}
    connector_details, kb_details = {}, {}

    for space_id in space_ids:
        try:
            space_info = source_qs.describe_space(AwsAccountId=source_account_id, SpaceId=space_id)
        except ClientError as e:
            inventory["errors"].append({"context": f"describe_space({space_id})", "user_message": classify_error(e)["user_message"]})
            continue
        try:
            resources = source_qs.list_space_resources(
                AwsAccountId=source_account_id, SpaceId=space_id).get("SpaceResources", [])
        except ClientError as e:
            inventory["errors"].append({"context": f"list_space_resources({space_id})", "user_message": classify_error(e)["user_message"]})
            resources = []

        connector_ids, kb_ids = [], []
        for r in resources:
            rtype = r.get("ResourceType")
            arn = r.get("ResourceDetails", {}).get("resourceArn", "")
            cid = arn.split("/")[-1]
            if rtype == "ACTION_CONNECTOR":
                connector_ids.append(cid); inventory["connectors"].add(cid)
            elif rtype == "KNOWLEDGE_BASE":
                kb_ids.append(cid); inventory["knowledge_bases"].add(cid)

        inventory["spaces"].append({"space_id": space_id, "name": space_info.get("Name"),
                                    "connectors": connector_ids, "knowledge_bases": kb_ids})

    # Agents
    try:
        all_agents = source_qs.list_agents(AwsAccountId=source_account_id)
    except ClientError as e:
        inventory["errors"].append({"context": "list_agents", "user_message": classify_error(e)["user_message"]})
        all_agents = {"AgentSummaries": []}

    for summary in all_agents.get("AgentSummaries", []):
        try:
            detail = source_qs.describe_agent(AwsAccountId=source_account_id, AgentId=summary["AgentId"])
        except ClientError as e:
            inventory["errors"].append({"context": f"describe_agent({summary['AgentId']})", "user_message": classify_error(e)["user_message"]})
            continue
        agent = detail["Agent"]
        linked = []
        for space_id in space_ids:
            space_arn = f"arn:aws:quicksight:{region}:{source_account_id}:space/{space_id}"
            if space_arn in agent.get("Spaces", []):
                linked.append(space_id)
        if linked:
            inventory["agents"].append({
                "agent_id": agent["AgentId"], "name": agent["Name"], "linked_spaces": linked,
                "connectors": [a.split("/")[-1] for a in agent.get("ActionConnectors", [])],
                "has_instructions": bool(agent.get("CustomPromptInterface", {}).get("CustomInstructions")),
            })
            for arn in agent.get("ActionConnectors", []):
                inventory["connectors"].add(arn.split("/")[-1])

    # Reverse maps: which spaces reference each connector / KB (via space resources)
    conn_to_spaces = {}
    kb_to_spaces = {}
    for sp in inventory["spaces"]:
        for cid in sp.get("connectors", []):
            conn_to_spaces.setdefault(cid, []).append(sp["space_id"])
        for kid in sp.get("knowledge_bases", []):
            kb_to_spaces.setdefault(kid, []).append(sp["space_id"])

    # Describe connectors for name/type
    for connector_id in inventory["connectors"]:
        try:
            src_cfg = source_qs.describe_action_connector(AwsAccountId=source_account_id, ActionConnectorId=connector_id)
            c = src_cfg.get("ActionConnector", src_cfg)
            connector_details[connector_id] = {"connector_id": connector_id, "name": c.get("Name", connector_id), "type": c.get("Type", "UNKNOWN"),
                                               "linked_to_spaces": conn_to_spaces.get(connector_id, [])}
        except ClientError as e:
            inventory["errors"].append({"context": f"describe_action_connector({connector_id})", "user_message": classify_error(e)["user_message"]})
            connector_details[connector_id] = {"connector_id": connector_id, "name": connector_id, "type": "UNKNOWN (failed to describe)",
                                               "linked_to_spaces": conn_to_spaces.get(connector_id, [])}

    # Describe knowledge bases for name/type
    for kb_id in inventory["knowledge_bases"]:
        try:
            kb = source_qs.describe_knowledge_base(AwsAccountId=source_account_id, KnowledgeBaseId=kb_id).get("KnowledgeBase", {})
            kb_details[kb_id] = {"knowledge_base_id": kb_id, "name": kb.get("Name", kb_id), "type": kb.get("Type", "UNKNOWN"),
                                 "status": kb.get("Status", "UNKNOWN"),
                                 "linked_to_spaces": kb_to_spaces.get(kb_id, [])}
        except ClientError as e:
            inventory["errors"].append({"context": f"describe_knowledge_base({kb_id})", "user_message": classify_error(e)["user_message"]})
            kb_details[kb_id] = {"knowledge_base_id": kb_id, "name": kb_id, "type": "UNKNOWN (failed to describe)",
                                 "linked_to_spaces": kb_to_spaces.get(kb_id, [])}

    inventory["connectors"] = list(connector_details.values())
    inventory["knowledge_bases"] = list(kb_details.values())
    inventory["status"] = "OK" if not inventory["errors"] else "COMPLETED_WITH_ERRORS"

    logger.info(f"[TOOL] preview_migration done: {len(inventory['spaces'])} spaces, {len(inventory['agents'])} agents, "
                f"{len(inventory['connectors'])} connectors, {len(inventory['knowledge_bases'])} KBs, {len(inventory['errors'])} errors")
    return json.dumps(inventory, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════
# CLI MODE (for local testing)
# ═══════════════════════════════════════════════════════════════════

def cli_mode():
    print("=" * 60)
    print("  Quick Space Migrator — Local CLI Mode")
    print("=" * 60)
    print(f"\n  SOURCE_ROLE_ARN: {SOURCE_ROLE_ARN or '(using local creds)'}")
    print(f"  TARGET_ROLE_ARN: {TARGET_ROLE_ARN or '(using local creds)'}\n")

    action = input("Action [migrate / preview]: ").strip().lower()
    source_account = input("Source account ID: ").strip()
    spaces = input("Spaces [all / comma-separated IDs]: ").strip() or "all"
    region = input("Region [us-east-1]: ").strip() or "us-east-1"

    if action == "preview":
        print("\n⏳ Reading source...\n")
        print(preview_migration(source_account, spaces, region))
    else:
        target_account = input("Target account ID: ").strip()
        source_env = input("Source env [dev]: ").strip() or "dev"
        target_env = input("Target env [prod]: ").strip() or "prod"
        qs_role = input(f"QuickSight service role [{DEFAULT_QS_SERVICE_ROLE}]: ").strip() or DEFAULT_QS_SERVICE_ROLE
        print("\n⏳ Migrating...\n")
        print(migrate_spaces(source_account, target_account, spaces, region, source_env, target_env, qs_role))
    print("\n✅ Done.")
    sys.exit(0)


# ═══════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--cli" in sys.argv:
        cli_mode()
    else:
        mcp.run(transport="streamable-http")
