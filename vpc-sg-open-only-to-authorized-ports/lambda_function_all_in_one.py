import boto3
import json
import logging
from datetime import datetime

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Enable debug mode
DEBUG_MODE = True

def lambda_handler(event, context):
    """
    AWS Lambda function to evaluate and optionally remediate security groups.
    Works with AWS Config to report compliance.
    Test with remediate=true in a safe environment to confirm rules are removed.
    Test with remediate=false to verify evaluation without changes.
    """
    if DEBUG_MODE:
        logger.info(f"Lambda invoked at {datetime.now().isoformat()}")
        logger.info(f"Event: {json.dumps(event)}")
        logger.info(f"Context: {context.function_name}, {context.aws_request_id}")
    
    # Initialize AWS clients
    config_client = boto3.client('config')
    ec2_client = boto3.client('ec2')

    # Parse event data
    invoking_event = json.loads(event['invokingEvent'])
    rule_parameters = json.loads(event.get('ruleParameters', '{}'))
    remediate = rule_parameters.get('remediate', False)  # Remediation flag (default: False)
    is_scheduled = invoking_event.get('messageType') == 'ScheduledNotification'

    if DEBUG_MODE:
        logger.info(f"Remediation enabled: {remediate}")
        logger.info(f"Is scheduled event: {is_scheduled}")

    evaluations = []

    if is_scheduled:
        # Evaluate all security groups for scheduled events
        if DEBUG_MODE:
            logger.info("Processing scheduled event - evaluating all security groups")
        response = ec2_client.describe_security_groups()
        if DEBUG_MODE:
            logger.info(f"Found {len(response['SecurityGroups'])} security groups to evaluate")
        
        for sg in response['SecurityGroups']:
            if DEBUG_MODE:
                logger.info(f"Evaluating security group: {sg['GroupId']} ({sg.get('GroupName', 'No name')})")
            evaluation = evaluate_and_remediate(ec2_client, sg['GroupId'], remediate)
            if evaluation:
                evaluation.update({
                    'ComplianceResourceType': 'AWS::EC2::SecurityGroup',
                    'ComplianceResourceId': sg['GroupId'],
                    'OrderingTimestamp': invoking_event['notificationCreationTime']
                })
                evaluations.append(evaluation)
    else:
        # Evaluate a specific security group for configuration changes
        config_item = invoking_event.get('configurationItem', {})
        if DEBUG_MODE:
            logger.info(f"Processing configuration change event for resource: {config_item.get('resourceType')}")
        
        if config_item.get('resourceType') == 'AWS::EC2::SecurityGroup':
            group_id = config_item['resourceId']
            if DEBUG_MODE:
                logger.info(f"Evaluating specific security group: {group_id}")
            
            evaluation = evaluate_and_remediate(ec2_client, group_id, remediate)
            if evaluation:
                evaluation.update({
                    'ComplianceResourceType': 'AWS::EC2::SecurityGroup',
                    'ComplianceResourceId': group_id,
                    'OrderingTimestamp': config_item['configurationItemCaptureTime']
                })
                evaluations.append(evaluation)

    # Send evaluations to AWS Config
    if evaluations:
        if DEBUG_MODE:
            logger.info(f"Sending {len(evaluations)} evaluations to AWS Config")
        config_client.put_evaluations(Evaluations=evaluations, ResultToken=event['resultToken'])

    if DEBUG_MODE:
        logger.info(f"Lambda execution complete. Results: {json.dumps(evaluations)}")
    
    return evaluations

def evaluate_and_remediate(ec2_client, group_id, remediate):
    """
    Evaluate a security group and optionally remediate non-compliant rules.
    """
    try:
        if DEBUG_MODE:
            logger.info(f"Evaluating rules for security group: {group_id}")
        
        # Fetch security group rules
        response = ec2_client.describe_security_group_rules(
            Filters=[{'Name': 'group-id', 'Values': [group_id]}]
        )
        rules = response['SecurityGroupRules']

        if DEBUG_MODE:
            logger.info(f"Found {len(rules)} rules in security group {group_id}")
        
        # Check for non-compliant rules (0.0.0.0/0)
        non_compliant_rules = []
        for rule in rules:
            if rule.get('CidrIpv4') == '0.0.0.0/0':
                direction = 'inbound' if not rule['IsEgress'] else 'outbound'
                non_compliant_rules.append((direction, rule['SecurityGroupRuleId']))
                if DEBUG_MODE:
                    protocol = rule.get('IpProtocol', 'all')
                    from_port = rule.get('FromPort', 'all')
                    to_port = rule.get('ToPort', 'all')
                    logger.info(f"Found non-compliant {direction} rule {rule['SecurityGroupRuleId']}: "
                               f"Protocol: {protocol}, Ports: {from_port}-{to_port}")

        if non_compliant_rules:
            annotation = f"Non-compliant rules found in {group_id}: {len(non_compliant_rules)} rules"
            if DEBUG_MODE:
                logger.info(f"Security group {group_id} is NON_COMPLIANT with {len(non_compliant_rules)} open rules")
            
            if remediate:
                if DEBUG_MODE:
                    logger.info(f"Remediating security group {group_id} by removing {len(non_compliant_rules)} rules")
                
                # Remediate by removing non-compliant rules
                for direction, rule_id in non_compliant_rules:
                    if DEBUG_MODE:
                        logger.info(f"Removing {direction} rule {rule_id} from security group {group_id}")
                    
                    if direction == 'inbound':
                        ec2_client.revoke_security_group_ingress(GroupId=group_id, SecurityGroupRuleIds=[rule_id])
                    else:
                        ec2_client.revoke_security_group_egress(GroupId=group_id, SecurityGroupRuleIds=[rule_id])
                
                if DEBUG_MODE:
                    logger.info(f"Remediation complete for security group {group_id}")
                
                return {
                    'ComplianceType': 'COMPLIANT',
                    'Annotation': f"Remediated {group_id} by removing non-compliant rules."
                }
            return {
                'ComplianceType': 'NON_COMPLIANT',
                'Annotation': annotation
            }
        
        if DEBUG_MODE:
            logger.info(f"Security group {group_id} is COMPLIANT")
        
        return {
            'ComplianceType': 'COMPLIANT',
            'Annotation': f"{group_id} has no rules allowing 0.0.0.0/0."
        }

    except ec2_client.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'InvalidGroup.NotFound':
            if DEBUG_MODE:
                logger.info(f"Security group {group_id} does not exist, skipping evaluation")
            return None  # Skip non-existent security groups
        
        if DEBUG_MODE:
            logger.error(f"AWS ClientError evaluating {group_id}: {e.response['Error']['Code']} - {e.response['Error']['Message']}")
        
        logger.error(f"Error evaluating {group_id}: {str(e)}")
        return {
            'ComplianceType': 'INSUFFICIENT_DATA',
            'Annotation': f"Error: {str(e)[:200]}"
        }
    except Exception as e:
        if DEBUG_MODE:
            logger.error(f"Unexpected error evaluating {group_id}: {str(e)}")
        
        return {
            'ComplianceType': 'INSUFFICIENT_DATA',
            'Annotation': f"Unexpected error: {str(e)[:200]}"
        }
