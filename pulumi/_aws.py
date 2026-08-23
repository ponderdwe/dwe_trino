"""
DWE Trino Infrastructure — AWS Pulumi IaC
Provisions: ALB (HTTP→HTTPS) + ASG + EC2 + Route53 CNAME

Single-instance ASG (stateful — Trino needs persistent storage).
Rolling update via instance refresh: new instance boots, old drains and terminates.
Run with: pulumi stack select prod && pulumi up --yes
"""

import base64
import json
from pathlib import Path

import boto3  # used only by Pulumi at deploy time (not EC2 bootstrap)
import pulumi
import pulumi_aws as aws
import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Hydration config — written by dwe-core at create-service / update-service time
# ─────────────────────────────────────────────────────────────────────────────
_dwe = yaml.safe_load((Path(__file__).parent / "dwe-hydration.yaml").read_text())
project_name    = _dwe["project_name"]
git_repo_url    = _dwe["git_repo_url"]
adapter_version = _dwe["adapter_version"]

# ─────────────────────────────────────────────────────────────────────────────
# Stack Config
# ─────────────────────────────────────────────────────────────────────────────
config = pulumi.Config()
env          = config.require("environment")
git_branch   = config.require("git_branch")
secret_id    = config.require("secret_id")
instance_type        = config.get("instance_type") or "r6i.large"
volume_size          = int(config.get("volume_size") or "100")
worker_count         = int(config.get("worker_count") or "1")
aws_region           = config.get("aws_region") or "us-east-1"
app_port             = 8080
startup_code_version = config.get("startup_code_version") or ""

suffix = f"-{env}" if env != "prod" else ""
tags = {
    "Project":     project_name,
    "ManagedBy":   "Pulumi",
    "Environment": env,
    "GitBranch":   git_branch,
}

# ─────────────────────────────────────────────────────────────────────────────
# Load secrets from AWS Secrets Manager
# ─────────────────────────────────────────────────────────────────────────────
def get_secret(sid: str) -> dict:
    client = boto3.client("secretsmanager", region_name=aws_region)
    resp = client.get_secret_value(SecretId=sid)
    return json.loads(resp["SecretString"])

secrets = get_secret(secret_id)

# ── Infrastructure ────────────────────────────────────────────────────────────
vpc_id                = secrets["VPC_ID"]
alb_subnet_ids        = json.loads(secrets["ALB_SUBNET_IDS"])
key_name              = secrets["KEY_NAME"]
ec2_security_group_id = secrets.get("EC2_SECURITY_GROUP_ID", "")
lb_security_group_id  = secrets.get("LB_SECURITY_GROUP_ID", "")
alb_internal          = secrets.get("ALB_INTERNAL", "false").lower() == "true"

# ── Networking / DNS ──────────────────────────────────────────────────────────
route53_zone_id     = secrets["ROUTE53_ZONE_ID"]
dns_name            = secrets["DNS_NAME"]
acm_certificate_arn = secrets["ACM_CERTIFICATE_ARN"]

# ── Git ───────────────────────────────────────────────────────────────────────
git_deploy_token    = secrets["git_deploy_token"]
git_deploy_username = secrets.get("git_deploy_username", "x-token-auth")

# ── Trino runtime ─────────────────────────────────────────────────────────────
# Individual values are not used in Python — config_generator.py reads them
# from .env on the EC2 instance. Validate presence here to catch missing secrets early.
for _key in ("CATALOG_URL", "ICEBERG_WAREHOUSE_DIR", "TRINO_USER", "TRINO_PASSWORD", "TRINO_SHARED_SECRET"):
    if not secrets.get(_key):
        raise ValueError(f"Required secret '{_key}' missing from Secrets Manager secret {secret_id}")

# ─────────────────────────────────────────────────────────────────────────────
# IAM — EC2 role (SSM + Secrets Manager + S3 for Iceberg warehouse)
# ─────────────────────────────────────────────────────────────────────────────
instance_role = aws.iam.Role(
    f"{project_name}-role{suffix}",
    name=f"{project_name}-role{suffix}",
    assume_role_policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }),
    tags=tags,
)
aws.iam.RolePolicyAttachment(f"{project_name}-ssm{suffix}",     role=instance_role.name, policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
aws.iam.RolePolicyAttachment(f"{project_name}-sm{suffix}",      role=instance_role.name, policy_arn="arn:aws:iam::aws:policy/SecretsManagerReadWrite")
aws.iam.RolePolicyAttachment(f"{project_name}-s3{suffix}",      role=instance_role.name, policy_arn="arn:aws:iam::aws:policy/AmazonS3FullAccess")

instance_profile = aws.iam.InstanceProfile(
    f"{project_name}-profile{suffix}", name=f"{project_name}-profile{suffix}",
    role=instance_role.name, tags=tags,
)

# ─────────────────────────────────────────────────────────────────────────────
# Security Groups
# ─────────────────────────────────────────────────────────────────────────────
vpc_info = aws.ec2.get_vpc(id=vpc_id)
access_cidr = [vpc_info.cidr_block] if alb_internal else ["0.0.0.0/0"]

alb_sg = aws.ec2.SecurityGroup(
    f"{project_name}-alb-sg{suffix}",
    name=f"{project_name}-alb-sg{suffix}",
    description="Trino ALB",
    vpc_id=vpc_id,
    tags={**tags, "Name": f"{project_name}-alb-sg{suffix}"},
)
aws.ec2.SecurityGroupRule(f"{project_name}-alb-http{suffix}",
    type="ingress", security_group_id=alb_sg.id,
    protocol="tcp", from_port=80, to_port=80, cidr_blocks=access_cidr)
aws.ec2.SecurityGroupRule(f"{project_name}-alb-https{suffix}",
    type="ingress", security_group_id=alb_sg.id,
    protocol="tcp", from_port=443, to_port=443, cidr_blocks=access_cidr)
aws.ec2.SecurityGroupRule(f"{project_name}-alb-egress{suffix}",
    type="egress", security_group_id=alb_sg.id,
    protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])

ec2_sg = aws.ec2.SecurityGroup(
    f"{project_name}-ec2-sg{suffix}",
    name=f"{project_name}-ec2-sg{suffix}",
    description="Trino EC2",
    vpc_id=vpc_id,
    tags={**tags, "Name": f"{project_name}-ec2-sg{suffix}"},
)
aws.ec2.SecurityGroupRule(f"{project_name}-ec2-app{suffix}",
    type="ingress", security_group_id=ec2_sg.id,
    protocol="tcp", from_port=app_port, to_port=app_port,
    source_security_group_id=alb_sg.id)
aws.ec2.SecurityGroupRule(f"{project_name}-ec2-ssh{suffix}",
    type="ingress", security_group_id=ec2_sg.id,
    protocol="tcp", from_port=22, to_port=22, cidr_blocks=access_cidr)
aws.ec2.SecurityGroupRule(f"{project_name}-ec2-egress{suffix}",
    type="egress", security_group_id=ec2_sg.id,
    protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])

# ─────────────────────────────────────────────────────────────────────────────
# AMI — Ubuntu 20.04 LTS
# ─────────────────────────────────────────────────────────────────────────────
ubuntu_ami = aws.ec2.get_ami(
    most_recent=True,
    filters=[
        aws.ec2.GetAmiFilterArgs(name="name",               values=["ubuntu/images/hvm-ssd/ubuntu-focal-20.04-amd64-server-*"]),
        aws.ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
    ],
    owners=["099720109477"],
)

# ─────────────────────────────────────────────────────────────────────────────
# User data — install Docker, clone repo, write .env from Secrets Manager
# Production: explicit -f docker-compose.yml so override file is NOT merged
# ─────────────────────────────────────────────────────────────────────────────
user_data_script = f"""#!/bin/bash
set -e
exec > >(tee /var/log/trino-init.log | logger -t trino-init) 2>&1

# startup_code_version={startup_code_version}

apt-get update -y
apt-get install -y apt-transport-https ca-certificates curl gnupg-agent software-properties-common git jq unzip python3 apache2-utils

# AWS CLI v2 (EC2 IAM role provides credentials — no keys needed)
curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install

# Docker + Compose
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | apt-key add -
add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable"
apt-get update -y && apt-get install -y docker-ce docker-ce-cli containerd.io
curl -L "https://github.com/docker/compose/releases/download/v2.24.0/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

# Fetch all secrets from AWS Secrets Manager
SECRET_JSON=$(aws secretsmanager get-secret-value --secret-id {secret_id} --region {aws_region} --query SecretString --output text)

GIT_USER=$(echo "$SECRET_JSON" | jq -r '.git_deploy_username // "x-token-auth"')
GIT_TOKEN=$(echo "$SECRET_JSON" | jq -r '.git_deploy_token')

# Clone repo with credentials injected into URL
REPO_URL="{git_repo_url}"
REPO_PATH=$(echo "$REPO_URL" | sed 's,https://,,')
git clone "https://$GIT_USER:$GIT_TOKEN@$REPO_PATH" /home/ubuntu/trino
git -C /home/ubuntu/trino checkout {git_branch}
git -C /home/ubuntu/trino rev-parse HEAD > /home/ubuntu/trino/.schema-version

# Write .env from all Secrets Manager key=value pairs
echo "$SECRET_JSON" | jq -r 'to_entries[] | .key + "=" + (.value | tostring)' > /home/ubuntu/trino/.env
chmod 600 /home/ubuntu/trino/.env

# Generate Trino config files (coordinator-config.properties, iceberg.properties, password.db)
cd /home/ubuntu/trino
python3 config_generator.py envs_prod.json --env-file .env

# Use explicit file so docker-compose.override.yml (local dev) is NOT merged
docker-compose -f docker-compose.yml up -d --scale trino-worker={worker_count}
"""
user_data = base64.b64encode(user_data_script.encode()).decode()

# ─────────────────────────────────────────────────────────────────────────────
# Launch Template
# ─────────────────────────────────────────────────────────────────────────────
sg_ids = [ec2_sg.id] + ([ec2_security_group_id] if ec2_security_group_id else [])
lt = aws.ec2.LaunchTemplate(
    f"{project_name}-lt{suffix}",
    name_prefix=f"{project_name}{suffix}-",
    image_id=ubuntu_ami.id,
    instance_type=instance_type,
    key_name=key_name,
    vpc_security_group_ids=sg_ids,
    iam_instance_profile=aws.ec2.LaunchTemplateIamInstanceProfileArgs(name=instance_profile.name),
    block_device_mappings=[aws.ec2.LaunchTemplateBlockDeviceMappingArgs(
        device_name="/dev/sda1",
        ebs=aws.ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
            volume_size=volume_size, volume_type="gp2",
            encrypted=True, delete_on_termination=True,
        ),
    )],
    user_data=user_data,
    tag_specifications=[aws.ec2.LaunchTemplateTagSpecificationArgs(
        resource_type="instance",
        tags={**tags, "Name": f"trino{suffix}"},
    )],
    tags=tags,
)

# ─────────────────────────────────────────────────────────────────────────────
# ALB
# ─────────────────────────────────────────────────────────────────────────────
alb_sg_ids = [alb_sg.id] + ([lb_security_group_id] if lb_security_group_id else [])
alb = aws.lb.LoadBalancer(
    f"{project_name}-alb{suffix}",
    name=f"{project_name}-alb{suffix}",
    internal=alb_internal,
    load_balancer_type="application",
    security_groups=alb_sg_ids,
    subnets=alb_subnet_ids,
    idle_timeout=600,
    tags={**tags, "Name": f"{project_name}-alb{suffix}"},
)

tg = aws.lb.TargetGroup(
    f"{project_name}-tg{suffix}",
    name=f"{project_name}-tg{suffix}",
    port=app_port,
    protocol="HTTP",
    vpc_id=vpc_id,
    deregistration_delay=30,
    health_check=aws.lb.TargetGroupHealthCheckArgs(
        enabled=True,
        path="/v1/info",
        port=str(app_port),
        protocol="HTTP",
        healthy_threshold=2,
        interval=30,
        timeout=10,
        unhealthy_threshold=3,
        matcher="200",
    ),
    tags={**tags, "Name": f"{project_name}-tg{suffix}"},
)

# HTTP → HTTPS redirect
aws.lb.Listener(
    f"{project_name}-http{suffix}",
    load_balancer_arn=alb.arn,
    port=80,
    protocol="HTTP",
    default_actions=[aws.lb.ListenerDefaultActionArgs(
        type="redirect",
        redirect=aws.lb.ListenerDefaultActionRedirectArgs(port="443", protocol="HTTPS", status_code="HTTP_301"),
    )],
)

# HTTPS → target group
aws.lb.Listener(
    f"{project_name}-https{suffix}",
    load_balancer_arn=alb.arn,
    port=443,
    protocol="HTTPS",
    ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06",
    certificate_arn=acm_certificate_arn,
    default_actions=[aws.lb.ListenerDefaultActionArgs(type="forward", target_group_arn=tg.arn)],
)

# ─────────────────────────────────────────────────────────────────────────────
# Auto Scaling Group (min=max=1 — stateful, single instance)
# ─────────────────────────────────────────────────────────────────────────────
asg = aws.autoscaling.Group(
    f"{project_name}-asg{suffix}",
    name=f"{project_name}-asg{suffix}",
    min_size=1, max_size=2, desired_capacity=1,
    vpc_zone_identifiers=alb_subnet_ids,
    target_group_arns=[tg.arn],
    launch_template=aws.autoscaling.GroupLaunchTemplateArgs(id=lt.id, version="$Latest"),
    health_check_type="ELB",
    health_check_grace_period=600,
    instance_refresh=aws.autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=aws.autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=0,
            instance_warmup=300,
        ),
    ),
    tags=[aws.autoscaling.GroupTagArgs(key=k, value=v, propagate_at_launch=True) for k, v in {**tags, "Name": f"trino{suffix}"}.items()],
)

# ─────────────────────────────────────────────────────────────────────────────
# Route53 — CNAME → ALB
# ─────────────────────────────────────────────────────────────────────────────
aws.route53.Record(
    f"{project_name}-dns{suffix}",
    zone_id=route53_zone_id,
    name=dns_name,
    type="CNAME",
    ttl=30,
    records=[alb.dns_name],
)

# ─────────────────────────────────────────────────────────────────────────────
# KG Phase 2 — register deployed services with the Deploy Management API
# ─────────────────────────────────────────────────────────────────────────────
_kg_host     = secrets.get("KG_API_HOST", "")
_kg_token    = secrets.get("KG_API_TOKEN", "")
_kg_mappings = _dwe.get("kg_mappings")

if _kg_host and _kg_token and _kg_mappings:
    import httpx as _httpx
    import warnings as _warnings

    _adapter_name  = _kg_mappings["adapter_name"]
    _kg_props_keys = _kg_mappings.get("kg_adapter_properties", {})
    _kg_outputs    = _kg_mappings.get("kg_pulumi_outputs", {})
    _kg_services   = _kg_mappings.get("services", [])

    _pulumi_export_map = {
        "alb_dns":  alb.dns_name,
        "url":      pulumi.Output.from_input(f"https://{dns_name}"),
        "asg_name": asg.name,
    }
    _out_names = list(_kg_outputs.keys())
    _out_vals  = [_pulumi_export_map[_kg_outputs[n]] for n in _out_names]

    def _phase2_hydrate(*resolved):
        props = dict(zip(_out_names, resolved))
        for prop, secret_key in _kg_props_keys.items():
            props[prop] = secrets.get(secret_key, "")

        _headers = {"Authorization": f"Bearer {_kg_token}"}
        _base    = _kg_host.rstrip("/")
        try:
            _httpx.patch(
                f"{_base}/adapters/{_adapter_name}/{env}",
                json={"properties": props},
                headers=_headers,
                timeout=10,
            )
        except Exception as _exc:
            _warnings.warn(f"[dwe-kg] PATCH /adapters failed: {_exc}")

        _svc_payloads = []
        for _svc in _kg_services:
            _trigger = _svc.get("trigger_secret", "")
            if _trigger and not secrets.get(_trigger):
                continue
            _svc_props = {p: secrets.get(sk, "") for p, sk in _svc.get("properties", {}).items()}
            _svc_payloads.append({"name": _svc["name"], "properties": _svc_props})

        if _svc_payloads:
            try:
                _httpx.post(
                    f"{_base}/adapters/{_adapter_name}/{env}/services",
                    json={"services": _svc_payloads},
                    headers=_headers,
                    timeout=10,
                )
            except Exception as _exc:
                _warnings.warn(f"[dwe-kg] POST /services failed: {_exc}")

    pulumi.Output.all(*_out_vals).apply(_phase2_hydrate)

# ─────────────────────────────────────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────────────────────────────────────
pulumi.export("alb_dns",     alb.dns_name)
pulumi.export("url",         f"https://{dns_name}")
pulumi.export("asg_name",    asg.name)
pulumi.export("environment", env)
