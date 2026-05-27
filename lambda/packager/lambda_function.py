import io
import json
import os
import logging
from datetime import datetime, timezone
import wave
from typing import Any, Dict, Optional, List
import boto3
from botocore.exceptions import ClientError, BotoCoreError
import agent_task
import queue_task

# Configure structured logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())


def get_env_var(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


VOICEMAIL_FOLDER = get_env_var("RECORDINGS_FOLDER").rstrip('/')
RECORDING_BUCKET = get_env_var("DESTINATION_BUCKET")
REGION = get_env_var("REGION")
PRESIGNER_FUNCTION = get_env_var("PRESIGNER_FUNCTION")

# Initialize AWS clients outside handler
connect_client = boto3.client('connect')
lambda_client = boto3.client('lambda')
s3_client = boto3.client('s3')
transcribe_client = boto3.client('transcribe')

PHRASE_TRIGGERS = (
    "hang up at anytime",
    "hang up at any time",
    "colgar en cualquier momento",
    "please leave a message after the beep",
)


def extract_transcript_details(event: dict) -> dict:
    """Extract and validate transcript processing details"""
    try:
        s3_record = event['Records'][0]['s3']
        transcript_key = s3_record['object']['key']

        # Skip write test files
        if '.write_access_check_file.temp' in transcript_key:
            raise ValueError("Write test file ignored")

        transcript_bucket = s3_record['bucket']['name']
        job_name = os.path.splitext(transcript_key)[0]
        logger.info("job_name: %s", job_name)
        contact_id = job_name.split('_')[0].split('/')[1]
        logger.info("contact_id: %s", contact_id)
        recording_key = f"{VOICEMAIL_FOLDER}/{contact_id}.wav"

        return {
            'transcript_key': transcript_key,
            'transcript_bucket': transcript_bucket,
            'contact_id': contact_id,
            'recording_key': recording_key
        }
    except (KeyError, IndexError) as e:
        raise ValueError(f"Invalid event structure: {e}") from e


def get_recording_tags(recording_key: str) -> dict:
    """Retrieve and validate recording tags from S3"""
    try:
        response = s3_client.get_object_tagging(
            Bucket=RECORDING_BUCKET,
            Key=recording_key
        )
        tags = {tag['Key']: tag['Value'] for tag in response['TagSet']}

        return tags
    except (ClientError, BotoCoreError) as e:
        if e.response.get('Error', {}).get('Code') == 'NoSuchKey':
            raise ValueError(f"Recording not found: {recording_key}") from e
        raise


def load_transcript_data(bucket: str, key: str) -> Dict[str, Any]:
    """Retrieve transcript JSON from S3"""
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read().decode('utf-8')
        return json.loads(content)
    except (ClientError, BotoCoreError) as e:
        logger.error("Failed to load transcript %s/%s: %s", bucket, key, e)
        raise
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid transcript JSON: {e}") from e


def extract_transcript_text(transcript: Dict[str, Any]) -> str:
    """Extract raw transcript text"""
    try:
        return transcript['results']['transcripts'][0]['transcript']
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"Invalid transcript format: {e}") from e


def _normalize_token(token: str) -> str:
    return ''.join(ch for ch in token.lower() if ch.isalnum() or ch == "'")


def _tokenize_phrase(phrase: str) -> List[str]:
    return [
        token
        for token in (_normalize_token(part) for part in phrase.split())
        if token
    ]


def locate_phrase_offset(transcript: Dict[str, Any]) -> Optional[float]:
    """Locate the end time of the configured phrase within the transcript"""
    items = transcript.get("results", {}).get("items", [])
    if not items:
        logger.info("Transcript missing timed items; skipping trim.")
        return None
    pronunciations = []

    for item in items:
        if item.get("type") != "pronunciation":
            continue
        alternatives = item.get("alternatives", [])
        if not alternatives:
            continue
        content = alternatives[0].get("content", "")
        normalized = _normalize_token(content)
        if not normalized:
            continue
        try:
            start_time = float(item["start_time"])
            end_time = float(item.get("end_time", start_time))
        except (KeyError, ValueError, TypeError):
            continue
        pronunciations.append((normalized, start_time, end_time))

    best_match: Optional[tuple[float, str]] = None

    for phrase in PHRASE_TRIGGERS:
        tokens = _tokenize_phrase(phrase)
        if not tokens:
            continue

        target_length = len(tokens)
        for idx in range(len(pronunciations) - target_length + 1):
            if all(
                pronunciations[idx + offset][0] == tokens[offset]
                for offset in range(target_length)
            ):
                _, _, end_time = pronunciations[idx + target_length - 1]
                if not best_match or end_time < best_match[0]:
                    best_match = (end_time, phrase)
                break

    if best_match:
        end_time, phrase = best_match
        logger.info(
            "Detected phrase '%s'; trimming voicemail after %.2fs",
            phrase,
            end_time,
        )
        return end_time

    logger.info(
        "Phrase(s) %s not present in transcript; serving full voicemail.",
        ", ".join(PHRASE_TRIGGERS),
    )
    return None


def extract_transcript_after_offset(transcript: Dict[str, Any], offset: float) -> str:
    """Reconstruct transcript text beginning after the specified offset"""
    results = transcript.get("results", {})
    items = results.get("items", [])
    if not items:
        return extract_transcript_after_phrase_text(transcript)

    include = False
    words = []
    for item in items:
        item_type = item.get("type")
        if item_type == "pronunciation":
            try:
                start_time = float(item["start_time"])
            except (KeyError, ValueError, TypeError):
                continue
            if start_time >= offset:
                include = True
            if include:
                token = item.get("alternatives", [{}])[0].get("content", "")
                if token:
                    words.append(token)
        elif item_type == "punctuation" and include:
            token = item.get("alternatives", [{}])[0].get("content", "")
            if token:
                if words:
                    words[-1] = f"{words[-1]}{token}"
                else:
                    words.append(token)

    if not words:
        return extract_transcript_after_phrase_text(transcript)

    return " ".join(words)


def extract_transcript_after_phrase_text(transcript: Dict[str, Any]) -> str:
    """Fallback trimming using raw transcript string"""
    raw_text = extract_transcript_text(transcript)
    lowered = raw_text.lower()
    earliest_idx: Optional[int] = None
    matched_phrase: Optional[str] = None

    for phrase in PHRASE_TRIGGERS:
        target = phrase.lower()
        idx = lowered.find(target)
        if idx == -1:
            continue
        if earliest_idx is None or idx < earliest_idx:
            earliest_idx = idx
            matched_phrase = phrase

    if earliest_idx is None or matched_phrase is None:
        return raw_text
    return raw_text[earliest_idx + len(matched_phrase):].lstrip(" :.-")


def trim_recording(recording_key: str, start_time: float, tags: Dict[str, str]) -> bool:
    """Trim the voicemail audio starting at the specified timestamp

    NOTE: Python 3.13+ removed audioop module, so only uncompressed WAV files are supported.
    Amazon Connect typically uses uncompressed WAV, so this should work for most cases.
    """
    try:
        response = s3_client.get_object(Bucket=RECORDING_BUCKET, Key=recording_key)
        audio_bytes = response['Body'].read()
    except (ClientError, BotoCoreError) as exc:
        logger.error("Unable to download original audio %s: %s", recording_key, exc)
        return False

    audio_stream = io.BytesIO(audio_bytes)
    try:
        with wave.open(audio_stream, "rb") as reader:
            params = reader.getparams()
            frame_rate = reader.getframerate()
            total_frames = reader.getnframes()
            start_frame = int(max(start_time, 0) * frame_rate)

            if start_frame >= total_frames:
                logger.warning(
                    "Trim start %.2fs exceeds audio length for %s",
                    start_time,
                    recording_key,
                )
                return False

            reader.setpos(start_frame)
            frames_to_write = reader.readframes(total_frames - start_frame)
            if not frames_to_write:
                logger.warning("No frames remain after trimming for %s", recording_key)
                return False

            compression = (params.comptype or "").lower()

            # Check for compression - audioop was removed in Python 3.13+
            if compression not in ("none", ""):
                logger.warning(
                    "Compressed audio (%s) detected for %s. "
                    "Audio trimming skipped - Python 3.13+ removed audioop support. "
                    "Amazon Connect typically uses uncompressed WAV.",
                    compression,
                    recording_key
                )
                return False

            output_stream = io.BytesIO()
            with wave.open(output_stream, "wb") as writer:
                writer.setnchannels(params.nchannels)
                writer.setframerate(params.framerate)
                writer.setsampwidth(params.sampwidth)
                writer.writeframes(frames_to_write)

            output_stream.seek(0)
    except wave.Error as exc:
        logger.error("Wave processing failed for %s: %s", recording_key, exc)
        return False

    try:
        s3_client.put_object(
            Bucket=RECORDING_BUCKET,
            Key=recording_key,
            Body=output_stream.getvalue(),
            ContentType="audio/wav",
        )
    except (ClientError, BotoCoreError) as exc:
        logger.error("Unable to upload trimmed audio %s: %s", recording_key, exc)
        return False

    updated_tags = dict(tags)
    updated_tags["Trimmed"] = "true"
    tag_set = [{'Key': k, 'Value': v} for k, v in updated_tags.items() if v]
    if tag_set:
        try:
            s3_client.put_object_tagging(
                Bucket=RECORDING_BUCKET,
                Key=recording_key,
                Tagging={'TagSet': tag_set}
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to reapply tags on trimmed audio %s: %s", recording_key, exc)

    logger.info("Uploaded trimmed audio for %s", recording_key)
    return True


def generate_presigned_url(contact_id: str, vmx3_mode: str) -> str:
    """Generate presigned URL via Lambda function"""
    try:
        logger.info("Generating presigned URL for contact: %s", contact_id)
        response = lambda_client.invoke(
            FunctionName=PRESIGNER_FUNCTION,
            InvocationType='RequestResponse',
            Payload=json.dumps({
                'recording_bucket': RECORDING_BUCKET,
                'contact_id': contact_id,
                'vmx3_mode': vmx3_mode,
                'region': REGION
            })
        )
        result = json.load(response['Payload'])
        return result.get('presigned_url', '')
    except (ClientError, BotoCoreError) as e:
        raise RuntimeError(f"Presigner invocation failed: {e}") from e


def format_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def process_voicemail(task_payload: dict):
    """Handle voicemail based on its type"""
    vmx_type = task_payload.get('VoicemailType')
    dest_type = task_payload.get("destination_type")
    logger.info("Processing voicemail - Type: %s, Destination: %s", vmx_type, dest_type)

    if vmx_type == 'task' and dest_type == "Agent":
        return agent_task.connect_agent_task(task_payload)
    if vmx_type == 'task' and dest_type == "Queue":
        return queue_task.connect_queue_task(task_payload)
    elif vmx_type == 'email':
        logger.info("Email voicemail processing would trigger here")
        return {'status': 'email_queued'}
    else:
        logger.error("Unsupported voicemail type: %s", vmx_type)
        return {'status': 'error', 'message': f'Unsupported voicemail type: {vmx_type}'}


def clean_ip_transcription_job(job_name: str) -> None:
    """Clean up transcription job artifacts"""
    try:
        transcribe_client.delete_transcription_job(TranscriptionJobName=job_name)
        logger.info("Deleted transcription job: %s", job_name)
    except ClientError as e:
        logger.error("Failed to delete transcription job: %s", e)


def lambda_handler(event, context) -> dict:
    """Process voicemail transcriptions and route appropriately"""
    logger.info("Received event: %s", json.dumps(event, default=str))

    try:
        # Extract transcript details
        details = extract_transcript_details(event)
        contact_id = details['contact_id']
        logger.info("Processing transcript for contact: %s", contact_id)

        # Retrieve recording tags
        tags = get_recording_tags(details['recording_key'])

        transcript_data = load_transcript_data(
            details['transcript_bucket'],
            details['transcript_key']
        )

        phrase_offset = locate_phrase_offset(transcript_data)

        # Trim audio once we have the transcript timing data
        if tags.get("Trimmed", "").lower() != "true":
            if phrase_offset and phrase_offset > 0:
                if trim_recording(details['recording_key'], phrase_offset, tags):
                    tags["Trimmed"] = "true"
            else:
                logger.info(
                    "Skip trimming for %s - phrase offset not detected",
                    contact_id,
                )
        else:
            logger.info("Recording already trimmed for %s", contact_id)

        # Build task payload with transcript processing
        transcript_text = extract_transcript_text(transcript_data)
        trimmed_fallback = extract_transcript_after_phrase_text(transcript_data)
        if phrase_offset and phrase_offset > 0:
            transcript_text = extract_transcript_after_offset(transcript_data, phrase_offset)
        elif trimmed_fallback != transcript_text:
            logger.info("Applied fallback transcript trimming for %s", contact_id)
            transcript_text = trimmed_fallback

        task_payload = {
            'vmQueueName': tags.get("vmQueueName", ''),
            'destination_type': tags.get("DestinationType", ''),
            'customer_number': tags.get("PhoneNumber", ''),
            'contact_id': tags.get("ContactId", ''),
            'voicemail_queue_arn': tags.get("VoicemailQueue", ''),
            'VoicemailType': tags.get("VoicemailType", ''),
            'voicemail_transcript': transcript_text,
            'Date_created': format_timestamp()
        }

        # Add optional fields if present in tags
        if 'AgentEmail' in tags:
            task_payload['agent_email'] = tags['AgentEmail']
        if 'AgentArn' in tags:
            task_payload['agent_arn'] = tags['AgentArn']
        if 'AgentName' in tags:
            task_payload['agent_name'] = tags['AgentName']

        logger.info("Task payload prepared (without transcript): %s",
                   json.dumps({k: v for k, v in task_payload.items()
                              if k != 'voicemail_transcript'}, default=str))

        # Generate presigned URL
        task_payload['presigned_url'] = generate_presigned_url(
            contact_id,
            tags.get("VoicemailType", 'task')
        )

        logger.info("DEBUG>>>>: Task Payload %s", json.dumps(task_payload, default=str))

        # Process based on voicemail type
        result = process_voicemail(task_payload)

        logger.info("Processing complete: %s", json.dumps(result, default=str))
        return {
            'statusCode': 200,
            'body': 'Voicemail processed successfully',
            'result': result
        }

    except ValueError as e:
        logger.error("Validation error: %s", e)
        return {
            'statusCode': 400,
            'body': f'Input validation failed: {str(e)}'
        }
    except (ClientError, BotoCoreError) as e:
        logger.error("AWS service error: %s", e)
        return {
            'statusCode': 500,
            'body': f'AWS service error: {e.response.get("Error", {}).get("Code", "Unknown")}'
        }
    except Exception as e:
        logger.exception("Unhandled exception: %s", e)
        return {
            'statusCode': 500,
            'body': 'Internal processing error'
        }