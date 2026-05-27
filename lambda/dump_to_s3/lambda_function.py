import logging
import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, Mapping, Optional
import boto3
from urllib.parse import unquote_plus
from botocore.exceptions import ClientError, BotoCoreError
from botocore.client import Config

# Set up structured logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def get_env_var(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# Environment variables
CONNECT_BUCKET = get_env_var("SOURCE_BUCKET")
DESTINATION_BUCKET = get_env_var("DESTINATION_BUCKET")
TABLE_NAME = get_env_var("TABLE_NAME")
RECORDINGS_FOLDER = get_env_var("RECORDINGS_FOLDER").rstrip('/')
INSTANCE_ID = get_env_var("CONNECT_INSTANCE_ID")

# Initialize AWS clients
s3_client = boto3.client("s3", config=Config(signature_version='s3v4'))
connect_client = boto3.client('connect')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)

# Pre-compile regex
CONTACT_ID_REGEX = re.compile(r"[\w]{8}-[\w]{4}-[\w]{4}-[\w]{4}-[\w]{12}")


def extract_contact_id(key: str) -> Optional[str]:
    """Extract contact ID from S3 object key"""
    if not key.lower().endswith(".wav"):
        return None
    match = CONTACT_ID_REGEX.search(key)
    return match.group(0) if match else None


@lru_cache(maxsize=500)
def get_agent_info(phone: str) -> Mapping[str, Any]:
    """Get agent info from DynamoDB with caching"""
    try:
        response = table.get_item(
            Key={"ID": phone},
            ProjectionExpression="AgentName, AgentARN"
        )
        return response["Item"] if "Item" in response else {}
    except (ClientError, BotoCoreError) as e:
        logger.error("DynamoDB Error for phone %s: %s", phone, e)
        return {}


def get_connect_attributes(contact_id: str) -> Dict[str, Any]:
    """Retrieve contact attributes from Amazon Connect"""
    try:
        response = connect_client.get_contact_attributes(
            InstanceId=INSTANCE_ID,
            InitialContactId=contact_id
        )
        attributes = response.get("Attributes", {})
        logger.info("Contact Attributes", extra={"attributes": attributes})

        return {
            "languageCode": attributes.get("LanguageCode"),
            "vmQueueName": attributes.get("vmQueueName"),
            "DestinationType": attributes.get("DestinationType"),
            "VoicemailType": attributes.get("voicemailType"),
            "phoneNumber": attributes.get("phoneNumber"),
            "callCenterNumber": attributes.get("callCenterNumber"),
            "voicemailQueue": attributes.get("voicemailQueue"),
            "isVoicemail": attributes.get("isVoicemail", "false").lower() == "true",
        }
    except (ClientError, BotoCoreError) as e:
        logger.error("Connect API error for %s: %s", contact_id, e)
        return {"isVoicemail": False}


def tag_s3_object(bucket: str, key: str, tags: Mapping[str, str]) -> bool:
    """Apply tags to S3 object with error handling"""
    try:
        tag_set = [{'Key': k, 'Value': v} for k, v in tags.items() if v]
        s3_client.put_object_tagging(
            Bucket=bucket,
            Key=key,
            Tagging={'TagSet': tag_set}
        )
        return True
    except (ClientError, BotoCoreError) as e:
        logger.error("Tagging failed for %s/%s: %s", bucket, key, e)
        return False


def key_exists(bucket: str, key: str) -> bool:
    """Check if S3 key exists"""
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def lambda_handler(event, context) -> Dict[str, Any]:
    logger.info("Processing %d records", len(event["Records"]))
    success_count = 0
    failure_count = 0

    for record in event["Records"]:
        try:
            # Skip non-creation events
            if record["eventName"] not in [
                "ObjectCreated:Put",
                "ObjectCreated:CompleteMultipartUpload"
            ]:
                continue

            # Process record
            src_bucket = record["s3"]["bucket"]["name"]
            src_key = unquote_plus(record["s3"]["object"]["key"])
            contact_id = extract_contact_id(src_key)

            if not contact_id:
                logger.warning("Contact ID not found in key: %s", src_key)
                failure_count += 1
                continue

            # Get Connect attributes
            attributes = get_connect_attributes(contact_id)
            if not attributes.get("isVoicemail"):
                logger.info("Skipping non-voicemail: %s", contact_id)
                continue
            if not attributes.get("callCenterNumber"):
                logger.warning("Missing callCenterNumber for: %s", contact_id)
                failure_count += 1
                continue

            # Default values (in case DestinationType is Queue)
            agent_name = "N/A"
            agent_arn = "N/A"

            # Only query DynamoDB if DestinationType is Agent
            destination_type = attributes.get("DestinationType")
            if destination_type == "Agent":
                agent_info = get_agent_info(attributes["callCenterNumber"])
                agent_name = agent_info.get("AgentName", "N/A")
                agent_arn = agent_info.get("AgentARN", "N/A")
            else:
                logger.info("Skipping DynamoDB lookup (DestinationType=%s)", destination_type)

            # Prepare destination
            dest_key = f"{RECORDINGS_FOLDER}/{contact_id}.wav"

            if key_exists(DESTINATION_BUCKET, dest_key):
                logger.info("Skipping. Object exists: %s", dest_key)
                continue

            # Copy file
            s3_client.copy_object(
                Bucket=DESTINATION_BUCKET,
                CopySource={"Bucket": src_bucket, "Key": src_key},
                Key=dest_key,
                MetadataDirective="COPY"
            )

            # Create tags
            tags = {
                "ContactId": contact_id,
                "vmQueueName": attributes.get("vmQueueName", ""),
                "DestinationType": attributes.get("DestinationType", ""),
                "VoicemailType": attributes.get("VoicemailType", "unknown"),
                "VoicemailQueue": attributes.get("voicemailQueue", "unknown"),
                "PhoneNumber": attributes.get("phoneNumber", "unknown"),
                "CallCenterNumber": attributes.get("callCenterNumber", "unknown"),
                "AgentArn": agent_arn,
                "AgentName": agent_name,
                "LanguageCode": attributes.get("languageCode", "en-US"),
            }

            # Apply tags
            if tag_s3_object(DESTINATION_BUCKET, dest_key, tags):
                success_count += 1
                logger.info("Processed contact: %s", contact_id)
            else:
                failure_count += 1

        except Exception as e:
            failure_count += 1
            logger.exception("Failed to process record: %s", e)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Processing complete",
            "success": success_count,
            "failures": failure_count
        })
    }