import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as connect from 'aws-cdk-lib/aws-connect';
import * as path from 'path';

// ─────────────────────────────────────────────────────────────────────────────
// CONFIGURATION — update these values before deploying
// ─────────────────────────────────────────────────────────────────────────────
export class VoicemailStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // CONFIG goes here — after super(), this is now available
    const CONFIG = {
      connectInstanceId:           this.node.tryGetContext('connectInstanceId'),
      connectInstanceArn:          `arn:aws:connect:${this.region}:${this.account}:instance/${this.node.tryGetContext('connectInstanceId')}`,
      voicemailBucketName:         this.node.tryGetContext('voicemailBucketName'),
      connectRecordingsBucketName: this.node.tryGetContext('connectRecordingsBucketName'),
      agentPhoneLookupTable:       this.node.tryGetContext('agentPhoneLookupTable') ?? 'AgentPhoneLookup',
      secretName:                  this.node.tryGetContext('secretName') ?? 'voicemail-presigner-credentials',
      recordingsFolder:            'recordings',
      transcriptionsFolder:        'transcriptions',
      taskTemplateId:              this.node.tryGetContext('taskTemplateId') ?? 'REPLACE_AFTER_FIRST_DEPLOY',
      vmx01FlowArn:                this.node.tryGetContext('vmx01FlowArn') ?? '',
      basicQueueId:                this.node.tryGetContext('basicQueueId'),
      beepPromptId:                this.node.tryGetContext('beepPromptId'),
      musicPromptId:               this.node.tryGetContext('musicPromptId'),
      lambdaNames: {
        dumpToS3:   'master-demo-dump-to-s3',
        transcribe: 'master-demo-transcribe-recordings',
        presigner:  'master-demo-presigner',
        packager:   'master-demo-packager',
      },
    };
    const basicQueueArn = `arn:aws:connect:${this.region}:${this.account}:instance/${CONFIG.connectInstanceId}/queue/${CONFIG.basicQueueId}`;
    const beepPromptArn = `arn:aws:connect:${this.region}:${this.account}:instance/${CONFIG.connectInstanceId}/prompt/${CONFIG.beepPromptId}`;
    const musicPromptArn = `arn:aws:connect:${this.region}:${this.account}:instance/${CONFIG.connectInstanceId}/prompt/${CONFIG.musicPromptId}`;
// ─────────────────────────────────────────────────────────────────────────────

    // ── 1. S3 BUCKET ──────────────────────────────────────────────────────────
    // Import the existing voicemail bucket rather than creating a new one,
    // since it already exists in your account.
    const voicemailBucket = s3.Bucket.fromBucketName(
      this, 'VoicemailBucket', CONFIG.voicemailBucketName
    );

    // Import the Connect-managed recordings bucket (source of WAV files)
    const connectRecordingsBucket = s3.Bucket.fromBucketName(
      this, 'ConnectRecordingsBucket', CONFIG.connectRecordingsBucketName
    );

    // ── 2. DYNAMODB TABLE ─────────────────────────────────────────────────────
    // Import existing AgentPhoneLookup table
    const agentTable = dynamodb.Table.fromTableName(
      this, 'AgentPhoneLookupTable', CONFIG.agentPhoneLookupTable
    );

    // ── 3. SECRETS MANAGER ───────────────────────────────────────────────────
    // The presigner Lambda uses a dedicated IAM user whose credentials live
    // in Secrets Manager so it can generate long-lived presigned URLs.
    const presignerSecret = secretsmanager.Secret.fromSecretNameV2(
      this, 'PresignerSecret', CONFIG.secretName
    );

    // ── 4. IAM ROLES ──────────────────────────────────────────────────────────

    // ── 4a. Shared base policy for all Lambda functions ──────────────────────
    const lambdaBasePolicy = new iam.ManagedPolicy(this, 'LambdaBasePolicy', {
      managedPolicyName: 'master-demo-voicemail-lambda-base',
      description: 'Base permissions shared by all voicemail Lambda functions',
      statements: [
        new iam.PolicyStatement({
          sid: 'CloudWatchLogs',
          effect: iam.Effect.ALLOW,
          actions: [
            'logs:CreateLogGroup',
            'logs:CreateLogStream',
            'logs:PutLogEvents',
          ],
          resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/lambda/*`]
        }),
      ],
    });

    // ── 4b. dump-to-s3 role ──────────────────────────────────────────────────
    const dumpToS3Role = new iam.Role(this, 'DumpToS3Role', {
      roleName: 'master-demo-dump-to-s3-role',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [lambdaBasePolicy],
    });
    dumpToS3Role.addToPolicy(new iam.PolicyStatement({
      sid: 'ReadConnectRecordings',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject', 's3:GetObjectTagging'],
      resources: [`arn:aws:s3:::${CONFIG.connectRecordingsBucketName}/*`],
    }));
    dumpToS3Role.addToPolicy(new iam.PolicyStatement({
      sid: 'WriteVoicemailBucket',
      effect: iam.Effect.ALLOW,
      actions: ['s3:PutObject', 's3:PutObjectTagging', 's3:GetObject', 's3:GetObjectTagging'],
      resources: [
        `arn:aws:s3:::${CONFIG.voicemailBucketName}`,
        `arn:aws:s3:::${CONFIG.voicemailBucketName}/*`,
      ],
    }));
    dumpToS3Role.addToPolicy(new iam.PolicyStatement({
      sid: 'ConnectGetAttributes',
      effect: iam.Effect.ALLOW,
      actions: ['connect:GetContactAttributes'],
      resources: [`${CONFIG.connectInstanceArn}/contact/*`],
    }));
    dumpToS3Role.addToPolicy(new iam.PolicyStatement({
      sid: 'DynamoAgentLookup',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [agentTable.tableArn],
    }));

    // ── 4c. transcribe-recordings role ───────────────────────────────────────
    const transcribeRole = new iam.Role(this, 'TranscribeRole', {
      roleName: 'master-demo-transcribe-recordings-role',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [lambdaBasePolicy],
    });
    transcribeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ReadVoicemailRecordings',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject'],
      resources: [`arn:aws:s3:::${CONFIG.voicemailBucketName}/${CONFIG.recordingsFolder}/*`],
    }));
    transcribeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'StartTranscribeJob',
      effect: iam.Effect.ALLOW,
      actions: [
        'transcribe:StartTranscriptionJob',
        'transcribe:GetTranscriptionJob',
        'transcribe:DeleteTranscriptionJob',
      ],
      resources: ['*'],  // Transcribe doesn't support resource-level ARNs for StartJob
    }));
    // Transcribe needs to read input and write output to S3
    transcribeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TranscribeS3Access',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject', 's3:PutObject'],
      resources: [`arn:aws:s3:::${CONFIG.voicemailBucketName}/*`],
    }));

    // ── 4d. presigner role ────────────────────────────────────────────────────
    const presignerRole = new iam.Role(this, 'PresignerRole', {
      roleName: 'master-demo-presigner-role',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [lambdaBasePolicy],
    });
    presignerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ReadSecret',
      effect: iam.Effect.ALLOW,
      actions: ['secretsmanager:GetSecretValue'],
      resources: [presignerSecret.secretArn],
    }));
    // Note: The actual S3 presigned URL is generated using the IAM *user*
    // credentials from Secrets Manager, not this role — so no direct S3 perm needed.

    // ── 4e. packager role ─────────────────────────────────────────────────────
    const packagerRole = new iam.Role(this, 'PackagerRole', {
      roleName: 'master-demo-packager-role',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [lambdaBasePolicy],
    });
    packagerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ReadTranscripts',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject', 's3:GetObjectTagging'],
      resources: [
        `arn:aws:s3:::${CONFIG.voicemailBucketName}/${CONFIG.recordingsFolder}/*`,
        `arn:aws:s3:::${CONFIG.voicemailBucketName}/${CONFIG.transcriptionsFolder}/*`,
      ],
    }));
    packagerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokePresigner',
      effect: iam.Effect.ALLOW,
      actions: ['lambda:InvokeFunction'],
      resources: [`arn:aws:lambda:${this.region}:${this.account}:function:${CONFIG.lambdaNames.presigner}`],
    }));
    packagerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ConnectCreateTask',
      effect: iam.Effect.ALLOW,
      actions: ['connect:StartTaskContact'],
      resources: [
        // Must allow the instance and the specific task template
        `${CONFIG.connectInstanceArn}`,
        `${CONFIG.connectInstanceArn}/contact-flow/*`,
        `${CONFIG.connectInstanceArn}/queue/*`,
        `${CONFIG.connectInstanceArn}/task-template/*`,
      ],
    }));

    // ── 5. LAMBDA FUNCTIONS ───────────────────────────────────────────────────
    // Common Lambda settings
    const commonLambdaProps = {
      runtime: lambda.Runtime.PYTHON_3_12,
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
    };

    // 5a. dump-to-s3 — triggered by Connect recordings bucket
    const dumpToS3Lambda = new lambda.Function(this, 'DumpToS3Lambda', {
      functionName: CONFIG.lambdaNames.dumpToS3,
      ...commonLambdaProps,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/dump_to_s3')),
      role: dumpToS3Role,
      environment: {
        SOURCE_BUCKET:        CONFIG.connectRecordingsBucketName,
        DESTINATION_BUCKET:   CONFIG.voicemailBucketName,
        RECORDINGS_FOLDER:    CONFIG.recordingsFolder,
        TABLE_NAME:           CONFIG.agentPhoneLookupTable,
        CONNECT_INSTANCE_ID:  CONFIG.connectInstanceId,
        REGION: this.region,
      },
      description: 'Copies Connect recordings to voicemail bucket and tags with contact attributes',
    });

    // 5b. transcribe-recordings — triggered when WAV lands in recordings/ folder
    const transcribeLambda = new lambda.Function(this, 'TranscribeLambda', {
      functionName: CONFIG.lambdaNames.transcribe,
      ...commonLambdaProps,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/transcribe_recordings')),
      role: transcribeRole,
      environment: {
        DESTINATION_BUCKET:   CONFIG.voicemailBucketName,
        RECORDINGS_FOLDER:    CONFIG.recordingsFolder,
        TRANSCRIPTIONS_FOLDER: CONFIG.transcriptionsFolder,
        REGION: this.region,
        TRANSCRIBE_ROLE_ARN:  transcribeRole.roleArn,
      },
      description: 'Starts Amazon Transcribe jobs for new voicemail recordings',
    });

    // 5c. presigner — invoked synchronously by packager
    const presignerLambda = new lambda.Function(this, 'PresignerLambda', {
      functionName: CONFIG.lambdaNames.presigner,
      ...commonLambdaProps,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/presigner')),
      role: presignerRole,
      environment: {
        DESTINATION_BUCKET:  CONFIG.voicemailBucketName,
        RECORDINGS_FOLDER:   CONFIG.recordingsFolder,
        SECRET_NAME:         CONFIG.secretName,
        REGION: this.region,
      },
      description: 'Generates presigned S3 URLs for voicemail audio playback',
    });

    // 5d. packager — triggered when transcription JSON lands in transcriptions/ folder
    const packagerLambda = new lambda.Function(this, 'PackagerLambda', {
      functionName: CONFIG.lambdaNames.packager,
      ...commonLambdaProps,
      timeout: cdk.Duration.seconds(120),
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/packager')),
      role: packagerRole,
      environment: {
        DESTINATION_BUCKET:   CONFIG.voicemailBucketName,
        RECORDINGS_FOLDER:    CONFIG.recordingsFolder,
        PRESIGNER_FUNCTION:   CONFIG.lambdaNames.presigner,
        CONNECT_INSTANCE_ID:  CONFIG.connectInstanceId,
        TASK_TEMPLATE_ID:     CONFIG.taskTemplateId,
        REGION: this.region,
      },
      description: 'Creates Connect tasks from completed voicemail transcriptions',
    });

    // ── 6. S3 EVENT NOTIFICATIONS ─────────────────────────────────────────────
    // NOTE: Adding notifications to an *imported* bucket (fromBucketName) requires
    // a custom resource. The cleanest approach for existing buckets is to add the
    // notification via CDK's BucketNotification construct, which creates a
    // CloudFormation custom resource under the hood.

    // Trigger dump-to-s3 when Connect puts a recording (WAV/SAV) in its bucket
    // ⚠️  If you get "NotificationConfiguration is not supported on an existing bucket"
    //     it means Connect owns the bucket and won't let CDK manage notifications.
    //     In that case, add the trigger manually in the Lambda console → Add trigger.
    connectRecordingsBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(dumpToS3Lambda),
      { suffix: '.wav' }
    );

    // Trigger transcribe when WAV lands in recordings/ folder of voicemail bucket
    // This uses CDK's BucketNotification which requires the bucket to allow it.
    // For the existing bucket you already own, this should work fine.
    voicemailBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(transcribeLambda),
      { prefix: `${CONFIG.recordingsFolder}/`, suffix: '.wav' }
    );

    // Trigger packager when transcription JSON lands in transcriptions/ folder
    voicemailBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(packagerLambda),
      { prefix: `${CONFIG.transcriptionsFolder}/`, suffix: '.json' }
    );

    // Grant Lambda permission for S3 to invoke them
    dumpToS3Lambda.addPermission('AllowS3Invocation', {
      principal: new iam.ServicePrincipal('s3.amazonaws.com'),
      sourceArn: `arn:aws:s3:::${CONFIG.connectRecordingsBucketName}`,
      sourceAccount: this.account,
    });
    transcribeLambda.addPermission('AllowS3Invocation', {
      principal: new iam.ServicePrincipal('s3.amazonaws.com'),
      sourceArn: `arn:aws:s3:::${CONFIG.voicemailBucketName}`,
      sourceAccount: this.account,
    });
    packagerLambda.addPermission('AllowS3Invocation', {
      principal: new iam.ServicePrincipal('s3.amazonaws.com'),
      sourceArn: `arn:aws:s3:::${CONFIG.voicemailBucketName}`,
      sourceAccount: this.account,
    });

    // ── 7. CONNECT CONTACT FLOWS ──────────────────────────────────────────────
    // CDK's aws-connect L1 constructs allow us to deploy the 3 flows as code.
    // Flow content is loaded from the JSON files you already have.
    // NOTE: CDK Connect flow resources use the *raw JSON string* of the flow.

    // Main_Entry_Flow
    // new connect.CfnContactFlow(this, 'MainEntryFlow', {
    //  instanceArn: CONFIG.connectInstanceArn,
    //  name: 'Main_Entry_Flow',
    //  type: 'CONTACT_FLOW',
    //  description: 'Main entry flow for incoming calls — routes to queue or voicemail',
      // Load content from NJH_Main_Entry__1_.json (your customized entry flow)
    //  content: JSON.stringify(require('../flows/Main_Entry_Flow.json')),
   //  });

    //Main_Customer_Queue_Flow_1
   const customerQueueFlowContent = JSON.stringify(require('../flows/Main_Customer_Queue_Flow_1.json'))
  .replace('MUSIC_PROMPT_ARN_PLACEHOLDER', musicPromptArn)
  .replace('MUSIC_PROMPT_ARN_PLACEHOLDER', musicPromptArn)
  .replace('BEEP_PROMPT_ARN_PLACEHOLDER', beepPromptArn)
  .replace('BASIC_QUEUE_ARN_PLACEHOLDER', basicQueueArn);

  new connect.CfnContactFlow(this, 'MainCustomerQueueFlow', {
    instanceArn: CONFIG.connectInstanceArn,
    name: 'Main_Customer_Queue_Flow_1',
    type: 'CUSTOMER_QUEUE',
    description: 'Queue flow — plays hold music and offers voicemail option',
    content: customerQueueFlowContent,
    });

    // VMX_VN_01 — voicemail recording + task creation flow
    new connect.CfnContactFlow(this, 'VmxVn01Flow', {
      instanceArn: CONFIG.connectInstanceArn,
      name: 'VMX_VN_01',
      type: 'CONTACT_FLOW',
      description: 'Voicemail recording flow — records message and triggers Lambda packager',
      content: JSON.stringify(require('../flows/VMX_VN_01.json')),
    });

    // // ── 8. CONNECT TASK TEMPLATE ──────────────────────────────────────────────
    // // CDK's CfnTaskTemplate creates the VoicemailTemplate in Connect.
    // // ⚠️  CloudFormation Connect task                                                      template support was added in 2023.
    // //     If your CDK/CFN version doesn't support it, create this manually once
    // //     and paste the ID into CONFIG.taskTemplateId above.
    // new connect.CfnTaskTemplate(this, 'VoicemailTaskTemplate', {
    //   instanceArn: CONFIG.connectInstanceArn,
    //   name: 'VoicemailTemplate',
    //   description: 'Voicemail for Queue Assignment',
    //   status: 'ACTIVE',
    //   fields: [
    //     {
    //       id: { name: 'Transcript Of Voicemail' },
    //       type: 'TEXT_AREA',
    //       description: 'Voicemail transcript text',
    //     },
    //     {
    //       id: { name: 'Click Link To Listen To Voicemail' },
    //       type: 'URL',
    //       description: 'Presigned S3 URL to play voicemail audio',
    //     },
    //     {
    //       id: { name: 'Customer Number' },
    //       type: 'TEXT_AREA',
    //       description: "Caller's phone number",
    //     },
    //     {
    //       id: { name: 'Voicemail Created On' },
    //       type: 'TEXT_AREA',
    //       description: 'Timestamp when voicemail was received',
    //     },
    //   ],
    // });

    // ── 9. IAM USER FOR PRESIGNED URLS ────────────────────────────────────────
    // A dedicated IAM user whose long-term credentials are stored in Secrets
    // Manager. This is necessary because presigned URLs must be signed with
    // credentials that remain valid for the URL's lifetime (up to 7 days).
    // Lambda execution role credentials expire and break the URL after ~1 hour.
    const presignedUrlUser = new iam.User(this, 'PresignedUrlUser', {
      userName: 'master-demo-presigned-url-user',
    });
    presignedUrlUser.addToPolicy(new iam.PolicyStatement({
      sid: 'GetVoicemailRecordings',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject'],
      resources: [`arn:aws:s3:::${CONFIG.voicemailBucketName}/${CONFIG.recordingsFolder}/*`],
    }));

    // ── 10. OUTPUTS ────────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'DumpToS3LambdaArn', {
      value: dumpToS3Lambda.functionArn,
      description: 'ARN of the dump-to-s3 Lambda (add as S3 trigger on Connect bucket)',
      exportName: 'DumpToS3LambdaArn',
    });
    new cdk.CfnOutput(this, 'TranscribeLambdaArn', {
      value: transcribeLambda.functionArn,
      description: 'ARN of the transcribe-recordings Lambda',
      exportName: 'TranscribeLambdaArn',
    });
    new cdk.CfnOutput(this, 'PresignerLambdaArn', {
      value: presignerLambda.functionArn,
      description: 'ARN of the presigner Lambda',
      exportName: 'PresignerLambdaArn',
    });
    new cdk.CfnOutput(this, 'PackagerLambdaArn', {
      value: packagerLambda.functionArn,
      description: 'ARN of the packager Lambda',
      exportName: 'PackagerLambdaArn',
    });
    new cdk.CfnOutput(this, 'PresignedUrlUserArn', {
      value: presignedUrlUser.userArn,
      description: 'ARN of the IAM user for presigned URLs — create access key and store in Secrets Manager',
      exportName: 'PresignedUrlUserArn',
    });
  }
}
