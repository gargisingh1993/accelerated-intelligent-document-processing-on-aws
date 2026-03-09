# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lambda function for user management operations with DynamoDB storage and Cognito sync.

Supports four roles (Cognito groups): Admin, Author, Reviewer, Viewer.
Users can optionally have allowedConfigVersions for config-version scoping.
"""

import logging
import os
import re
import uuid
from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
cognito = boto3.client("cognito-idp")

USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
AUTHOR_GROUP = os.environ.get("AUTHOR_GROUP", "Author")
REVIEWER_GROUP = os.environ.get("REVIEWER_GROUP", "Reviewer")
VIEWER_GROUP = os.environ.get("VIEWER_GROUP", "Viewer")
ALLOWED_SIGNUP_EMAIL_DOMAINS = os.environ.get("ALLOWED_SIGNUP_EMAIL_DOMAINS", "")

# Valid personas map to Cognito group names
VALID_PERSONAS = {
    "Admin": ADMIN_GROUP,
    "Author": AUTHOR_GROUP,
    "Reviewer": REVIEWER_GROUP,
    "Viewer": VIEWER_GROUP,
}


def handler(event, context):
    """Handle user management operations from AppSync."""
    logger.info(f"Received event: {event}")

    field = event.get("info", {}).get("fieldName", "")
    arguments = event.get("arguments", {})

    if field == "createUser":
        return create_user(arguments)
    elif field == "deleteUser":
        return delete_user(arguments)
    elif field == "listUsers":
        return list_users()

    raise ValueError(f"Unknown operation: {field}")


def create_user(args):
    """Create user in DynamoDB and sync to Cognito."""
    email = args["email"]
    persona = args["persona"]
    allowed_config_versions = args.get("allowedConfigVersions")
    user_id = str(uuid.uuid4())

    # Validate email format
    email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if not re.match(email_pattern, email):
        raise ValueError(f"Invalid email format: {email}")

    # Validate email domain if restrictions are configured
    if ALLOWED_SIGNUP_EMAIL_DOMAINS and ALLOWED_SIGNUP_EMAIL_DOMAINS.strip():
        allowed_domains = [
            d.strip().lower() for d in ALLOWED_SIGNUP_EMAIL_DOMAINS.split(",") if d.strip()
        ]
        if allowed_domains:  # Only validate if there are actual domains configured
            if "@" not in email:
                raise ValueError(f"Invalid email format: {email}")
            email_domain = email.split("@")[1].lower()
            if email_domain not in allowed_domains:
                raise ValueError(
                    f"Email domain '{email_domain}' is not allowed. "
                    f"Allowed domains: {', '.join(allowed_domains)}"
                )

    # Validate persona - support all four roles
    if persona not in VALID_PERSONAS:
        raise ValueError(
            f"Invalid persona: {persona}. Must be one of: {', '.join(VALID_PERSONAS.keys())}"
        )

    logger.info(f"Creating user with email {email} and persona {persona}")

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Check if user already exists
    existing_users = table.query(
        IndexName="EmailIndex", KeyConditionExpression=Key("email").eq(email)
    )

    if existing_users.get("Items"):
        raise ValueError(f"User with email {email} already exists")

    # Create user record in DynamoDB
    user_record = {
        "PK": f"USER#{user_id}",
        "SK": f"USER#{user_id}",
        "userId": user_id,
        "email": email,
        "persona": persona,
        "status": "active",
        "createdAt": datetime.utcnow().isoformat() + "Z",
        "updatedAt": datetime.utcnow().isoformat() + "Z",
    }

    # Store allowedConfigVersions if provided (Phase 2 - config-version scoping)
    if allowed_config_versions is not None:
        user_record["allowedConfigVersions"] = allowed_config_versions

    table.put_item(Item=user_record)

    # Sync to Cognito
    try:
        sync_user_to_cognito(user_id, email, persona, "create")
    except Exception as e:
        logger.error(f"Failed to sync user to Cognito: {e}")
        # Rollback DynamoDB record
        table.delete_item(Key={"PK": f"USER#{user_id}", "SK": f"USER#{user_id}"})
        raise e

    logger.info(f"User {email} created successfully")
    result = {
        "userId": user_id,
        "email": email,
        "persona": persona,
        "status": "active",
        "createdAt": user_record["createdAt"],
    }
    if allowed_config_versions is not None:
        result["allowedConfigVersions"] = allowed_config_versions
    return result


def delete_user(args):
    """Delete user from DynamoDB and sync to Cognito."""
    user_id = args["userId"]

    logger.info(f"Deleting user {user_id}")

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Get user record
    response = table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"USER#{user_id}"})

    if not response.get("Item"):
        raise ValueError(f"User {user_id} not found")

    user_record = response["Item"]
    email = user_record["email"]

    # Delete from DynamoDB
    table.delete_item(Key={"PK": f"USER#{user_id}", "SK": f"USER#{user_id}"})

    # Sync to Cognito
    try:
        sync_user_to_cognito(user_id, email, user_record["persona"], "delete")
    except Exception as e:
        logger.warning(f"Failed to sync user deletion to Cognito: {e}")
        # Continue with deletion as DynamoDB is the source of truth

    logger.info(f"User {user_id} deleted successfully")
    return True


def format_datetime(dt_str):
    """Ensure datetime string is valid ISO 8601 with Z suffix for AppSync."""
    if not dt_str:
        return None
    # Remove any existing timezone offset (+00:00) and trailing Z
    dt_str = dt_str.replace("+00:00", "").rstrip("Z")
    return dt_str + "Z"


def _determine_persona_from_groups(groups):
    """Determine persona from Cognito groups, using highest precedence."""
    group_names = [g["GroupName"] for g in groups]
    # Check in precedence order
    if ADMIN_GROUP in group_names:
        return "Admin"
    if AUTHOR_GROUP in group_names:
        return "Author"
    if REVIEWER_GROUP in group_names:
        return "Reviewer"
    if VIEWER_GROUP in group_names:
        return "Viewer"
    # Default to Viewer for users with no recognized group
    return "Viewer"


def list_users():
    """List all users - sync from Cognito first, then return from DynamoDB."""
    logger.info("Listing all users")

    # First, sync Cognito users to DynamoDB
    sync_cognito_users_to_dynamodb()

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Scan for all user records
    response = table.scan(
        FilterExpression="begins_with(PK, :pk_prefix)",
        ExpressionAttributeValues={":pk_prefix": "USER#"},
    )

    users = []
    for item in response.get("Items", []):
        user = {
            "userId": item["userId"],
            "email": item["email"],
            "persona": item["persona"],
            "status": item.get("status", "active"),
            "createdAt": format_datetime(item.get("createdAt")),
        }
        # Include allowedConfigVersions if present
        if "allowedConfigVersions" in item:
            user["allowedConfigVersions"] = item["allowedConfigVersions"]
        users.append(user)

    # Sort by creation date (newest first)
    users.sort(key=lambda x: x.get("createdAt") or "", reverse=True)

    logger.info(f"Found {len(users)} users")
    return {"users": users}


def sync_cognito_users_to_dynamodb():
    """Sync existing Cognito users to DynamoDB table."""
    logger.info("Syncing Cognito users to DynamoDB")

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Get existing emails in DynamoDB for quick lookup
    existing_response = table.scan(
        FilterExpression="begins_with(PK, :pk_prefix)",
        ExpressionAttributeValues={":pk_prefix": "USER#"},
        ProjectionExpression="email",
    )
    existing_emails = {item["email"] for item in existing_response.get("Items", [])}

    # List all Cognito users
    paginator = cognito.get_paginator("list_users")

    for page in paginator.paginate(UserPoolId=USER_POOL_ID):
        for user in page.get("Users", []):
            username = user["Username"]

            # Get email from attributes
            email = username
            for attr in user.get("Attributes", []):
                if attr["Name"] == "email":
                    email = attr["Value"]
                    break

            # Skip if already in DynamoDB
            if email in existing_emails:
                continue

            # Get user's groups to determine persona
            try:
                groups_response = cognito.admin_list_groups_for_user(
                    Username=username, UserPoolId=USER_POOL_ID
                )
                persona = _determine_persona_from_groups(
                    groups_response.get("Groups", [])
                )
            except Exception as e:
                logger.warning(f"Could not get groups for user {username}: {e}")
                persona = "Viewer"

            # Create user record in DynamoDB
            user_id = str(uuid.uuid4())
            if user.get("UserCreateDate"):
                # Convert to UTC and format without timezone offset
                dt = user["UserCreateDate"]
                created_at = dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
            else:
                created_at = datetime.utcnow().isoformat() + "Z"

            user_record = {
                "PK": f"USER#{user_id}",
                "SK": f"USER#{user_id}",
                "userId": user_id,
                "email": email,
                "persona": persona,
                "status": "active",
                "createdAt": created_at,
                "updatedAt": datetime.utcnow().isoformat() + "Z",
            }

            table.put_item(Item=user_record)
            logger.info(f"Synced Cognito user {email} to DynamoDB")


def sync_user_to_cognito(user_id, email, persona, operation):
    """Sync user operations to Cognito."""
    if operation == "create":
        # Create user in Cognito
        cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )

        # Add to appropriate Cognito group based on persona
        group_name = VALID_PERSONAS.get(persona)
        if group_name:
            cognito.admin_add_user_to_group(
                UserPoolId=USER_POOL_ID, Username=email, GroupName=group_name
            )
            logger.info(f"User {email} synced to Cognito and added to group {group_name}")
        else:
            logger.warning(f"Unknown persona '{persona}' - user created without group assignment")

    elif operation == "delete":
        # Delete user from Cognito
        try:
            cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=email)
            logger.info(f"User {email} deleted from Cognito")
        except cognito.exceptions.UserNotFoundException:
            logger.warning(f"User {email} not found in Cognito during deletion")
