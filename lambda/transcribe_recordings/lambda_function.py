import json
import os
import logging
import time
import re
from typing import Dict, Any
from urllib.parse import unquote_plus
import boto3
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

OUTPUT_BUCKET = get_env_var("VOICEMAIL_BUCKET")
RECORDING_BUCKET_FOLDER = get_env_var("RECORDINGS_FOLDER").rstrip('/')

# Initialize AWS clients outside handler for reuse
s3_client = boto3.client('s3')
transcribe_client = boto3.client('transcribe')

# Constants
MAX_JOB_NAME_LENGTH = 200
JOB_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+$')
ALLOWED_EXTENSIONS = {'.wav', '.mp3', '.flac', '.ogg', '.mp4', '.webm'}


def validate_job_name(name: str) -> bool:
    """Ensure Transcribe job name meets requirements"""
    return (
        len(name) <= MAX_JOB_NAME_LENGTH and
        JOB_NAME_PATTERN.match(name) is not None
    )


def extract_recording_details(record: Dict[str, Any]) -> Dict[str, str]:
    """Extract and validate recording details from S3 event record"""
    try:
        recording_key = unquote_plus(record['s3']['object']['key'])
        recording_bucket = record['s3']['bucket']['name']

        if not recording_key.startswith(RECORDING_BUCKET_FOLDER + '/'):
            raise ValueError(f"Key not in recordings folder: {recording_key}")

        filename = os.path.basename(recording_key)
        contact_id, extension = os.path.splitext(filename)

        if extension.lower() not in ALLOWED_EXTENSIONS:
            raise ValueError(f"Unsupported file type: {extension}")

        media_uri = f"s3://{recording_bucket}/{recording_key}"

        return {
            'contact_id': contact_id,
            'media_uri': media_uri,
            'filename': filename
        }
    except KeyError as e:
        raise ValueError(f"Missing expected key in event: {e}") from e


def start_transcription_job(contact_id: str, media_uri: str) -> Dict[str, Any]:
    """Start Transcribe job with valid job name and configuration"""
    timestamp = str(int(time.time()))
    base_name = f"{contact_id}_{timestamp}"

    job_name = base_name.replace(' ', '_')[:MAX_JOB_NAME_LENGTH]
    if not validate_job_name(job_name):
        job_name = re.sub(r'[^a-zA-Z0-9._-]', '_', job_name)[:MAX_JOB_NAME_LENGTH]

    logger.info("Starting transcription job: %s", job_name)

    return transcribe_client.start_transcription_job(
        TranscriptionJobName=job_name,
        LanguageCode='en-US',
        MediaFormat=os.path.splitext(media_uri)[1][1:],
        Media={'MediaFileUri': media_uri},
        OutputBucketName=OUTPUT_BUCKET,
        OutputKey=f'transcriptions/{job_name}.json',
        Settings={
            'ShowSpeakerLabels': True,
            'MaxSpeakerLabels': 3
        }
    )


def is_already_trimmed(bucket: str, key: str) -> bool:
    """Check if recording has already been trimmed to avoid duplicate processing"""
    try:
        response = s3_client.get_object_tagging(Bucket=bucket, Key=key)
        tags = {tag['Key']: tag['Value'] for tag in response['TagSet']}
        return tags.get('Trimmed', '').lower() == 'true'
    except (ClientError, BotoCoreError) as e:
        logger.warning("Could not retrieve tags for %s/%s: %s", bucket, key, e)
        return False


def lambda_handler(event, context) -> Dict[str, Any]:
    """Handle new voicemail recordings by starting transcription jobs"""
    logger.info("Received event: %s", json.dumps(event, default=str))

    try:
        for record in event.get('Records', []):
            try:
                details = extract_recording_details(record)
                contact_id = details['contact_id']
                media_uri = details['media_uri']

                recording_bucket = record['s3']['bucket']['name']
                recording_key = unquote_plus(record['s3']['object']['key'])
                if is_already_trimmed(recording_bucket, recording_key):
                    logger.info("Skipping already trimmed recording: %s", contact_id)
                    continue

                logger.info("Processing recording: %s", contact_id)

                response = start_transcription_job(contact_id, media_uri)
                logger.info(
                    "Transcribe job started: %s",
                    response['TranscriptionJob']['TranscriptionJobName']
                )

            except ValueError as e:
                logger.error("Validation error: %s", e)
            except (ClientError, BotoCoreError) as e:
                logger.error("AWS service error: %s", e)
            except Exception as e:
                logger.exception("Processing failed: %s", e)

        return {
            'statusCode': 200,
            'body': 'Processing initiated successfully'
        }

    except Exception as e:
        logger.exception("Unhandled exception: %s", e)
        return {
            'statusCode': 500,
            'body': 'Internal server error'
        }