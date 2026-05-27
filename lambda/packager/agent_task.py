import json
import os
import logging
import boto3
from botocore.exceptions import ClientError, BotoCoreError

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


INSTANCE_ID = get_env_var("CONNECT_INSTANCE_ID")
TASK_TEMPLATE_ID = get_env_var("TASK_TEMPLATE_ID")

connect_client = boto3.client('connect')

# Constants for task configuration
TASK_NAME = "VoicemailTemplate"
DESCRIPTION_TEMPLATE = "Voicemail for {}"


def build_task_references(payload: dict) -> dict:
    """Construct references dictionary for task - matches task template field names exactly"""
    return {
        "Transcript Of Voicemail": {
            "Value": payload['voicemail_transcript'],
            "Type": "STRING"
        },
        "Click Link To Listen To Voicemail": {
            "Value": payload['presigned_url'],
            "Type": "URL"
        },
        "Customer Number": {
            "Value": f"tel:{payload['customer_number']}",
            "Type": "STRING"
        },
        "Voicemail Created On": {
            "Value": payload['Date_created'],
            "Type": "STRING"
        }
    }


def build_task_attributes(payload: dict) -> dict:
    """Construct attributes dictionary for task"""
    attributes = {
        "DestinationType": payload.get('destination_type', 'Agent'),
        'agent_name': payload.get('agent_name', 'N/A'),
        'voicemail_queue': payload.get('agent_arn', ''),
        "contact_id": payload['contact_id'],
        "customer_number": payload['customer_number'],
        "presigned_url": payload['presigned_url'],
        "transcript": payload['voicemail_transcript'][:4096] + "..."
        if len(payload['voicemail_transcript']) > 4096
        else payload['voicemail_transcript']
    }

    # Add optional fields if available
    if 'agent_email' in payload:
        attributes["agent_email"] = payload['agent_email']

    return attributes


def connect_agent_task(payload: dict) -> dict:
    """Create Connect task for voicemail notification"""
    try:
        logger.info("Creating agent task for voicemail")

        # Build task components
        references = build_task_references(payload)
        attributes = build_task_attributes(payload)

        # Prepare task parameters
        task_params = {
            "InstanceId": INSTANCE_ID,
            "Name": TASK_NAME,
            "Description": DESCRIPTION_TEMPLATE.format(
                payload.get('agent_name', 'Agent')
            ),
            "TaskTemplateId": TASK_TEMPLATE_ID,
            "References": references,
            "Attributes": attributes
        }

        logger.info("Task parameters: %s", json.dumps(task_params, indent=2))

        # Create task in Amazon Connect
        response = connect_client.start_task_contact(
            InstanceId=INSTANCE_ID,
            Name=TASK_NAME,
            Description=task_params['Description'],
            TaskTemplateId=task_params['TaskTemplateId'],
            References=task_params['References'],
            Attributes=task_params['Attributes']
        )

        logger.info("Task created successfully: %s", response['ContactId'])
        return {
            'status': 'success',
            'contact_id': response['ContactId']
        }

    except (ValueError, ClientError, BotoCoreError) as e:
        logger.error("Task creation failed: %s", e)
        return {
            'status': 'error',
            'error_type': type(e).__name__,
            'message': str(e)
        }
    except Exception as e:
        logger.exception("Unexpected error in task creation: %s", e)
        return {
            'status': 'error',
            'error_type': 'InternalError',
            'message': 'An unexpected error occurred'
        }