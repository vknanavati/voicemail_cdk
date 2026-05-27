#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { VoicemailStack } from '../lib/master-demo-voicemail-cdk-stack';

const app = new cdk.App();

new VoicemailStack(app, 'MasterDemoVoicemailStack', {
  env: {
    account: '308665918648',
    region: 'us-west-2',
  },
  description: 'Master Demo Voicemail Solution for Amazon Connect',
});
