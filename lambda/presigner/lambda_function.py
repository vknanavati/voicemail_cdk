import json
import logging
import os
from typing import Dict, Any
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError, BotoCoreError

# Configure structured logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())

# Environment variables with validation
def get_env_var(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

BUCKET_NAME = get_env_var("DESTINATION_BUCKET")
AWS_REGION = get_env_var("REGION")
AUDIO_BUCKET_FOLDER = get_env_var("RECORDINGS_FOLDER").rstrip('/')
SECRET_NAME = get_env_var("SECRET_NAME")


def get_secret_credentials():
    """Fetch IAM user credentials from Secrets Manager"""
    sm_client = boto3.client("secretsmanager", region_name=AWS_REGION)
    secret_value = sm_client.get_secret_value(SecretId=SECRET_NAME)
    secret_dict = json.loads(secret_value["SecretString"])
    return secret_dict["presigned_url_account_AK"], secret_dict["presigned_url_account_SAK"]

def create_s3_client_with_user_credentials(access_key, secret_key):
    """Create S3 client using IAM user credentials"""
    session = boto3.session.Session()
    return session.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=AWS_REGION,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 10, "mode": "standard"},
        ),
    )

def extract_contact_id(event: Dict[str, Any]) -> str:
    """Extract contact ID from various event formats"""
    # Try Connect contact flow format
    try:
        contact_data = event['Details']['ContactData']
        return contact_data['Attributes']['contact_id']
    except KeyError:
        pass

    # Try direct invocation format
    if 'contact_id' in event:
        return event['contact_id']

    # Fallback to attribute scan
    for key in ['ContactId', 'contactId']:
        if key in event.get('Details', {}).get('ContactData', {}).get('Attributes', {}):
            return event['Details']['ContactData']['Attributes'][key]

    raise ValueError("No contact_id found in event payload")

def generate_presigned_url(contact_id: str, s3_client) -> str:
    """Generate presigned URL for voicemail recording"""
    recording_key = f"{AUDIO_BUCKET_FOLDER}/{contact_id}.wav"
    logger.info("Generating URL for: %s", recording_key)

    return s3_client.generate_presigned_url(
        ClientMethod='get_object',
        Params={
            'Bucket': BUCKET_NAME,
            'Key': recording_key,
            'ResponseContentDisposition': 'inline',
            'ResponseContentType': 'audio/wav'
        },
        ExpiresIn=604800  # 7 days
    )

def lambda_handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """Handle voicemail URL generation requests"""
    logger.info("Received event: %s", json.dumps(event, default=str))
    response = {'result': 'success'}

    try:
        logger.info("Initialize S3 client")
        access_key, secret_key = get_secret_credentials()
        s3_client = create_s3_client_with_user_credentials(access_key, secret_key)
        logger.info("S3 client initialized with IAM user credentials")
    except Exception as e:
        logger.error(f"S3 client initialization failed: {e}")
        return {
            'result': 'error',
            'errorType': 'ServiceError',
            'errorMessage': 'S3 client initialization failed'
        }
    try:
        contact_id = extract_contact_id(event)
        logger.info("Processing contact: %s", contact_id)

        presigned_url = generate_presigned_url(contact_id, s3_client)

        logger.info("Presigned URL: %s", presigned_url)

        response['presigned_url'] = presigned_url
        logger.info("Generated presigned URL")

        return response

    except ValueError as e:
        logger.error("Validation error: %s", str(e))
        return {
            'result': 'error',
            'errorType': 'ValidationError',
            'errorMessage': str(e)
        }
    except (ClientError, BotoCoreError) as e:
        logger.error("AWS service error: %s", e)
        return {
            'result': 'error',
            'errorType': 'ServiceError',
            'errorMessage': 'Failed to generate presigned URL'
        }
    except Exception as e:
        logger.exception("Unexpected error: %s", e)
        return {
            'result': 'error',
            'errorType': 'InternalError',
            'errorMessage': 'Internal processing failure'
        }